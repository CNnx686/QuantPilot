"""Dedicated process entry point. No credentials are serialized into engine config."""
import json
import sys
import time
from . import live_account, live_control, live_store as ledger, settings


def engine():
    import importlib.metadata
    from freqtrade.worker import Worker
    from freqtrade.enums import State, RunMode
    from .live_guard import install_resolver
    from .live_cli import credentials
    if importlib.metadata.version('freqtrade') != '2026.9' or importlib.metadata.version('ccxt') != '4.5.85':
        raise ValueError('Installed engine versions changed')
    record = ledger.get('deployment')
    live_control.verify(record)
    root = ledger.path().parent
    source = settings.ARTIFACTS / record['experiment_id']
    config = json.loads((source / 'effective-config.json').read_text(encoding='utf-8'))
    config.update(dry_run=False, strategy='SmaTrend', strategy_path=str(source / 'strategies'),
                  user_data_dir=root, db_url='sqlite:///' + (root / 'trades.sqlite3').as_posix(),
                  initial_state='paused', runmode=RunMode.LIVE, force_entry_enable=False, position_adjustment_enable=False,
                  cancel_open_orders_on_exit=False, telegram={'enabled': False, 'token': '', 'chat_id': ''})
    config.pop('api_server', None)
    config['exchange'] = {'name': 'binance', 'pair_whitelist': ['BTC/USDT', 'ETH/USDT'], 'pair_blacklist': [],
                          'key': '', 'secret': '', 'ccxt_config': {'timeout': 5000, 'requests_trust_env': True},
                          'ccxt_async_config': {'aiohttp_trust_env': True}}
    config['trading_mode'] = 'spot'
    config['stake_amount'] = min(float(config['stake_amount']), record['limits']['per_order'] / 1.02)
    config['stake_currency'] = 'USDT'
    config['max_open_trades'] = 2
    config['order_types'] = dict(config['order_types'], stoploss_on_exchange=False)
    read_client = live_account.client()
    from .data import sha256
    def evidence_snapshot(side):
        if side == 'buy':
            if sha256(source / 'strategies' / 'SmaTrend.py') != record['evidence']['versions']['strategy_sha256'] or sha256(source / 'effective-config.json') != record['evidence']['versions']['config_sha256']:
                raise ValueError('Frozen execution source changed')
        return live_account.snapshot(read_client, side)
    install_resolver(evidence_snapshot, record['limits'])
    # Inject keys into CCXT instances only after initialization and before Worker sees them.
    from freqtrade.resolvers import ExchangeResolver
    guarded = ExchangeResolver.load_exchange
    def load(*args, **kwargs):
        live_control.verify(ledger.get('deployment'))
        exchange = guarded(*args, **kwargs)
        secret = credentials(record['account']['ref'])
        for name in ('_api', '_api_async'):
            api = getattr(exchange, name, None)
            if api:
                api.apiKey, api.secret = secret['apiKey'], secret['secret']
        return exchange
    ExchangeResolver.load_exchange = staticmethod(load)
    worker = Worker({}, config=config)
    # Entry gate controls buys while Worker remains RUNNING to manage local exits.
    try:
        state = None
        while True:
            control = ledger.get('deployment')
            if control['status'] == 'stopped':
                break
            worker.freqtrade.state = State.RUNNING if control['status'] == 'active' else State.PAUSED
            state = worker._worker(old_state=state)
            # Freqtrade has now had an opportunity to commit Order/Trade. Failure
            # blocks future buys while normal exit management continues.
            try:
                live_account.reconcile(read_client, root / 'trades.sqlite3')
            except Exception:
                ledger.put('reconciliation_required', True)
    finally:
        worker.exit()


def main():
    mode = sys.argv[1]
    if mode == 'engine':
        engine()
        return
    import threading
    import os
    def deadline():
        try:
            ledger.put('read_job', {'status': 'failed', 'kind': mode, 'reason': 'Read deadline exceeded'})
        finally:
            os._exit(2)
    timer = threading.Timer(90, deadline)
    timer.daemon = True
    timer.start()
    try:
        exchange = live_account.client()
        if mode == 'preflight':
            ledger.put('preflight', live_account.preflight(exchange))
        elif mode in {'reconcile', 'recover_flat'}:
            if not live_account.reconcile(exchange, ledger.path().parent / 'trades.sqlite3', mode == 'recover_flat'):
                raise ValueError('Unmanaged remote orders remain')
        else:
            raise ValueError('Unknown read job')
        ledger.put('read_job', {'status': 'succeeded', 'kind': mode, 'completed': time.time()})
    except Exception:
        ledger.put('read_job', {'status': 'failed', 'kind': mode, 'reason': 'Authoritative account evidence incomplete; review account permissions, flatness and journal recovery'})
    finally:
        timer.cancel()


if __name__ == '__main__':
    main()
