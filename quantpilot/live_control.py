"""Serialized live lifecycle with separate process and engine readiness evidence."""
import os
import subprocess
import sys
import time
import uuid
import threading
import psutil
from . import settings, live_store as ledger, live_evidence
from .deployments import owned, stop_owned, child_environment

_heartbeat_stop = threading.Event()
_heartbeat_thread = None


def start_heartbeat():
    global _heartbeat_thread
    stop_heartbeat()
    _heartbeat_stop.clear()
    owner = uuid.uuid4().hex
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        ledger.put('controller', owner, db)
        record = ledger.get('deployment', db=db)
        if record and record.get('status') != 'stopped':
            record.update(status='paused', revision=record['revision'] + 1, heartbeat=0)
            if record.get('operation'):
                record['operation_interrupted'] = True
            ledger.put('deployment', record, db)
    def beat():
        while not _heartbeat_stop.wait(3):
            with ledger.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if ledger.get('controller', db=db) != owner:
                    return
                record = ledger.get('deployment', db=db)
                if record:
                    record['heartbeat'] = time.time()
                    ledger.put('deployment', record, db)
    _heartbeat_thread = threading.Thread(target=beat, daemon=True)
    _heartbeat_thread.start()


def stop_heartbeat():
    global _heartbeat_thread
    _heartbeat_stop.set()
    if _heartbeat_thread:
        _heartbeat_thread.join(timeout=12)
        if _heartbeat_thread.is_alive():
            raise RuntimeError('Previous live controller has not stopped')
        _heartbeat_thread = None


def ready(record):
    proof = ledger.get('engine_ready') or {}
    root = owned(record)
    versions = record.get('evidence', {}).get('versions', {})
    if (not root or proof.get('deployment_id') != record['id']
            or proof.get('guard_installed') is not True or proof.get('reconciled') is not True
            or proof.get('status') == 'stopped' or not 0 <= time.time() - proof.get('heartbeat', 0) <= 60
            or proof.get('engine_version') != '2026.9'
            or proof.get('strategy_sha256') != versions.get('strategy_sha256')
            or proof.get('source_config_sha256') != versions.get('config_sha256')):
        return False
    try:
        process = psutil.Process(proof['pid'])
        if abs(process.create_time() - proof['created']) > .05:
            return False
        if process.pid != root.pid and root.pid not in {p.pid for p in process.parents()}:
            return False
        from .data import sha256
        return sha256(ledger.path().parent / 'engine-config.json') == proof['effective_config_sha256']
    except (psutil.Error, OSError, KeyError):
        return False


def status():
    record = ledger.get('deployment') or {}
    deployment = {k: v for k, v in record.items() if k in {'id', 'status', 'revision'}}
    if record:
        deployment.update(process_alive=bool(owned(record)), engine_ready=ready(record))
    return {'auth_configured': bool(ledger.get('auth')), 'accounts_configured': bool(ledger.get('account')),
            'policy': live_evidence.POLICY, 'supported_pairs': ['BTC/USDT', 'ETH/USDT'],
            'supported_venue': 'binance', 'fixture_only_evidence': True,
            'implementation_capabilities': ['local_dpapi_enrollment', 'session_csrf', 'durable_intents', 'spot_guard', 'paused_process_launch', 'exact_id_reconciliation'],
            'gates': ['local_auth', 'dedicated_account_enrollment', 'historical_audit', '24h_dryrun_and_closed_trade', 'authoritative_preflight', 'explicit_activation'],
            'deployment': deployment, 'read_job': ledger.get('read_job', {}).get('status', 'not_started')}


def shutdown_guard():
    record = ledger.get('deployment')
    if record and record.get('status') != 'stopped':
        raise RuntimeError('Live protection may be active. Explicitly stop the live process before global shutdown.')


def spawn(mode, identifier):
    command = [sys.executable, '-m', 'quantpilot.live_runner', mode, identifier]
    process = subprocess.Popen(command, cwd=settings.ROOT, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=child_environment(), creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        identity = psutil.Process(process.pid)
        return {'pid': identity.pid, 'created': identity.create_time(), 'exe': identity.exe(), 'cmdline': identity.cmdline()}
    except psutil.Error:
        process.wait(timeout=5)
        raise ValueError('Child exited before process ownership could be recorded') from None


def matching_processes(mode, identifier):
    matches = []
    for process in psutil.process_iter(['pid', 'cmdline', 'create_time', 'exe']):
        try:
            info = process.info
            command = info['cmdline'] or []
            if command[1:] == ['-m', 'quantpilot.live_runner', mode, identifier]:
                matches.append({'process': {'pid': info['pid'], 'created': info['create_time'],
                                          'exe': info['exe'], 'cmdline': command}})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def stop_exact(mode, identifier, record):
    candidates = [record] + matching_processes(mode, identifier)
    for candidate in candidates:
        stop_owned(candidate)
    if any(owned(candidate) for candidate in candidates) or matching_processes(mode, identifier):
        raise ValueError('Process termination could not be verified')


def read_job(kind):
    if kind not in {'preflight', 'reconcile', 'recover_flat'}:
        raise ValueError('Unknown read job')
    current = ledger.get('read_job', {})
    if current.get('status') == 'running':
        if time.time() <= current.get('deadline', 0):
            raise ValueError('A bounded read job is already running')
        stop_exact(current['kind'], current['id'], current)
        ledger.finish_read(current['id'], 'failed', {'reason': 'Read deadline exceeded'})
    elif current.get('id'):
        stop_exact(current['kind'], current['id'], current)
    identifier = uuid.uuid4().hex
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if ledger.get('read_job', {}, db).get('status') == 'running':
            raise ValueError('A bounded read job is already running')
        deployment = ledger.get('deployment', {}, db)
        if deployment.get('status') == 'active' or deployment.get('operation'):
            raise ValueError('Pause the deployment before authoritative reads')
        account = ledger.get('account', db=db)
        if not account:
            raise ValueError('Local account enrollment required')
        ledger.put('read_job', {'id': identifier, 'status': 'running', 'kind': kind,
                              'started': time.time(), 'deadline': time.time() + 90, 'account': account}, db)
    try:
        identity = spawn(kind, identifier)
        with ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = ledger.get('read_job', {}, db)
            if job.get('id') != identifier:
                raise ValueError('Read process lease was superseded')
            job['process'] = identity
            ledger.put('read_job', job, db)
    except Exception:
        stop_exact(kind, identifier, {})
        ledger.finish_read(identifier, 'failed', {'reason': 'Read process failed to start'})
        raise
    def watch():
        process = owned({'process': identity})
        try:
            if process:
                process.wait(timeout=90)
            stop_exact(kind, identifier, {'process': identity})
            ledger.finish_read(identifier, 'failed', {'reason': 'Read process exited without publishing evidence'})
        except psutil.TimeoutExpired:
            try:
                stop_exact(kind, identifier, {'process': identity})
                ledger.finish_read(identifier, 'failed', {'reason': 'Read deadline exceeded'})
            except Exception:
                pass  # Keep quarantined until process exit can be proved.
    threading.Thread(target=watch, daemon=True).start()
    return {'id': identifier, 'status': 'queued'}


def _preflight(record, db=None):
    account = ledger.get('account', db=db)
    preflight = ledger.get('preflight', db=db)
    if (not account or account != record['account'] or not preflight or preflight['account'] != account
            or not 0 <= time.time() - preflight['observed_at'] <= 60):
        raise ValueError('Fresh authoritative preflight required')
    if ledger.get('reconciliation_required', False, db):
        raise ValueError('Account reconciliation required')
    if ledger.get('read_job', {}, db).get('status') == 'running':
        raise ValueError('Authoritative read is still running')
    return preflight


def verify(record):
    evidence = live_evidence.audit(record['experiment_id'], record['dryrun_id'])
    if evidence['versions'] != record['evidence']['versions']:
        raise ValueError('Historical source hashes changed')
    _preflight(record)
    return evidence


def _summary(record):
    return {k: record[k] for k in ('id', 'status', 'revision')}


def _finish_operation(identifier, token, updates):
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        record = ledger.get('deployment', {}, db)
        if record.get('id') != identifier or record.get('operation') != token or record.get('operation_interrupted'):
            raise ValueError('Lifecycle operation was superseded; deployment stays paused')
        record.update(updates)
        record.pop('operation', None)
        record['revision'] += 1
        ledger.put('deployment', record, db)
        return _summary(record)


def _launch(record, token):
    try:
        identity = spawn('engine', record['id'])
        return _finish_operation(record['id'], token, {'process': identity, 'status': 'paused'})
    except Exception:
        stop_exact('engine', record['id'], {})
        try:
            _finish_operation(record['id'], token, {'status': 'stopped', 'process': None, 'last_error': 'Engine launch failed'})
        except ValueError:
            pass
        raise


def create(payload):
    evidence = live_evidence.audit(payload['experiment_id'], payload['dryrun_id'])
    token = uuid.uuid4().hex
    record = payload | {'id': uuid.uuid4().hex, 'status': 'starting', 'revision': 1, 'operation': token,
                        'account': ledger.get('account'), 'evidence': evidence, 'heartbeat': 0}
    verify(record)
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if ledger.get('deployment', db=db):
            raise ValueError('One global deployment already owns the account')
        pre = _preflight(record, db)
        if pre['flat'] is not True:
            raise ValueError('Initial account must be flat')
        snapshot = pre['snapshot']
        checkpoint = snapshot['history_since']
        if not 0 <= time.time() - checkpoint / 1000 <= 60 or snapshot['fills'] or snapshot['deposits'] or snapshot['withdrawals']:
            raise ValueError('Initial funding checkpoint is stale or changed during observation')
        ledger.put('deployment', record, db)
        ledger.put('baseline', {'equity': snapshot['balances']['USDT']['total'], 'observed_at': checkpoint / 1000}, db)
        ledger.put('daily_risk', {'equity': snapshot['balances']['USDT']['total'], 'net_inflows': snapshot['net_inflows'],
                               'day': time.strftime('%Y-%m-%d', time.gmtime(checkpoint / 1000)),
                               'observed_at': checkpoint / 1000, 'partial_day': True}, db)
        ledger.put('history_since', checkpoint, db)
        ledger.put('engine_ready', None, db)
    return _launch(record, token)


def action(name, revision, acknowledge=False):
    if name not in {'activate', 'pause', 'stop_process', 'restart'}:
        raise ValueError('Unsupported action')
    if name == 'stop_process' and not acknowledge:
        raise ValueError('Acknowledge that local stoploss protection ceases')
    record = ledger.get('deployment')
    if not record or record['revision'] != revision:
        raise ValueError('Resource revision conflict')
    if name in {'activate', 'restart'}:
        verify(record)
    token = uuid.uuid4().hex
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        record = ledger.get('deployment', {}, db)
        if record.get('revision') != revision:
            raise ValueError('Resource revision conflict')
        if record.get('operation') and not (name == 'stop_process' and record.get('operation_interrupted')):
            raise ValueError('Lifecycle operation is in progress')
        if name in {'activate', 'restart'}:
            _preflight(record, db)
        if name == 'activate':
            if not ready(record):
                raise ValueError('Guarded engine startup and reconciliation are not ready')
            record['status'] = 'active'
        elif name == 'pause':
            if record.get('status') == 'stopped':
                raise ValueError('Restart the stopped engine first')
            record['status'] = 'paused'
        elif name == 'restart':
            if owned(record) or matching_processes('engine', record['id']):
                raise ValueError('Stop existing engine before restart')
            record.update(status='starting', operation=token)
            ledger.put('engine_ready', None, db)
        else:
            record.update(status='stopping', operation=token)
            record.pop('operation_interrupted', None)
        record['revision'] += 1
        ledger.put('deployment', record, db)
    if name == 'restart':
        return _launch(record, token)
    if name == 'stop_process':
        stop_exact('engine', record['id'], record)
        return _finish_operation(record['id'], token, {'status': 'stopped', 'process': None})
    return _summary(record)
