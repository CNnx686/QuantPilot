"""Dedicated process entry point. No credentials are serialized into engine config."""
import json
import sys
import time
from . import live_account, live_control, live_store as ledger, settings


def normalized_config(record):
    """Use the installed CLI configuration pipeline on a secret-free live snapshot."""
    import os
    from freqtrade.configuration import Configuration
    from freqtrade.enums import RunMode
    if any(name.startswith('FREQTRADE__') for name in os.environ):
        raise ValueError('Environment overrides are prohibited for live execution')
    root = ledger.path().parent
    source = settings.ARTIFACTS / record['experiment_id']
    config = json.loads((source / 'effective-config.json').read_text(encoding='utf-8'))
    if config.get('freqai', {}).get('enabled') or config.get('freqaimodel'):
        raise ValueError('ML evidence is ineligible for live SMA')
    if set(config['exchange']['pair_whitelist']) != {'BTC/USDT', 'ETH/USDT'}:
        raise ValueError('Live evidence must cover exactly BTC/USDT and ETH/USDT')
    root.mkdir(parents=True, exist_ok=True)
    (root / 'userdata').mkdir(exist_ok=True)
    config.update(dry_run=False, strategy='SmaTrend', strategy_path=str(source / 'strategies'),
                  user_data_dir=str(root / 'userdata'), datadir=str(root / 'market'),
                  db_url='sqlite:///' + (root / 'trades.sqlite3').as_posix(),
                  initial_state='paused', force_entry_enable=False, position_adjustment_enable=False,
                  cancel_open_orders_on_exit=False, telegram={'enabled': False, 'token': '', 'chat_id': ''})
    config.pop('api_server', None)
    config['exchange'] = {'name': 'binance', 'pair_whitelist': ['BTC/USDT', 'ETH/USDT'], 'pair_blacklist': [],
                          'key': '', 'secret': '', 'enable_ws': False,
                          'ccxt_config': {'timeout': 5000, 'requests_trust_env': True,
                            'options': {'defaultType': 'spot', 'fetchMarkets': {'types': ['spot']}}},
                          'ccxt_async_config': {'aiohttp_trust_env': True}}
    config['trading_mode'] = 'spot'
    config['stake_amount'] = min(float(config['stake_amount']), record['limits']['per_order'] / 1.02)
    config['stake_currency'] = 'USDT'
    config['max_open_trades'] = 2
    # The historical config intentionally has no order_types. StrategyResolver loads
    # SmaTrend's tested defaults before the bot validates the normalized config.
    config.pop('order_types', None)
    config.pop('logfile', None)
    target = root / 'engine-config.json'
    temporary = root / 'engine-config.pending.json'
    temporary.write_text(json.dumps(config, allow_nan=False), encoding='utf-8')
    temporary.replace(target)
    args = {'config': [str(target)], 'command': 'trade', 'strategy': 'SmaTrend',
            'strategy_path': str(source / 'strategies'), 'print_colorized': False}
    normalized = Configuration(args, RunMode.LIVE).get_config()
    return normalized, args


def bootstrap(record, read_client):
    """Construct the real Worker with guarded exchange before RPC/strategy startup."""
    import importlib.metadata
    import os
    import psutil
    from freqtrade.worker import Worker
    from freqtrade.exchange import Exchange
    from freqtrade.resolvers import ExchangeResolver
    from .live_guard import install_resolver
    from .live_cli import credentials
    from .data import sha256
    if importlib.metadata.version('freqtrade') != '2026.9' or importlib.metadata.version('ccxt') != '4.5.85':
        raise ValueError('Installed engine versions changed')
    live_control.verify(record)
    config, args = normalized_config(record)
    source = settings.ARTIFACTS / record['experiment_id']
    root = ledger.path().parent
    frozen_config_digest = sha256(root / 'engine-config.json')
    def evidence_snapshot(side):
        if side == 'buy':
            if sha256(source / 'strategies' / 'SmaTrend.py') != record['evidence']['versions']['strategy_sha256'] or sha256(source / 'effective-config.json') != record['evidence']['versions']['config_sha256']:
                raise ValueError('Frozen execution source changed')
        return live_account.snapshot(read_client, side)
    original_resolver = ExchangeResolver.load_exchange
    original_ccxt = Exchange._init_ccxt
    def initialize_ccxt(self, exchange_config, sync, ccxt_kwargs):
        api = original_ccxt(self, exchange_config, sync, ccxt_kwargs)
        # Inject into CCXT objects immediately after construction, before exchange
        # initialization can perform private reads. No engine config holds keys.
        secret = credentials(record['account']['ref'])
        api.apiKey, api.secret = secret['apiKey'], secret['secret']
        return api
    Exchange._init_ccxt = initialize_ccxt
    install_resolver(evidence_snapshot, record['limits'])
    guarded = ExchangeResolver.load_exchange
    def load(*args, **kwargs):
        live_control.verify(ledger.get('deployment'))
        if sha256(root / 'engine-config.json') != frozen_config_digest:
            raise ValueError('Frozen live runtime configuration changed')
        return guarded(*args, **kwargs)
    ExchangeResolver.load_exchange = staticmethod(load)
    def restore():
        ExchangeResolver.load_exchange = staticmethod(original_resolver)
        Exchange._init_ccxt = original_ccxt
    try:
        worker = Worker(args, config=config)
        strategy = worker.freqtrade.strategy
        if (strategy.can_short or strategy.position_adjustment_enable
                or strategy.order_types['stoploss_on_exchange']
                or worker.freqtrade.config['cancel_open_orders_on_exit']
                or not worker.freqtrade.exchange._quantpilot_live_guard):
            worker.exit()
            raise ValueError('Resolved engine policy differs from live SMA policy')
        process = psutil.Process(os.getpid())
        try:
            reconciled = live_account.reconcile(read_client, root / 'trades.sqlite3') is True
        except Exception:
            reconciled = False
            ledger.put('reconciliation_required', True)
        ledger.put('engine_ready', {'deployment_id': record['id'], 'pid': process.pid,
                   'created': process.create_time(), 'engine_version': '2026.9',
                   'strategy_sha256': sha256(source / 'strategies' / 'SmaTrend.py'),
                   'source_config_sha256': sha256(source / 'effective-config.json'),
                   'effective_config_sha256': sha256(root / 'engine-config.json'),
                   'initialized_at': time.time(), 'heartbeat': time.time(), 'status': 'paused',
                   'guard_installed': True, 'reconciled': reconciled})
        return worker, restore
    except BaseException:
        restore()
        raise


def engine():
    from freqtrade.enums import State
    record = ledger.get('deployment')
    if len(sys.argv) < 3 or sys.argv[2] != record['id']:
        raise ValueError('Engine deployment identity is required')
    read_client = live_account.client()
    worker, restore = bootstrap(record, read_client)
    root = ledger.path().parent
    try:
        state = None
        while True:
            control = ledger.get('deployment')
            if control['status'] == 'stopped':
                break
            worker.freqtrade.state = State.RUNNING if control['status'] == 'active' else State.PAUSED
            state = worker._worker(old_state=state)
            ready = ledger.get('engine_ready')
            ready.update(heartbeat=time.time(), status=control['status'])
            ledger.put('engine_ready', ready)
            try:
                if ledger.get('read_job', {}).get('status') != 'running':
                    reconciled = live_account.reconcile(read_client, root / 'trades.sqlite3')
                    ready.update(reconciled=reconciled)
                    ledger.put('engine_ready', ready)
            except Exception:
                ledger.put('reconciliation_required', True)
    finally:
        try:
            worker.exit()
        finally:
            restore()
            ready = ledger.get('engine_ready', {})
            ready.update(status='stopped', heartbeat=time.time())
            ledger.put('engine_ready', ready)


def main():
    mode = sys.argv[1]
    if mode == 'engine':
        engine()
        return
    if len(sys.argv) != 3:
        raise ValueError('Read job identity is required')
    job_id = sys.argv[2]
    import threading
    import os
    def deadline():
        try:
            ledger.finish_read(job_id, 'failed', {'kind': mode, 'reason': 'Read deadline exceeded'})
        finally:
            os._exit(2)
    timer = threading.Timer(90, deadline)
    timer.daemon = True
    timer.start()
    try:
        with ledger.read_scope(job_id):
            exchange = live_account.client()
            if mode == 'preflight':
                ledger.put('preflight', live_account.preflight(exchange))
            elif mode in {'reconcile', 'recover_flat'}:
                if not live_account.reconcile(exchange, ledger.path().parent / 'trades.sqlite3', mode == 'recover_flat'):
                    raise ValueError('Unmanaged remote orders remain')
            else:
                raise ValueError('Unknown read job')
        ledger.finish_read(job_id, 'succeeded', {'kind': mode, 'completed': time.time()})
    except Exception:
        ledger.finish_read(job_id, 'failed', {'kind': mode, 'reason': 'Authoritative account evidence incomplete; review account permissions, flatness and journal recovery'})
    finally:
        timer.cancel()


if __name__ == '__main__':
    main()
