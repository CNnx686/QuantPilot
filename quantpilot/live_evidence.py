"""Independent live admission; teaching release status is never an authorization."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from . import settings, store, releases, deployment_store
from .data import sha256

POLICY = {'id': 'live-sma-v1', 'min_trades': 30, 'min_days': 60, 'max_drawdown': .10,
          'min_lookahead_signals': 20, 'dryrun_hours': 24, 'dryrun_closed_trades': 1}


def audit(experiment_id, dryrun_id):
    if any(releases.POLICY[k] != POLICY[k] for k in ('min_trades', 'min_days', 'max_drawdown', 'min_lookahead_signals')):
        raise ValueError('Historical audit policy changed; live-sma-v1 requires review')
    job = store.get(experiment_id)
    if not job or job['config']['strategy'] != 'SmaTrend':
        raise ValueError('Only SmaTrend historical evidence is eligible')
    if set(job['config']['pairs']) != {'BTC/USDT', 'ETH/USDT'}:
        raise ValueError('Live SMA requires historical and dry-run evidence for both BTC/USDT and ETH/USDT')
    historical = releases.audit(job)
    if any(c['status'] != 'passed' for c in historical['checks'] if c['required']):
        raise ValueError('Historical integrity or diagnostic evidence incomplete')
    with deployment_store.connect() as db:
        dry = deployment_store.get(db, dryrun_id)
    if dry['experiment_id'] != experiment_id or dry['versions']['base_strategy_sha256'] != historical['versions']['strategy_sha256']:
        raise ValueError('Dry-run strategy does not match historical evidence')
    root = settings.HOME / 'runtime' / 'deployments' / dryrun_id
    if sha256(root / 'strategies' / 'SmaTrend.py') != historical['versions']['strategy_sha256']:
        raise ValueError('Dry-run strategy file changed')
    for name, digest in [('strategies/QuantPilotSmaDryRun.py', 'wrapper_sha256'), ('source-config.json', 'source_config_sha256'), ('engine-config.json', 'effective_config_sha256')]:
        if sha256(root / name) != dry['versions'][digest]:
            raise ValueError('Dry-run frozen configuration changed')
    config = json.loads((root / 'engine-config.json').read_text(encoding='utf-8'))
    if config.get('dry_run') is not True or config.get('strategy') != 'QuantPilotSmaDryRun':
        raise ValueError('Expected actual dry-run engine configuration')
    decisions = [json.loads(line) for line in (root / 'decisions.jsonl').read_text(encoding='utf-8').splitlines()]
    by_pair = {}
    for decision in decisions:
        if decision.get('versions') != dry['versions']:
            raise ValueError('Dry-run decision provenance changed')
        available = datetime.fromisoformat(decision['available_at'])
        observed = datetime.fromisoformat(decision['observed_at'])
        if decision['signal_valid'] and available <= observed <= available + timedelta(minutes=10):
            by_pair.setdefault(decision['pair'], set()).add(available)
    for pair in job['config']['pairs']:
        hours = by_pair.get(pair, set())
        if not any(all(hour + timedelta(hours=n) in hours for n in range(24)) for hour in hours):
            raise ValueError('24 consecutive timely hourly decisions per pair required')
    engine_db = root / 'trades.sqlite3'
    with sqlite3.connect('file:' + engine_db.as_posix() + '?mode=ro', uri=True) as db:
        count = db.execute("SELECT count(*) FROM trades WHERE is_open=0 AND strategy='QuantPilotSmaDryRun' AND close_date IS NOT NULL AND pair IN ('BTC/USDT','ETH/USDT')").fetchone()[0]
        if count < 1:
            raise ValueError('At least one actual closed engine dry-run trade required')
    return {'policy': POLICY, 'versions': historical['versions'], 'dryrun_id': dryrun_id,
            'decision_sha256': sha256(root / 'decisions.jsonl'), 'closed_trades': count}
