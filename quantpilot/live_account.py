"""Authoritative read-only Binance evidence and exact-ID reconciliation."""
import time
from . import live_store as ledger
from .live_guard import PAIRS, finite


def permissions(value):
    required = {'enableReading': True, 'enableSpotAndMarginTrading': True,
                'enableWithdrawals': False, 'ipRestrict': True,
                'enableInternalTransfer': False, 'permitsUniversalTransfer': False,
                'enableFutures': False, 'enableMargin': False}
    return all(value.get(key) is expected for key, expected in required.items())


def client():
    import ccxt
    from .live_cli import credentials
    account = ledger.get('account')
    if not account:
        raise ValueError('Local account enrollment required')
    return ccxt.binance(credentials(account['ref']) | {'enableRateLimit': True, 'timeout': 5000, 'requests_trust_env': True,
                        'options': {'defaultType': 'spot'}})


def history(exchange, method, since):
    """Bounded offset pagination; full final page requires another page, never assumed complete."""
    records = {}
    for page in range(10):
        batch = getattr(exchange, method)('USDT', since, 100, {'offset': page * 100})
        for item in batch:
            if not item.get('id') or item.get('currency') != 'USDT' or item.get('status') != 'ok':
                raise ValueError('Unknown external cashflow')
            if item['id'] in records:
                raise ValueError('Cashflow pagination repeated identifiers')
            records[item['id']] = item
        if len(batch) < 100:
            return list(records.values())
    raise ValueError('Cashflow pagination limit reached')


def snapshot(exchange, side='buy'):
    started_ms = int(time.time() * 1000)
    state = exchange.private_get_account()
    if state.get('canTrade') is not True or state.get('accountType') != 'SPOT':
        raise ValueError('Spot account status is not authoritative')
    balances = {}
    for item in state['balances']:
        free, used = finite(item['free']), finite(item['locked'])
        if free + used > 0:
            balances[item['asset']] = {'free': free, 'used': used, 'total': free + used}
    balances.setdefault('USDT', {'free': 0, 'used': 0, 'total': 0})
    result = {'balances': balances, 'balances_at': time.time()}
    if side == 'sell':
        return result
    if not permissions(exchange.sapi_get_account_apirestrictions()):
        raise ValueError('Permission policy unverified')
    quotes = {pair: exchange.fetch_ticker(pair) for pair in PAIRS}
    quote_times = [finite(q['timestamp'], True) / 1000 for q in quotes.values()]
    if not all(0 <= time.time() - stamp <= 15 for stamp in quote_times):
        raise ValueError('Exchange quote timestamp stale or unknown')
    quotes_at = min(quote_times)
    orders = exchange.fetch_open_orders()
    orders_at = time.time()
    if len(orders) >= 1000:
        raise ValueError('Open-order completeness unverified')
    since = int(ledger.get('history_since', started_ms))
    deposits, withdrawals = history(exchange, 'fetch_deposits', since), history(exchange, 'fetch_withdrawals', since)
    # All asset flows are checked: USDT-only history cannot establish absence of non-USDT flows.
    all_deposits = exchange.fetch_deposits(None, since, 1000)
    all_withdrawals = exchange.fetch_withdrawals(None, since, 1000)
    if len(all_deposits) >= 1000 or len(all_withdrawals) >= 1000 or any(t.get('currency') != 'USDT' for t in all_deposits + all_withdrawals):
        raise ValueError('Other-asset or incomplete transfer history')
    with ledger.connect() as db:
        known = {r['pair'] + ':' + str(r['remote_id']) for r in db.execute('SELECT pair,remote_id FROM intents WHERE remote_id IS NOT NULL')}
        recovered = set(ledger.get('recovered_external_orders', [], db))
    external = any(o['symbol'] + ':' + str(o['id']) not in known for o in orders)
    # Audit every supported-pair fill since enrollment with stable Binance IDs.
    fills = []
    for pair in sorted(PAIRS):
        seen = set()
        cursor = None
        for page in range(10):
            batch = exchange.fetch_my_trades(pair, since if cursor is None else None, 100,
                                             {} if cursor is None else {'fromId': cursor})
            for fill in batch:
                if not fill.get('id') or fill['id'] in seen or not str(fill['id']).isdigit():
                    raise ValueError('Fill pagination has no stable progress')
                seen.add(fill['id'])
                fee = fill.get('fee')
                if not fee or fee.get('currency') not in {'USDT', pair.split('/')[0]}:
                    raise ValueError('Unknown fee currency')
                finite(fee['cost'])
                if fill['symbol'] != pair:
                    raise ValueError('Fill symbol differs from requested market')
                if pair + ':' + str(fill.get('order')) not in known | recovered:
                    external = True
                fills.append(fill)
            if len(batch) < 100:
                break
            cursor = int(batch[-1]['id']) + 1
        else:
            raise ValueError('Fill pagination limit reached')
    with ledger.connect() as db:
        for fill in fills:
            import json
            key = fill['symbol'] + ':' + str(fill['id'])
            encoded = json.dumps(fill, sort_keys=True, allow_nan=False)
            old = db.execute('SELECT payload FROM fills WHERE id=?', (key,)).fetchone()
            if old and old[0] != encoded:
                raise ValueError('Authoritative fill changed')
            db.execute('INSERT OR IGNORE INTO fills VALUES(?,?,?)', (key, str(fill['order']), encoded))
    now = time.time()
    for item in deposits + withdrawals + fills:
        timestamp = finite(item['timestamp'], True)
        if timestamp < since or timestamp > time.time() * 1000:
            raise ValueError('History timestamp is outside the verified interval')
    inflows = sum(finite(t['amount']) for t in deposits) - sum(finite(t['amount']) for t in withdrawals)
    baseline = ledger.get('baseline')
    if baseline:
        expected = {'USDT': baseline['equity'] + inflows, 'BTC': 0.0, 'ETH': 0.0}
        for fill in fills:
            base = fill['symbol'].split('/')[0]
            direction = 1 if fill['side'] == 'buy' else -1 if fill['side'] == 'sell' else 0
            if not direction:
                raise ValueError('Unknown fill direction')
            expected[base] += direction * finite(fill['amount'])
            expected['USDT'] -= direction * finite(fill['cost'])
            expected[fill['fee']['currency']] -= finite(fill['fee']['cost'])
        if set(balances) - set(expected) or any(abs(expected[asset] - balances.get(asset, {}).get('total', 0)) > 1e-8 for asset in expected):
            raise ValueError('Balances do not reconcile to initial funding, fills and external flows')
    result.update(quotes=quotes, quotes_at=quotes_at, orders_at=orders_at, open_orders=orders,
                  history_at=now, history_complete=True, external_writer=external,
                  net_inflows=inflows, observed_fill_orders=sorted({f['symbol'] + ':' + str(f['order']) for f in fills}),
                  history_since=since, fills=fills, deposits=deposits, withdrawals=withdrawals)
    return result


def preflight(exchange):
    value = snapshot(exchange)
    flat = not value['open_orders'] and not (set(value['balances']) - {'USDT'})
    if value['external_writer'] or (not ledger.get('deployment') and not flat):
        raise ValueError('Initial dedicated account must be USDT-only and flat; no dust exemption')
    if not ledger.get('baseline') and (value['fills'] or value['deposits'] or value['withdrawals']):
        raise ValueError('Initial funding checkpoint changed during observation; repeat preflight')
    # Bracket the read-only history scan with a second account balance observation.
    final = snapshot(exchange, 'sell')
    if final['balances'] != value['balances']:
        raise ValueError('Account changed during preflight; repeat with a quiet account')
    value['balances_at'] = final['balances_at']
    return {'observed_at': time.time(), 'account': ledger.get('account'), 'snapshot': value,
            'checkpoint_ms': value['history_since'],
            'permission_policy': 'spot-read-IP-only-v1', 'flat': flat}


def reconstruct_daily_risk(value, baseline):
    """Reconstruct UTC midnight exactly when the account held only cash then.

    Overnight asset holdings need an unavailable historical bid valuation, so they
    deliberately remain blocked until a later cash-only midnight is provable.
    The immutable funding vector is never reused as the new day's equity.
    """
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    midnight = int(day.timestamp() * 1000)
    if not baseline or baseline.get('observed_at', float('inf')) * 1000 > midnight:
        raise ValueError('Daily equity checkpoint unavailable before this funding baseline')
    if value['history_complete'] is not True or value['history_since'] > midnight:
        raise ValueError('Complete history through UTC midnight required')
    flows = sum(finite(t['amount']) for t in value['deposits'] if t['timestamp'] < midnight)
    flows -= sum(finite(t['amount']) for t in value['withdrawals'] if t['timestamp'] < midnight)
    balances = {'USDT': baseline['equity'] + flows, 'BTC': 0.0, 'ETH': 0.0}
    for fill in value['fills']:
        if fill['timestamp'] >= midnight:
            continue
        base = fill['symbol'].split('/')[0]
        direction = 1 if fill['side'] == 'buy' else -1
        balances[base] += direction * finite(fill['amount'])
        balances['USDT'] -= direction * finite(fill['cost'])
        balances[fill['fee']['currency']] -= finite(fill['fee']['cost'])
    if any(abs(balances[a]) > 1e-8 for a in ('BTC', 'ETH')):
        raise ValueError('Historical midnight holdings require verified bid valuation; entries remain blocked')
    return {'day': day.strftime('%Y-%m-%d'), 'equity': finite(balances['USDT']),
            'net_inflows': flows, 'observed_at': time.time(), 'partial_day': False,
            'proof': 'complete-funding-fill-flow-replay-cash-only-midnight'}


def reconcile(exchange, engine_db, flat_recovery=False):
    import sqlite3
    snapshot_value = snapshot(exchange)
    with ledger.connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM intents')]
    updates = []
    for row in rows:
        def queue(state, remote_id, filled, managed):
            updates.append((state, remote_id, filled, managed, row['id'],
                            row['state'], row['transport_started'], row['remote_id'],
                            row['filled'], row['managed']))
        if row['transport_started'] == 0 and row['remote_id'] is None and row['filled'] == 0:
            queue('cancelled', None, 0, 1)
            continue
        try:
            remote = exchange.fetch_order(None, row['pair'], {'origClientOrderId': row['id']})
        except Exception as exc:
            import ccxt
            if not (isinstance(exc, ccxt.OrderNotFound) and flat_recovery
                    and not snapshot_value['open_orders'] and not (set(snapshot_value['balances']) - {'USDT'})
                    and row['remote_id'] is None and row['filled'] == 0):
                raise
            queue('cancelled', None, 0, 1)
            continue
        if remote.get('clientOrderId') != row['id'] or str(remote.get('id')) != str(row['remote_id'] or remote.get('id')):
            raise ValueError('Exact order identity mismatch')
        filled = finite(remote['filled'])
        if filled < row['filled']:
            raise ValueError('Fill quantity regressed')
        managed = False
        if engine_db.is_file():
            with sqlite3.connect('file:' + engine_db.as_posix() + '?mode=ro', uri=True) as ft:
                managed = bool(ft.execute('SELECT 1 FROM orders WHERE order_id=? AND ft_pair=?', (str(remote['id']), row['pair'])).fetchone())
        state = {'open': 'partial' if filled else 'submitted', 'closed': 'closed', 'canceled': 'cancelled', 'cancelled': 'cancelled', 'expired': 'cancelled', 'rejected': 'cancelled'}.get(remote['status'], 'unknown')
        if flat_recovery:
            if snapshot_value['open_orders'] or set(snapshot_value['balances']) - {'USDT'} or state not in {'closed', 'cancelled'}:
                raise ValueError('Operator recovery requires all known orders terminal and account exactly flat')
            managed = True
        queue(state, str(remote['id']), filled, int(managed))
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        conflict = False
        for update in updates:
            changed = db.execute('''UPDATE intents SET state=?,remote_id=?,filled=?,managed=?
                WHERE id=? AND state=? AND transport_started=? AND remote_id IS ?
                AND filled=? AND managed=?''', update)
            conflict |= changed.rowcount != 1
        if flat_recovery and not conflict:
            if snapshot_value['open_orders'] or set(snapshot_value['balances']) - {'USDT'}:
                raise ValueError('Recovery requires flat account')
            ledger.put('recovered_external_orders', snapshot_value['observed_fill_orders'], db)
            ledger.put('last_flat_recovery', {'observed_at': time.time(), 'snapshot': snapshot_value}, db)
        unresolved = ledger.uncertain(db)
        blocked = conflict or unresolved or (snapshot_value['external_writer'] and not flat_recovery)
        ledger.put('reconciliation_required', blocked, db)
        if not blocked:
            daily = ledger.get('daily_risk', {}, db)
            if daily.get('day') != time.strftime('%Y-%m-%d', time.gmtime()):
                try:
                    ledger.put('daily_risk', reconstruct_daily_risk(snapshot_value, ledger.get('baseline', db=db)), db)
                    ledger.put('daily_risk_error', None, db)
                except ValueError as exc:
                    ledger.put('daily_risk_error', str(exc), db)
    return not blocked
