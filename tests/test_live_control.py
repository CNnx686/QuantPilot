"""Offline fixture evidence only. Never enrolls or connects an account."""
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from freqtrade.exceptions import DependencyException
from quantpilot import settings, live_store as ledger, live_auth as auth
from quantpilot.live_guard import wrap_exchange, reserve
from quantpilot.live_account import permissions
from quantpilot.live_api import router


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)


def limits():
    return dict(capital=100, exposure=100, per_order=80, daily_loss=10, buffer=.02, max_spread=.01)


def snapshot(side='buy'):
    now = time.time()
    return dict(quotes_at=now, balances_at=now, orders_at=now, history_at=now,
                history_complete=True, external_writer=False, open_orders=[],
                balances={'USDT': dict(free=100, used=0, total=100), 'BTC': dict(free=1, used=0, total=1)},
                quotes={'BTC/USDT': dict(bid=1, ask=1), 'ETH/USDT': dict(bid=1, ask=1)})


def activate():
    ledger.put('deployment', dict(status='active', heartbeat=time.time()))
    ledger.put('baseline', dict(equity=101, day=time.strftime('%Y-%m-%d', time.gmtime())))
    ledger.put('daily_risk', dict(equity=101, net_inflows=0, day=time.strftime('%Y-%m-%d', time.gmtime())))


class Transport:
    def __init__(self):
        self.calls = []
        self.timeout = False

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.calls.append(params)
        if self.timeout:
            raise TimeoutError('secret must not escape')
        return {'id': '123'}


class Exchange:
    _config = {'trading_mode': 'spot', 'dry_run': False}
    markets = {'BTC/USDT': {'limits': {'amount': {'min': .001}, 'cost': {'min': .01}}}}

    def __init__(self):
        self._api = Transport()

    def amount_to_precision(self, pair, amount):
        return amount

    def create_order(self, *, pair, ordertype, side, amount, rate, leverage, time_in_force='GTC', reduceOnly=False, initial_order=True):
        return self._api.create_order(pair, ordertype, side, amount, rate)

    def create_stoploss(self, pair, amount, stop_price, order_types, side, leverage):
        return self._api.create_order(pair, 'stop', side, amount, stop_price)


def buy(exchange, initial=True):
    return exchange.create_order(pair='BTC/USDT', ordertype='limit', side='buy', amount=1,
                                 rate=1, leverage=1, initial_order=initial)


@pytest.mark.parametrize('initial', [True, False])
def test_all_entries_guarded(initial):
    exchange = wrap_exchange(Exchange(), snapshot, limits())
    with pytest.raises(DependencyException):
        buy(exchange, initial)
    assert not exchange._api.calls


def test_timeout_durable_no_retry():
    activate()
    exchange = wrap_exchange(Exchange(), snapshot, limits())
    exchange._api.timeout = True
    with pytest.raises(DependencyException, match='uncertain'):
        buy(exchange)
    restarted = wrap_exchange(Exchange(), snapshot, limits())
    with pytest.raises(DependencyException):
        buy(restarted)
    assert len(exchange._api.calls) == 1
    assert restarted._api.calls == []
    assert exchange._api.calls[0]['newClientOrderId'].startswith('qp')


def test_protective_sell_while_paused():
    exchange = wrap_exchange(Exchange(), snapshot, limits())
    exchange.create_stoploss('BTC/USDT', .5, .9, {}, 'sell', 1)
    assert len(exchange._api.calls) == 1
    with pytest.raises(DependencyException):
        exchange.create_stoploss('BTC/USDT', 2, .9, {}, 'sell', 1)


@pytest.mark.parametrize('change', ['stale', 'nan', 'incomplete'])
def test_invalid_snapshot(change):
    activate()
    value = snapshot()
    if change == 'stale': value['quotes_at'] -= 30
    if change == 'nan': value['balances']['USDT']['total'] = float('nan')
    if change == 'incomplete': value['history_complete'] = False
    exchange = wrap_exchange(Exchange(), lambda side: value, limits())
    with pytest.raises(DependencyException): buy(exchange)
    assert not exchange._api.calls


def test_concurrent_reservation_only_one():
    activate()
    def run(_):
        try:
            reserve('BTC/USDT', 'buy', 60, 1, snapshot(), limits())
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(2) as executor:
        assert sum(executor.map(run, range(2))) == 1


def test_raw_transport_denied():
    exchange = wrap_exchange(Exchange(), snapshot, limits())
    with pytest.raises(DependencyException):
        exchange._api.create_order('BTC/USDT', 'limit', 'buy', 1, 1)
    assert exchange._api.calls == []


def test_permission_unknown_fails():
    assert not permissions({})
    value = dict(enableReading=True, enableSpotAndMarginTrading=True, enableWithdrawals=False,
                 ipRestrict=True, enableInternalTransfer=False, permitsUniversalTransfer=False,
                 enableFutures=False, enableMargin=False)
    assert permissions(value)
    value['enableWithdrawals'] = 'false'
    assert not permissions(value)


def test_auth_origin_csrf_and_idempotency():
    auth.initialize('a very long fixture password')
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, base_url='http://127.0.0.1:8765')
    assert client.get('/api/live/status').json()['auth_configured']
    assert client.get('/api/live/account').status_code == 401
    assert client.post('/api/live/login', json={'password': 'a very long fixture password'}).status_code == 403
    header = {'Origin': auth.ORIGIN}
    response = client.post('/api/live/login', json={'password': 'a very long fixture password'}, headers=header)
    assert 'HttpOnly' in response.headers['set-cookie']
    assert client.get('/api/live/account').status_code == 200
    assert client.post('/api/live/deployment/pause', json={'revision': 1}, headers=header).status_code == 403
    header.update({'X-CSRF-Token': response.json()['csrf_token'], 'Idempotency-Key': 'fixture'})
    assert client.post('/api/live/deployment/pause', json={'revision': 1}, headers=header).status_code == 409
    assert client.post('/api/live/deployment/pause', json={'revision': 2}, headers=header).json()['detail'] == 'Idempotency body conflict'


def test_global_shutdown_refuses():
    from quantpilot.live_control import shutdown_guard
    ledger.put('deployment', {'status': 'paused'})
    with pytest.raises(RuntimeError): shutdown_guard()
    ledger.put('deployment', {'status': 'stopped'})
    shutdown_guard()


@pytest.mark.parametrize('initial', [True, False])
def test_installed_freqtrade_method_uses_guard(initial):
    from freqtrade.exchange import Exchange as InstalledExchange
    from freqtrade.enums import TradingMode
    import types
    activate()
    exchange = Exchange()
    exchange.create_order = types.MethodType(InstalledExchange.create_order, exchange)
    exchange._get_params = lambda *args: {}
    exchange._amount_to_contracts = lambda pair, value: value
    exchange._order_needs_price = lambda *args: True
    exchange.price_to_precision = lambda pair, value: value
    exchange._lev_prep = lambda *args, **kwargs: None
    exchange._log_exchange_response = lambda *args: None
    exchange._order_contracts_to_amount = lambda value: value
    exchange = wrap_exchange(exchange, snapshot, limits())
    assert buy(exchange, initial)['id'] == '123'
    assert len(exchange._api.calls) == 1
    assert exchange._api.calls[0]['newClientOrderId'].startswith('qp')


def test_daily_rollover_does_not_reset():
    activate()
    ledger.put('daily_risk', {'equity': 101, 'net_inflows': 0, 'day': '2000-01-01'})
    with pytest.raises(ValueError, match='Daily baseline'):
        reserve('BTC/USDT', 'buy', 1, 1, snapshot(), limits())


def test_login_rate_limit_persisted():
    from fastapi import HTTPException
    auth.initialize('a very long fixture password')
    for _ in range(5):
        with pytest.raises(HTTPException) as error:
            auth.login('wrong')
        assert error.value.status_code == 401
    with pytest.raises(HTTPException) as error:
        auth.login('a very long fixture password')
    assert error.value.status_code == 429


def test_post_response_database_failure_retains_reservation():
    activate()
    exchange = wrap_exchange(Exchange(), snapshot, limits())
    with ledger.connect() as db:
        db.execute("CREATE TRIGGER refuse_submit BEFORE UPDATE ON intents WHEN NEW.state='submitted' BEGIN SELECT RAISE(FAIL,'fixture persistence failure'); END")
    with pytest.raises(DependencyException): buy(exchange)
    with ledger.connect() as db:
        row = db.execute('SELECT * FROM intents').fetchone()
        assert row['state'] == 'unknown'
        assert row['reserve'] > 0
    with pytest.raises(DependencyException): buy(exchange)
    assert len(exchange._api.calls) == 1


class ReadFixture:
    def __init__(self):
        self.flat = False
        self.transfer = False

    def private_get_account(self):
        usdt = (100.98 if self.flat else 89.99) + (1 if self.transfer else 0)
        return {'canTrade': True, 'accountType': 'SPOT', 'balances': [
            {'asset': 'USDT', 'free': str(usdt), 'locked': '0'},
            {'asset': 'BTC', 'free': '0' if self.flat else '1', 'locked': '0'}]}

    def sapi_get_account_apirestrictions(self):
        return dict(enableReading=True, enableSpotAndMarginTrading=True, enableWithdrawals=False,
                    ipRestrict=True, enableInternalTransfer=False, permitsUniversalTransfer=False,
                    enableFutures=False, enableMargin=False)

    def fetch_ticker(self, pair):
        return {'bid': 10, 'ask': 10, 'timestamp': time.time() * 1000}

    def fetch_open_orders(self): return []
    def fetch_deposits(self, *args): return []
    def fetch_withdrawals(self, *args): return []

    def fetch_my_trades(self, pair, *args):
        if pair != 'BTC/USDT': return []
        result = [dict(id='1', order='123', symbol=pair, amount=1, cost=10, side='buy', timestamp=1000, fee=dict(currency='USDT', cost=.01))]
        if self.flat:
            result.append(dict(id='2', order='operator-exit', symbol=pair, amount=1, cost=11, side='sell', timestamp=2000, fee=dict(currency='USDT', cost=.01)))
        return result

    def fetch_order(self, identifier, pair, params):
        assert identifier is None and params == {'origClientOrderId': 'qpfixture'}
        return dict(id='123', clientOrderId='qpfixture', filled=1, status='canceled')


def test_partial_orphan_operator_flat_recovery(tmp_path):
    from quantpilot.live_account import reconcile, snapshot as account_snapshot
    ledger.put('baseline', {'equity': 100, 'day': '2000-01-01'})
    ledger.put('history_since', 1)
    with ledger.connect() as db:
        db.execute("INSERT INTO intents(id,pair,side,amount,reserve,state,remote_id,created) VALUES('qpfixture','BTC/USDT','buy',2,20,'unknown','123',1)")
    exchange = ReadFixture()
    assert reconcile(exchange, tmp_path / 'no-engine.sqlite3') is False
    with ledger.connect() as db:
        row = db.execute('SELECT * FROM intents').fetchone()
        assert row['state'] == 'cancelled' and row['filled'] == 1 and not row['managed']
    exchange.flat = True
    assert reconcile(exchange, tmp_path / 'no-engine.sqlite3', True) is True
    assert ledger.get('baseline')['equity'] == 100
    assert account_snapshot(exchange)['external_writer'] is False
    with ledger.connect() as db:
        assert db.execute('SELECT count(*) FROM fills').fetchone()[0] == 2
    exchange.transfer = True
    with pytest.raises(ValueError, match='Balances do not reconcile'):
        account_snapshot(exchange)


def test_daily_profit_not_reused_to_hide_current_day_loss():
    from datetime import datetime, timezone
    from quantpilot.live_account import reconstruct_daily_risk
    midnight = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    prior = [dict(symbol='BTC/USDT', side='buy', amount=1, cost=10, timestamp=midnight-2000, fee={'currency': 'USDT', 'cost': 0}),
             dict(symbol='BTC/USDT', side='sell', amount=1, cost=110, timestamp=midnight-1000, fee={'currency': 'USDT', 'cost': 0})]
    value = dict(history_complete=True, history_since=midnight-10000, deposits=[], withdrawals=[], fills=prior)
    daily = reconstruct_daily_risk(value, {'equity': 100, 'observed_at': (midnight-10000)/1000})
    assert daily['equity'] == 200 and daily['partial_day'] is False
    activate()
    ledger.put('daily_risk', daily)
    current = snapshot()
    current['balances'] = {'USDT': {'free': 120, 'used': 0, 'total': 120}}
    with pytest.raises(ValueError, match='Daily loss'):
        reserve('BTC/USDT', 'buy', 1, 1, current, limits())
    value['fills'] = prior[:1]
    with pytest.raises(ValueError, match='midnight holdings'):
        reconstruct_daily_risk(value, {'equity': 100, 'observed_at': (midnight-10000)/1000})


def test_proven_no_send_does_not_require_remote_lookup(tmp_path):
    from quantpilot.live_account import reconcile
    ledger.put('history_since', 1)
    ledger.put('baseline', {'equity': 100, 'observed_at': 0})
    with ledger.connect() as db:
        db.execute("INSERT INTO intents(id,pair,side,amount,reserve,state,created,transport_started) VALUES('nosend','BTC/USDT','buy',1,1,'intent',1,0)")
    exchange = ReadFixture()
    exchange.flat = True
    # Clean, funded account with no transport call or fills.
    exchange.private_get_account = lambda: {'canTrade': True, 'accountType': 'SPOT', 'balances': [{'asset': 'USDT', 'free': '100', 'locked': '0'}]}
    exchange.fetch_my_trades = lambda *a: []
    exchange.fetch_order = lambda *a: pytest.fail('No-send intent must not query remote order')
    assert reconcile(exchange, tmp_path/'no-engine.sqlite3')
    with ledger.connect() as db:
        row = db.execute('SELECT * FROM intents').fetchone()
        assert row['state'] == 'cancelled' and row['managed'] == 1


@pytest.mark.parametrize('change', ['reconciled', 'paused', 'account_changed', 'read_job', 'reconciliation_required'])
def test_admission_rechecked_atomically_at_transport(change):
    from quantpilot.live_guard import scope
    activate()
    class InterleavedExchange(Exchange):
        def create_order(self, *, pair, ordertype, side, amount, rate, leverage,
                         time_in_force='GTC', reduceOnly=False, initial_order=True):
            identifier = scope.get()['id']
            if change == 'reconciled':
                with ledger.connect() as db:
                    db.execute("UPDATE intents SET state='cancelled',managed=1 WHERE id=?", (identifier,))
            elif change == 'paused':
                ledger.put('deployment', dict(status='paused', heartbeat=time.time()))
            elif change == 'account_changed':
                ledger.put('account', {'ref': 'changed'})
            elif change == 'read_job':
                ledger.put('read_job', {'status': 'running'})
            else:
                ledger.put('reconciliation_required', True)
            return self._api.create_order(pair, ordertype, side, amount, rate)
    exchange = wrap_exchange(InterleavedExchange(), snapshot, limits())
    with pytest.raises(DependencyException): buy(exchange)
    assert exchange._api.calls == []
    with ledger.connect() as db:
        intent = db.execute('SELECT * FROM intents').fetchone()
        assert intent['transport_started'] == 0
        assert intent['state'] == 'cancelled'


def test_dispatch_marker_cannot_be_released_as_no_send():
    from quantpilot.live_guard import scope
    activate()
    class ClaimedExchange(Exchange):
        def create_order(self, *, pair, ordertype, side, amount, rate, leverage,
                         time_in_force='GTC', reduceOnly=False, initial_order=True):
            with ledger.connect() as db:
                db.execute("UPDATE intents SET transport_started=1,state='unknown' WHERE id=?", (scope.get()['id'],))
            return self._api.create_order(pair, ordertype, side, amount, rate)
    exchange = wrap_exchange(ClaimedExchange(), snapshot, limits())
    with pytest.raises(DependencyException): buy(exchange)
    assert exchange._api.calls == []
    with ledger.connect() as db:
        intent = db.execute('SELECT * FROM intents').fetchone()
        assert intent['transport_started'] == 1 and intent['state'] == 'unknown'
        assert intent['reserve'] > 0


def test_each_quote_timestamp_rejects_hidden_future():
    from quantpilot.live_account import snapshot as account_snapshot
    exchange = ReadFixture()
    exchange.fetch_ticker = lambda pair: {'bid': 10, 'ask': 10,
        'timestamp': (time.time() + (60 if pair == 'ETH/USDT' else -1)) * 1000}
    with pytest.raises(ValueError, match='quote timestamp'):
        account_snapshot(exchange)


def test_denial_diagnostics_are_fixed_codes_not_external_errors():
    activate()
    secret = 'fixture-private-key-DO-NOT-STORE'
    def broken_snapshot(side):
        raise RuntimeError(secret)
    exchange = wrap_exchange(Exchange(), broken_snapshot, limits())
    with pytest.raises(DependencyException): buy(exchange)
    denial = ledger.get('last_order_denial')
    assert denial['code'] == 'account_evidence_unavailable'
    assert secret not in str(denial)
    exchange = wrap_exchange(Exchange(), snapshot, limits())
    exchange._api.timeout = True
    with pytest.raises(DependencyException): buy(exchange)
    assert ledger.get('last_order_denial')['code'] == 'submission_unknown'


def test_order_identifiers_are_scoped_to_symbol():
    from quantpilot.live_account import snapshot as account_snapshot
    ledger.put('history_since', 1)
    with ledger.connect() as db:
        db.execute("INSERT INTO intents(id,pair,side,amount,reserve,state,remote_id,created,managed) VALUES('known-btc','BTC/USDT','buy',1,10,'closed','123',1,1)")
    exchange = ReadFixture()
    exchange.fetch_my_trades = lambda pair, *args: [dict(id='1', order='123', symbol=pair,
        amount=1, cost=10, side='buy', timestamp=1000, fee=dict(currency='USDT', cost=.01))] if pair == 'ETH/USDT' else []
    value = account_snapshot(exchange)
    assert value['external_writer'] is True
    assert value['observed_fill_orders'] == ['ETH/USDT:123']


@pytest.mark.parametrize('terminal', ['expired', 'rejected'])
def test_terminal_partial_order_can_be_flat_recovered(tmp_path, terminal):
    from quantpilot.live_account import reconcile
    ledger.put('history_since', 1)
    ledger.put('baseline', {'equity': 100, 'observed_at': 0})
    with ledger.connect() as db:
        db.execute("INSERT INTO intents(id,pair,side,amount,reserve,state,remote_id,created) VALUES('qpfixture','BTC/USDT','buy',2,20,'unknown','123',1)")
    exchange = ReadFixture()
    exchange.flat = True
    exchange.fetch_order = lambda *args: {'id': '123', 'clientOrderId': 'qpfixture', 'filled': 1, 'status': terminal}
    assert reconcile(exchange, tmp_path/'no-engine.sqlite3', True)
    with ledger.connect() as db:
        row = db.execute('SELECT * FROM intents').fetchone()
        assert row['state'] == 'cancelled' and row['filled'] == 1 and row['managed'] == 1


def test_open_orders_with_same_numeric_id_on_two_pairs_are_distinct():
    from quantpilot.live_guard import validate_snapshot
    value = snapshot()
    value['open_orders'] = [dict(id='123', symbol=pair, side='buy', remaining=10, price=1)
                            for pair in ('BTC/USDT', 'ETH/USDT')]
    cost, equity, exposure = validate_snapshot(value, 'BTC/USDT', 'buy', 1, 1, limits())
    assert exposure == 21


@pytest.mark.parametrize('concurrent_change', ['dispatch', 'partial_fill'])
def test_reconciliation_cas_preserves_newer_order_state(tmp_path, monkeypatch, concurrent_change):
    from quantpilot import live_account
    first_state = 'intent' if concurrent_change == 'dispatch' else 'partial'
    first_remote = None if concurrent_change == 'dispatch' else 'first-remote'
    first_transport = 0 if concurrent_change == 'dispatch' else 1
    first_filled = 0 if concurrent_change == 'dispatch' else .25
    with ledger.connect() as db:
        db.execute('''INSERT INTO intents(id,pair,side,amount,reserve,state,remote_id,created,transport_started,filled)
                      VALUES('first','BTC/USDT','sell',1,1,?,?,1,?,?)''',
                   (first_state, first_remote, first_transport, first_filled))
        db.execute("INSERT INTO intents(id,pair,side,amount,reserve,state,remote_id,created,transport_started) VALUES('second','ETH/USDT','sell',1,1,'submitted','second-remote',2,1)")
    monkeypatch.setattr(live_account, 'snapshot', lambda *args: {
        'external_writer': False, 'open_orders': [], 'balances': {'USDT': {'total': 100}}})
    class InterleavedRead:
        def fetch_order(self, identifier, pair, params):
            intent = params['origClientOrderId']
            if intent == 'second':
                with ledger.connect() as db:
                    if concurrent_change == 'dispatch':
                        db.execute("UPDATE intents SET transport_started=1,state='submitted',remote_id='accepted-first' WHERE id='first'")
                    else:
                        db.execute("UPDATE intents SET state='partial',filled=.75,managed=1 WHERE id='first'")
            return {'id': intent + '-remote', 'clientOrderId': intent,
                    'filled': .25 if intent == 'first' else 0, 'status': 'open'}
    assert live_account.reconcile(InterleavedRead(), tmp_path/'no-engine.sqlite3') is False
    assert ledger.get('reconciliation_required') is True
    with ledger.connect() as db:
        first = db.execute("SELECT * FROM intents WHERE id='first'").fetchone()
        if concurrent_change == 'dispatch':
            assert first['transport_started'] == 1 and first['state'] == 'submitted'
            assert first['remote_id'] == 'accepted-first' and first['managed'] == 0
        else:
            assert first['filled'] == .75 and first['managed'] == 1
        assert first['reserve'] == 1
