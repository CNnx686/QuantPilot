"""Owned, isolated Freqtrade dry-run lifecycle. No exchange account or forced order actions."""
import importlib.metadata
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil

from . import deployment_rpc as rpc, deployment_store as db, settings, store
from .data import sha256
from .engine import write_json
from .releases import verify_release, safe_file

TERMINAL = {'stopped', 'failed'}


def child_environment():
    # Preserve public-market proxy configuration; do not propagate application/model credentials.
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith(('FREQTRADE__', 'QUANTPILOT_LLM', 'OPENAI_', 'DEEPSEEK_', 'ANTHROPIC_', 'GEMINI_'))
            and not any(s in k.upper() for s in ('API_KEY', 'API_SECRET'))} | {'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}


def directory(record):
    return settings.HOME / 'runtime' / 'deployments' / record['id']


def owned(record):
    identity = record.get('process')
    if not identity:
        return None

    try:
        process = psutil.Process(identity['pid'])
        if (abs(process.create_time() - identity['created']) <= .05
                and Path(process.exe()).resolve() == Path(identity['exe']).resolve()
                and process.cmdline() == identity['cmdline']):
            return process
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
        pass
    return None


def stop_owned(record):
    """Windows venv python is a redirector: terminate its verified descendant tree too."""
    process = owned(record)
    identities = []
    if process:
        for descendant in process.children(recursive=True):
            try:
                identities.append({'process': {'pid': descendant.pid, 'created': descendant.create_time(),
                                               'exe': descendant.exe(), 'cmdline': descendant.cmdline()}})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        identities.append(record)
    for identity in reversed(identities[:-1]):
        target = owned(identity)
        if target:
            try:
                target.terminate()
            except psutil.NoSuchProcess:
                pass
    target = owned(record)
    if target:
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            pass
    for identity in identities:
        target = owned(identity)
        if target:
            try:
                target.wait(timeout=5)
            except psutil.TimeoutExpired:
                target = owned(identity)
                if target:
                    target.kill()
                    target.wait(timeout=5)


def public(record):
    fields = ('id', 'release_id', 'experiment_id', 'mode', 'status', 'ready', 'created_at',
              'updated_at', 'error', 'versions', 'snapshot', 'snapshot_stale')
    result = {k: record.get(k) for k in fields}
    result['decisions'] = []
    try:
        # Bounded file tail keeps long-running journals cheap to poll.
        with (directory(record) / 'decisions.jsonl').open('rb') as stream:
            stream.seek(max(0, stream.seek(0, 2) - 128_000))
            lines = stream.read().decode('utf-8', errors='replace').splitlines()
        for line in lines[-100:]:
            try:
                result['decisions'].append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return result


def reconcile(record):
    if record['status'] in TERMINAL:
        return record
    process = owned(record)
    record.update(ready=False, snapshot_stale=True, updated_at=store.now())
    if process is None:
        if not record.get('process') and time.time() - record['launch_at'] < 30:
            record.update(status='starting', error='Engine launch is being recorded')
            return record
        record.update(status='failed', error='Owned engine process is no longer available')
        return record
    try:
        config = rpc.call(record['rpc'], 'show_config')
        health = rpc.call(record['rpc'], 'health')
        if not rpc.ready(config, health):
            record.update(status='starting' if record.get('snapshot') is None and time.time() - record['launch_at'] < 180 else 'unavailable',
                          error='Waiting for verified dry-run configuration and a fresh successful processing loop')
            return record
        snapshot = {'observed_at': store.now(), 'show_config': config, 'health': health,
                    'balance': rpc.call(record['rpc'], 'balance'), 'positions': rpc.call(record['rpc'], 'status'),
                    'trades': rpc.call(record['rpc'], 'trades')}
        record.update(status=config['state'], ready=True, error=None, snapshot=snapshot, snapshot_stale=False)
    except rpc.Unavailable:
        record.update(status='starting' if record.get('snapshot') is None and time.time() - record['launch_at'] < 180 else 'unavailable',
                      error='Engine RPC unavailable; saved snapshot is stale')
    return record


def prepare(record, release, job):
    root = directory(record)
    root.mkdir(parents=True, exist_ok=False)
    (root / 'strategies').mkdir()
    (root / 'userdata').mkdir()
    source = settings.ARTIFACTS / job['id']
    for relative, expected in [('strategies/SmaTrend.py', release['versions']['strategy_sha256']),
                               ('effective-config.json', release['versions']['config_sha256'])]:
        target = root / (relative if relative.startswith('strategies/') else 'source-config.json')
        shutil.copy2(safe_file(source, relative), target)
        if sha256(target) != expected:
            raise ValueError('Frozen deployment source hash mismatch')
    wrapper = root / 'strategies/QuantPilotSmaDryRun.py'
    shutil.copy2(settings.ROOT / 'strategies/QuantPilotSmaDryRun.py', wrapper)
    effective = json.loads((root / 'source-config.json').read_text(encoding='utf-8'))
    if effective.get('freqai', {}).get('enabled') or effective.get('freqaimodel'):
        raise ValueError('ML deployments are not supported')
    effective.update(dry_run=True, initial_state='paused', force_entry_enable=False,
                     strategy='QuantPilotSmaDryRun', strategy_path=str(root / 'strategies'),
                     user_data_dir=str(root / 'userdata'), datadir=str(root / 'market'),
                     db_url='sqlite:///' + (root / 'trades.sqlite3').as_posix())
    effective['exchange'] = {k: v for k, v in effective['exchange'].items()
                             if k in {'name', 'pair_whitelist', 'pair_blacklist', 'ccxt_config'}}
    effective['exchange'].update(key='', secret='')
    # Only public market transport options are allowed, including inside CCXT config.
    transport = effective['exchange'].get('ccxt_config', {})
    effective['exchange']['ccxt_config'] = {
        'enableRateLimit': True, 'timeout': transport.get('timeout', 15000),
        'requests_trust_env': True, 'aiohttp_trust_env': True,
        'options': {'defaultType': 'spot', 'fetchMarkets': {'types': ['spot']}}}
    effective['telegram'] = {'enabled': False, 'token': '', 'chat_id': ''}
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    record['rpc'] = {'port': port, 'username': secrets.token_hex(16), 'password': secrets.token_urlsafe(32)}
    effective['api_server'] = {'enabled': True, 'listen_ip_address': '127.0.0.1', 'listen_port': port,
                               'verbosity': 'error', 'enable_openapi': False, 'CORS_origins': [],
                               'username': record['rpc']['username'], 'password': record['rpc']['password'],
                               'jwt_secret_key': secrets.token_urlsafe(32), 'ws_token': secrets.token_urlsafe(32)}
    write_json(root / 'engine-config.json', effective)
    record['versions'] = {'engine_version': importlib.metadata.version('freqtrade'),
                          'base_strategy_sha256': sha256(root / 'strategies/SmaTrend.py'),
                          'wrapper_sha256': sha256(wrapper), 'source_config_sha256': sha256(root / 'source-config.json'),
                          'effective_config_sha256': sha256(root / 'engine-config.json'),
                          'adapter_sha256': sha256(Path(__file__)), 'release_id': release['id']}
    write_json(root / 'snapshot.json', record['versions'])
    return [sys.executable, '-m', 'freqtrade', 'trade', '--config', str(root / 'engine-config.json'),
            '--strategy', 'QuantPilotSmaDryRun', '--strategy-path', str(root / 'strategies'), '--no-color']


def create(release_id, token):
    # Serialize preparation/launch so concurrent clicks never create two processes.
    with db.connect() as conn:
        previous = conn.execute('SELECT record FROM deployments WHERE token=?', (token,)).fetchone()
        if previous:
            record = json.loads(previous['record'])
            if record['release_id'] != release_id:
                raise ValueError('Idempotency-Key already belongs to a different deployment request')
            recover(record)
            reconcile(record)
            db.save(conn, record)
            return public(record), False
        release, job = verify_release(release_id)
        if job['config'].get('strategy') != 'SmaTrend':
            raise ValueError('Only SmaTrend teaching simulation is supported')
        version = importlib.metadata.version('freqtrade')
        if version != '2026.9' or release['versions']['engine_version'] != version:
            raise ValueError('Installed Freqtrade version differs from the frozen release')
        for current in db.records(conn):
            recover(current)
            reconcile(current)
            db.save(conn, current)
            if current['status'] not in TERMINAL:
                raise ValueError('Another deployment is active; stop it before creating another')
        record = {'id': str(uuid.uuid4()), 'release_id': release_id, 'experiment_id': job['id'],
                  'mode': 'dry_run', 'status': 'starting', 'ready': False, 'created_at': store.now(),
                  'updated_at': store.now(), 'error': None, 'snapshot': None, 'snapshot_stale': True,
                  'launch_at': time.time()}
        command = prepare(record, release, job)
        # A durable launch intent closes restart ambiguity; a missing identity remains blocked.
        record['command'] = command
        conn.execute('INSERT INTO deployments VALUES(?,?,?)', (record['id'], token, json.dumps(record)))
    try:
        with (directory(record) / 'engine.log').open('ab') as log:
            child = subprocess.Popen(command, cwd=directory(record), stdout=log, stderr=log,
                                     stdin=subprocess.DEVNULL, shell=False, env=child_environment(),
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        process = psutil.Process(child.pid)
        record['process'] = {'pid': child.pid, 'created': process.create_time(), 'exe': process.exe(), 'cmdline': process.cmdline()}
        with db.connect() as conn:
            current = db.get(conn, record['id'])
            if current['status'] in TERMINAL:
                stop_owned(record)
                record = current
            else:
                db.save(conn, record)
    except Exception:
        if 'child' in locals() and child.poll() is None:
            if record.get('process'):
                stop_owned(record)
            else:
                child.terminate()
                child.wait(timeout=5)
        record.update(status='failed', error='Engine launch failed; inspect private local engine log')
        with db.connect() as conn:
            db.save(conn, record)
    return public(record), True


def recover(record):
    if owned(record) or record['status'] in TERMINAL:
        return
    # Only an exact recorded typed command with launch-time match can be adopted.
    for process in psutil.process_iter(['pid', 'create_time', 'exe', 'cmdline']):
        info = process.info
        if (info['cmdline'] == record.get('command') and info['exe']
                and Path(info['exe']).resolve() in {Path(sys.executable).resolve(), Path(getattr(sys, '_base_executable', sys.executable)).resolve()}
                and abs(info['create_time'] - record['launch_at']) < 30):
            record['process'] = {'pid': info['pid'], 'created': info['create_time'], 'exe': info['exe'], 'cmdline': info['cmdline']}
            return


def get(identifier):
    with db.connect() as conn:
        record = db.get(conn, identifier)
        recover(record)
        reconcile(record)
        db.save(conn, record)
        return public(record)


def list_deployments():
    with db.connect() as conn:
        result = []
        for record in db.records(conn):
            recover(record)
            reconcile(record)
            db.save(conn, record)
            result.append(public(record))
        return result


def action(identifier, operation):
    with db.connect() as conn:
        record = db.get(conn, identifier)
        recover(record)
        if operation == 'stop':
            if not record.get('process') and record['status'] not in TERMINAL and time.time() - record['launch_at'] < 30:
                raise ValueError('Engine launch is being recorded; retry stop shortly')
            stop_owned(record)
            record.update(status='stopped', ready=False, snapshot_stale=True, updated_at=store.now(), error=None)
        else:
            reconcile(record)
            if not record['ready']:
                raise ValueError('Engine is not ready; cannot change entry state')
            rpc.call(record['rpc'], 'stopentry' if operation == 'pause' else 'start', method='POST')
            reconcile(record)
        db.save(conn, record)
        return public(record)


def stop_all():
    with db.connect() as conn:
        identifiers = [r['id'] for r in db.records(conn) if r['status'] not in TERMINAL]
    for identifier in identifiers:
        action(identifier, 'stop')
