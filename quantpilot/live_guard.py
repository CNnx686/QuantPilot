"""Fail-closed spot guard beneath every Freqtrade order entry path.

The snapshot provider must perform authoritative reads. A successful remote submit
is still blocked until reconciliation proves Freqtrade persisted that exact ID.
"""
import contextvars
import functools
import inspect
import math
import time
import uuid
from freqtrade.exceptions import DependencyException
from . import live_store as ledger

PAIRS = {'BTC/USDT', 'ETH/USDT'}
scope = contextvars.ContextVar('live_order_intent', default=None)
DENIAL_CODES = {
    'Entry control paused or stale': 'entry_control_paused_or_stale',
    'Unresolved order requires reconciliation': 'reconciliation_required',
    'Daily baseline requires authoritative reconciliation': 'daily_risk_checkpoint_required',
    'Daily loss budget reached': 'daily_loss_limit',
    'Reserved cash budget exceeded': 'reserved_cash_limit',
    'Reserved exposure budget exceeded': 'reserved_exposure_limit',
    'Cash or order budget exceeded': 'cash_or_order_limit',
    'Exposure budget exceeded': 'exposure_limit',
    'Live entry control changed before transport': 'control_changed_before_dispatch',
    'Live intent is no longer eligible for transport': 'intent_changed_before_dispatch',
    'Uncertain protective order requires exact reconciliation before another exit': 'protective_order_uncertain',
    'Stale account evidence': 'stale_account_evidence',
    'Transport snapshot expired': 'stale_account_evidence',
}


def finite(value, positive=False):
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise ValueError('Invalid numeric input')
    return number


def validate_snapshot(snapshot, pair, side, amount, rate, limits):
    now = time.time()
    for field in ('quotes_at', 'balances_at', 'orders_at', 'history_at'):
        stamp = finite(snapshot[field], True)
        if not 0 <= now - stamp <= 15:
            raise ValueError('Stale account evidence')
    if snapshot['history_complete'] is not True or snapshot['external_writer'] is not False:
        raise ValueError('Account history requires reconciliation')
    balances = snapshot['balances']
    if set(balances) - {'USDT', 'BTC', 'ETH'}:
        raise ValueError('Unsupported assets or fee currency')
    for item in balances.values():
        if abs(finite(item['free']) + finite(item['used']) - finite(item['total'])) > 1e-8:
            raise ValueError('Inconsistent account balance')
    bids = {p: finite(snapshot['quotes'][p]['bid'], True) for p in PAIRS}
    ask = finite(snapshot['quotes'][pair]['ask'], True)
    bid = bids[pair]
    if ask < bid or (ask - bid) / bid > finite(limits['max_spread']):
        raise ValueError('Invalid spread')
    base = pair.split('/')[0]
    if side == 'sell':
        if amount > finite(balances.get(base, {}).get('free', 0)) + 1e-12:
            raise ValueError('Exit could increase exposure')
        return 0, 0, 0
    equity = finite(balances['USDT']['total'])
    exposure = 0
    for asset in ('BTC', 'ETH'):
        value = finite(balances.get(asset, {}).get('total', 0)) * bids[asset + '/USDT']
        equity += value
        exposure += value
    seen = set()
    for order in snapshot['open_orders']:
        identity = (order.get('symbol'), str(order.get('id')))
        if not order.get('id') or identity in seen or order['symbol'] not in PAIRS:
            raise ValueError('Unidentified remote order')
        seen.add(identity)
        if order['side'] == 'buy':
            exposure += finite(order['remaining']) * finite(order['price'], True)
    cost = amount * max(rate, ask) * (1 + finite(limits['buffer']))
    if cost > finite(limits['per_order'], True) or cost > finite(balances['USDT']['free']):
        raise ValueError('Cash or order budget exceeded')
    if exposure + cost > min(finite(limits['capital'], True), finite(limits['exposure'], True)):
        raise ValueError('Exposure budget exceeded')
    return cost, equity, exposure


def reserve(pair, side, amount, rate, snapshot, limits):
    if pair not in PAIRS or side not in {'buy', 'sell'}:
        raise ValueError('Unsupported spot order')
    amount, rate = finite(amount, True), finite(rate, True)
    # Exit safety needs only fresh holdings; broken flow history must not disable exits.
    if side == 'sell':
        if not 0 <= time.time() - finite(snapshot['balances_at'], True) <= 15:
            raise ValueError('Fresh holdings required for exit')
        if amount > finite(snapshot['balances'][pair.split('/')[0]]['free']) + 1e-12:
            raise ValueError('Exit exceeds owned free balance')
        cost, equity, exposure = 0, 0, 0
    else:
        cost, equity, exposure = validate_snapshot(snapshot, pair, side, amount, rate, limits)
    identifier = 'qp' + uuid.uuid4().hex[:30]
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        control = ledger.get('deployment', {}, db)
        if side == 'sell' and db.execute("SELECT 1 FROM intents WHERE pair=? AND side='sell' AND (state IN ('intent','unknown') OR (remote_id IS NOT NULL AND managed=0)) LIMIT 1", (pair,)).fetchone():
            raise ValueError('Uncertain protective order requires exact reconciliation before another exit')
        if side == 'buy':
            if control.get('status') != 'active' or not 0 <= time.time() - control.get('heartbeat', 0) <= 15:
                raise ValueError('Entry control paused or stale')
            if ledger.uncertain(db) or ledger.get('reconciliation_required', False, db):
                raise ValueError('Unresolved order requires reconciliation')
            baseline = ledger.get('daily_risk', db=db)
            if not baseline or baseline['day'] != time.strftime('%Y-%m-%d', time.gmtime()):
                raise ValueError('Daily baseline requires authoritative reconciliation')
            flows = float(snapshot.get('net_inflows', 0))
            if not math.isfinite(flows):
                raise ValueError('Invalid external flow')
            if baseline['equity'] + flows - baseline['net_inflows'] - equity > finite(limits['daily_loss'], True):
                raise ValueError('Daily loss budget reached')
            remote = {o.get('clientOrderId') for o in snapshot['open_orders']}
            pending = sum(r['reserve'] for r in db.execute("SELECT id,reserve FROM intents WHERE state IN ('intent','submitted','partial','unknown')") if r['id'] not in remote)
            if pending + cost > snapshot['balances']['USDT']['free']:
                raise ValueError('Reserved cash budget exceeded')
            if exposure + pending + cost > min(limits['capital'], limits['exposure']):
                raise ValueError('Reserved exposure budget exceeded')
        db.execute('INSERT INTO intents(id,pair,side,amount,reserve,state,created,transport_started) VALUES(?,?,?,?,?,?,?,0)',
                   (identifier, pair, side, amount, cost, 'intent', time.time()))
    return identifier


def wrap_exchange(exchange, snapshot_provider, limits):
    if getattr(exchange, '_quantpilot_live_guard', False):
        return exchange
    if exchange._config.get('trading_mode') != 'spot' or exchange._config.get('dry_run') is not False:
        raise ValueError('Guard requires actual spot configuration')
    transport = exchange._api.create_order
    enrolled_account = ledger.get('account')

    @functools.wraps(transport)
    def guarded_transport(*args, **kwargs):
        context = scope.get()
        if not context or context['called']:
            raise DependencyException('Live order denied: unexpected transport path')
        bound = inspect.signature(transport).bind(*args, **kwargs)
        bound.apply_defaults()
        params = dict(bound.arguments.get('params') or {})
        if params.get('newClientOrderId') not in (None, context['id']):
            raise DependencyException('Live order denied: conflicting client identifier')
        params['newClientOrderId'] = context['id']
        bound.arguments['params'] = params
        with ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if any(not 0 <= time.time() - stamp <= 15 for stamp in context['snapshot_times']):
                raise ValueError('Transport snapshot expired')
            if context['side'] == 'buy':
                control = ledger.get('deployment', {}, db)
                if (control.get('status') != 'active'
                        or not 0 <= time.time() - control.get('heartbeat', 0) <= 15
                        or ledger.get('account', db=db) != enrolled_account
                        or control.get('account') != enrolled_account
                        or ledger.get('read_job', {}, db).get('status') == 'running'
                        or ledger.get('reconciliation_required', False, db)):
                    raise DependencyException('Live entry control changed before transport')
            claimed = db.execute("UPDATE intents SET transport_started=1 WHERE id=? AND state='intent' AND transport_started=0", (context['id'],))
            if claimed.rowcount != 1:
                raise DependencyException('Live intent is no longer eligible for transport')
            # From this point a commit failure is uncertain, never a proven no-send
            # cleanup. Set the marker before leaving the transaction context.
            context['called'] = True
        return transport(*bound.args, **bound.kwargs)

    exchange._api.create_order = guarded_transport

    def install(name):
        original = getattr(exchange, name)
        signature = inspect.signature(original)

        @functools.wraps(original)
        def guarded(*args, **kwargs):
            identifier = None
            token = None
            context = None
            pair, side = None, None
            stage = 'invalid_order_parameters'
            try:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                values = bound.arguments
                if values['leverage'] != 1 or values.get('reduceOnly', False):
                    raise ValueError('Only unleveraged spot is supported')
                pair, side, amount = values['pair'], values['side'], values['amount']
                if name == 'create_stoploss' and side != 'sell':
                    raise ValueError('Only protective sell stops supported')
                if name == 'create_order' and values['ordertype'] not in {'limit', 'market'}:
                    raise ValueError('Unsupported order type')
                rate = values.get('rate', values.get('stop_price'))
                amount = finite(amount, True)
                rate = finite(rate, True)
                amount = finite(exchange.amount_to_precision(pair, amount), True)
                bound.arguments['amount'] = amount
                if name == 'create_order' and hasattr(exchange, 'price_to_precision'):
                    rate = finite(exchange.price_to_precision(pair, rate), True)
                    bound.arguments['rate'] = rate
                market = exchange.markets[pair]
                minimum_amount = finite(market['limits']['amount']['min'], True)
                minimum_cost = finite(market['limits']['cost']['min'], True)
                if amount < minimum_amount or amount * rate < minimum_cost:
                    raise ValueError('Order is below exchange minimum')
                stage = 'account_evidence_unavailable'
                snapshot = snapshot_provider(side)
                stage = 'risk_admission_denied'
                identifier = reserve(pair, side, amount, rate, snapshot, limits)
                fields = ('balances_at',) if side == 'sell' else ('balances_at', 'quotes_at', 'orders_at', 'history_at')
                context = {'id': identifier, 'called': False, 'side': side,
                           'snapshot_times': [finite(snapshot[field], True) for field in fields]}
                token = scope.set(context)
                stage = 'dispatch_denied'
                result = original(*bound.args, **bound.kwargs)
                if not context['called'] or not result.get('id'):
                    raise ValueError('Order response is not authoritative')
                with ledger.connect() as db:
                    db.execute("UPDATE intents SET state='submitted',remote_id=? WHERE id=?", (str(result['id']), identifier))
                return result
            except Exception as exc:
                if identifier:
                    try:
                        if context and not context['called']:
                            with ledger.connect() as db:
                                db.execute("UPDATE intents SET state='cancelled',reserve=0,managed=1 WHERE id=? AND transport_started=0", (identifier,))
                        else:
                            ledger.mark_unknown(identifier)
                    except Exception:
                        pass  # The already committed intent itself remains fail-closed.
                try:
                    code = 'submission_unknown' if context and context['called'] else DENIAL_CODES.get(str(exc), stage)
                    ledger.put('last_order_denial', {'at': time.time(), 'code': code,
                               'pair': pair if pair in PAIRS else None, 'side': side if side in {'buy', 'sell'} else None})
                except Exception:
                    pass
                raise DependencyException('Live order denied or uncertain; inspect local reconciliation status') from None
            finally:
                if token is not None:
                    scope.reset(token)
        setattr(exchange, name, guarded)

    install('create_order')
    install('create_stoploss')
    exchange._quantpilot_live_guard = True
    return exchange


def install_resolver(snapshot_provider, limits):
    """Call before Worker construction, also covers Worker reconfiguration."""
    from freqtrade.resolvers import ExchangeResolver
    original = ExchangeResolver.load_exchange

    def load(*args, **kwargs):
        return wrap_exchange(original(*args, **kwargs), snapshot_provider, limits)
    ExchangeResolver.load_exchange = staticmethod(load)
    return original
