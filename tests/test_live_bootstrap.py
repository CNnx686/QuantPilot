"""Real installed Worker/Exchange classes, hermetic fake CCXT transport only."""
import json
import shutil
import time
from pathlib import Path
import pytest
from quantpilot import settings, live_store as ledger, live_runner, live_control, live_cli
from quantpilot.engine import engine_config
from quantpilot.data import sha256


@pytest.fixture
def worker_fixture(tmp_path, monkeypatch):
    import ccxt
    from freqtrade.exchange import Exchange
    from freqtrade.rpc import RPCManager
    from freqtrade.strategy import IStrategy
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(settings, 'ARTIFACTS', tmp_path / 'artifacts')
    source = settings.ARTIFACTS / 'fixture'
    (source / 'strategies').mkdir(parents=True)
    shutil.copy2(settings.ROOT / 'strategies/SmaTrend.py', source / 'strategies/SmaTrend.py')
    raw = engine_config({'pairs': ['BTC/USDT', 'ETH/USDT'], 'stake': 10, 'wallet': 100, 'fee': .001}, source)
    assert 'order_types' not in raw
    (source / 'effective-config.json').write_text(json.dumps(raw))
    record = {'id': 'fixture', 'experiment_id': 'fixture', 'account': {'ref': 'fixture'},
              'status': 'paused', 'limits': dict(capital=100, exposure=100, per_order=20, daily_loss=10, buffer=.02, max_spread=.01),
              'evidence': {'versions': {'strategy_sha256': sha256(source / 'strategies/SmaTrend.py'),
                                       'config_sha256': sha256(source / 'effective-config.json')}}}
    ledger.put('deployment', record)
    monkeypatch.setattr(live_control, 'verify', lambda record: None)
    from quantpilot import live_account
    monkeypatch.setattr(live_account, 'reconcile', lambda *a, **k: True)
    monkeypatch.setattr(live_cli, 'credentials', lambda reference: {'apiKey': 'FIXTURE_KEY', 'secret': 'FIXTURE_SECRET'})
    # Any unmocked sync or asynchronous network attempt is a test failure.
    def no_network(*args, **kwargs):
        raise AssertionError('No network is permitted in bootstrap fixture')
    monkeypatch.setattr(ccxt.binance, 'request', no_network)
    import ccxt.pro
    monkeypatch.setattr(ccxt.pro.binance, 'request', no_network)
    markets = {}
    for pair in ('BTC/USDT', 'ETH/USDT'):
        markets[pair] = dict(id=pair.replace('/', ''), symbol=pair, base=pair.split('/')[0], quote='USDT',
            active=True, spot=True, type='spot', swap=False, future=False, contract=False,
            precision={'amount': .00001, 'price': .01}, limits={'amount': {'min': .00001, 'max': 10000},
              'cost': {'min': 1, 'max': None}, 'price': {'min': .01, 'max': None}},
            maker=.001, taker=.001, info={})
    def reload(self, *args, **kwargs):
        self._markets = markets
        self._api.set_markets(markets)
        self._api_async.set_markets(markets)
    monkeypatch.setattr(Exchange, 'reload_markets', reload)
    monkeypatch.setattr(ccxt.binance, 'fetch_time', lambda self: int(time.time() * 1000))
    events = []
    def submit(self, symbol, type, side, amount, price=None, params=None):
        events.append(('transport', params))
        if getattr(self, '_fixture_timeout', False):
            raise ccxt.RequestTimeout('fixture timeout')
        return dict(id='fixture-order', symbol=symbol, type=type, side=side, amount=amount,
                    price=price, filled=0, remaining=amount, status='open')
    monkeypatch.setattr(ccxt.binance, 'create_order', submit)
    def balance(self, *args, **kwargs):
        assert self.apiKey == 'FIXTURE_KEY' and self.secret == 'FIXTURE_SECRET'
        events.append('credential_before_private_read')
        return {'USDT': {'free': 100, 'used': 0, 'total': 100}, 'free': {'USDT': 100}, 'used': {'USDT': 0}, 'total': {'USDT': 100}}
    monkeypatch.setattr(ccxt.binance, 'fetch_balance', balance)
    original_rpc = RPCManager.__init__
    def rpc_init(self, bot):
        assert bot.exchange._quantpilot_live_guard
        assert bot.exchange._api.apiKey == 'FIXTURE_KEY'
        events.append('guard_before_rpc')
        original_rpc(self, bot)
    monkeypatch.setattr(RPCManager, '__init__', rpc_init)
    original_start = IStrategy.ft_bot_start
    def bot_start(self, **kwargs):
        assert self.dp._exchange._quantpilot_live_guard
        events.append('guard_before_bot_start')
        original_start(self, **kwargs)
    monkeypatch.setattr(IStrategy, 'ft_bot_start', bot_start)
    worker, restore = live_runner.bootstrap(record, object())
    try:
        yield worker, record, events
    finally:
        worker.exit()
        restore()


def test_real_worker_boots_from_historical_shape(worker_fixture):
    from freqtrade.enums import State, RunMode
    worker, record, events = worker_fixture
    assert worker.freqtrade.state == State.PAUSED
    assert worker.freqtrade.config['runmode'] == RunMode.LIVE
    assert isinstance(worker.freqtrade.config['user_data_dir'], Path)
    assert worker.freqtrade.strategy.order_types['stoploss_on_exchange'] is False
    assert 'credential_before_private_read' in events
    assert 'guard_before_rpc' in events and 'guard_before_bot_start' in events
    ready = ledger.get('engine_ready')
    assert ready['deployment_id'] == record['id'] and ready['guard_installed']
    assert worker.freqtrade.config['exchange'].get('key') in ('', None)
    assert 'FIXTURE_SECRET' not in (ledger.path().parent / 'engine-config.json').read_text()


@pytest.mark.parametrize('mode', ['initial', 'replace', 'pos_adjust'])
def test_installed_bot_entry_modes_guard_callback_exception(worker_fixture, monkeypatch, mode):
    from freqtrade.exceptions import DependencyException
    from quantpilot import live_account
    worker, record, events = worker_fixture
    bot = worker.freqtrade
    monkeypatch.setattr(bot, 'get_valid_enter_price_and_stake', lambda *a: (10, 10, 1))
    called = []
    def broken_confirmation(**kwargs):
        called.append('callback')
        raise RuntimeError('fixture callback failure')
    monkeypatch.setattr(bot.strategy, 'confirm_trade_entry', broken_confirmation)
    def read(side):
        called.append('guard')
        raise ValueError('fixture denies account evidence')
    monkeypatch.setattr(live_account, 'snapshot', lambda client, side: read(side))
    with pytest.raises(DependencyException):
        bot.execute_entry('BTC/USDT', 10, price=10, mode=mode)
    assert 'guard' in called
    assert ('callback' in called) == (mode == 'initial')
    assert not any(isinstance(e, tuple) and e[0] == 'transport' for e in events)


def test_installed_stoploss_decorator_is_guarded(worker_fixture, monkeypatch):
    from quantpilot import live_account
    worker, record, events = worker_fixture
    monkeypatch.setattr(live_account, 'snapshot', lambda *a: {
        'balances_at': time.time(), 'balances': {'BTC': {'free': 1, 'used': 0, 'total': 1}}})
    order = worker.freqtrade.exchange.create_stoploss('BTC/USDT', .5, 10, {'stoploss': 'limit'}, 'sell', 1)
    calls = [e for e in events if isinstance(e, tuple) and e[0] == 'transport']
    assert order['id'] == 'fixture-order' and len(calls) == 1
    assert calls[0][1]['newClientOrderId'].startswith('qp')


def test_installed_worker_reconfigure_reinstalls_guard(worker_fixture):
    worker, record, events = worker_fixture
    worker._reconfigure()
    assert events.count('guard_before_rpc') == 2
    assert events.count('guard_before_bot_start') == 2
    assert worker.freqtrade.exchange._quantpilot_live_guard


def test_installed_stoploss_timeout_no_blind_retry(worker_fixture, monkeypatch):
    from quantpilot import live_account
    from freqtrade.exceptions import DependencyException
    worker, record, events = worker_fixture
    monkeypatch.setattr(live_account, 'snapshot', lambda *a: {
        'balances_at': time.time(), 'balances': {'BTC': {'free': 1, 'used': 0, 'total': 1}}})
    exchange = worker.freqtrade.exchange
    exchange._api._fixture_timeout = True
    for _ in range(2):
        with pytest.raises(DependencyException):
            exchange.create_stoploss('BTC/USDT', .5, 10, {'stoploss': 'limit'}, 'sell', 1)
    assert len([e for e in events if isinstance(e, tuple) and e[0] == 'transport']) == 1
