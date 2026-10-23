"""Read-only, standard-library local readiness diagnostics. No engine or RPC calls."""
import argparse
import importlib.metadata
import json
import os
import platform
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from . import settings

VERSION = '0.5.1-restored'


def read_connection(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)


def snapshot(dataset_loader=None):
    if dataset_loader is None:
        from .data import get_dataset
        dataset_loader = get_dataset
    try:
        engine = importlib.metadata.version('freqtrade')
    except importlib.metadata.PackageNotFoundError:
        engine = None
    try:
        manifest = dataset_loader()
        metadata = {k: manifest[k] for k in ('id', 'source', 'exchange', 'pairs', 'timeframe', 'start', 'end',
                    'available_start', 'available_end', 'timezone', 'sha256', 'usage') if k in manifest} if manifest else None
        available = bool(manifest and manifest.get('available', False))
    except (OSError, ValueError, TypeError, KeyError):
        metadata, available = None, False
    counts = dict(experiments=0, research=0, releases=0)
    worker = dict(alive=False, heartbeat_at=None)
    readable = False
    if settings.DB.is_file():
        try:
            with closing(read_connection(settings.DB)) as conn:
                for table in counts:
                    counts[table] = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                row = conn.execute('SELECT heartbeat FROM worker WHERE slot=1').fetchone()
                if row:
                    worker = dict(alive=0 <= time.time() - row[0] < 30,
                                  heartbeat_at=datetime.fromtimestamp(row[0], timezone.utc).isoformat())
                readable = True
        except (sqlite3.Error, ValueError, TypeError, OverflowError, OSError):
            counts = dict(experiments=0, research=0, releases=0)
            worker = dict(alive=False, heartbeat_at=None)
    deployments = dict(live_verified=False, database_readable=False, records=[])
    path = settings.HOME / 'runtime' / 'deployments.sqlite3'
    if path.is_file():
        try:
            with closing(read_connection(path)) as conn:
                records = [json.loads(row[0]) for row in conn.execute('SELECT record FROM deployments ORDER BY rowid DESC')]
            deployments.update(database_readable=True, records=[{
                **{k: row.get(k) for k in ('id', 'release_id', 'created_at')},
                'state': row.get('status') or row.get('state') or 'unknown'} for row in records])
        except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError):
            pass
    # Only boolean configuration metadata leaves this process; no URL/model/key is emitted.
    from .llm import configuration
    llm = dict(configured=all(configuration().values()), optional=True)
    built = (settings.ROOT / 'apps' / 'web' / 'dist' / 'index.html').is_file()
    checks = []
    def check(identifier, ok, detail, action, optional=False):
        checks.append(dict(id=identifier, status='ok' if ok else 'warning' if optional else 'error',
                           detail=detail, action=None if ok else action))
    check('engine', engine == '2026.9', 'Installed engine package metadata only', 'Run setup.ps1 to install pinned dependencies')
    check('frontend', built, 'Local frontend build', 'Run npm run build in apps/web')
    check('dataset', available, 'Local manifest and file existence; hashes not re-audited', 'Prepare the historical dataset explicitly')
    check('database', readable, 'Saved research database readable', 'Start the local service to initialize storage')
    check('worker', worker['alive'], 'Saved worker lease freshness; no process probe', 'Start or restart the local service')
    check('llm', llm['configured'], 'Optional provider configuration only; connectivity untested', 'Configure a provider or use the local template', True)
    check('deployments', deployments['database_readable'], 'Persisted deployment states only; live state not verified', 'Use deployment controls for an explicit live status check', True)
    return dict(version=VERSION, python_version=platform.python_version(), engine_version=engine,
                generated_at=datetime.now(timezone.utc).isoformat(), frontend=dict(built=built),
                dataset=dict(available=available, metadata=metadata), database=dict(exists=settings.DB.is_file(), readable=readable),
                worker=worker, llm=llm, saved_counts=counts, deployments=deployments, checklist=checks,
                core_ready=not any(c['status'] == 'error' for c in checks))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    result = snapshot()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    else:
        print('QuantPilot ' + VERSION)
        for check in result['checklist']:
            print(f"{check['status']}: {check['id']} — {check['detail']}" + (f"; {check['action']}" if check['action'] else ''))
    return 0 if result['core_ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
