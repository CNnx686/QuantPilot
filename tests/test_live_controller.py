"""Lifecycle races and late-account-publication tests; no exchange requests."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from quantpilot import live_control as control, live_store as ledger, settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(control, 'matching_processes', lambda *args: [])
    ledger.put('account', {'ref': 'fixture'})


def setup_record(monkeypatch):
    record = dict(id='fixture', status='paused', revision=1, account={'ref': 'fixture'},
                  experiment_id='exp', dryrun_id='dry', evidence={'versions': {}})
    ledger.put('deployment', record)
    ledger.put('preflight', dict(account=record['account'], observed_at=time.time()))
    monkeypatch.setattr(control.live_evidence, 'audit', lambda *args: {'versions': {}})
    monkeypatch.setattr(control, 'ready', lambda record: True)
    return record


def test_same_revision_activate_pause_only_one_wins(monkeypatch):
    setup_record(monkeypatch)
    barrier = threading.Barrier(2)
    def run(name):
        barrier.wait()
        try:
            return control.action(name, 1)
        except ValueError:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, ['activate', 'pause']))
    winner = next(r for r in results if r)
    assert sum(r is not None for r in results) == 1
    assert ledger.get('deployment')['status'] == winner['status']
    assert ledger.get('deployment')['revision'] == 2
    control.action('pause', 2)
    assert ledger.get('deployment')['status'] == 'paused'
    assert ledger.get('deployment')['revision'] == 3


def test_failed_activation_does_not_consume_revision(monkeypatch):
    setup_record(monkeypatch)
    monkeypatch.setattr(control, 'ready', lambda record: False)
    with pytest.raises(ValueError, match='not ready'):
        control.action('activate', 1)
    assert ledger.get('deployment')['revision'] == 1


def test_future_preflight_rejected(monkeypatch):
    record = setup_record(monkeypatch)
    ledger.put('preflight', dict(account=record['account'], observed_at=time.time() + 20))
    with pytest.raises(ValueError, match='Fresh'):
        control.verify(record)


def test_late_spawn_cannot_undo_pause_on_api_restart(monkeypatch):
    setup_record(monkeypatch)
    monkeypatch.setattr(control, 'owned', lambda record: None)
    monkeypatch.setattr(control, 'stop_exact', lambda *args: None)
    def spawn(*args):
        control.start_heartbeat()
        control.stop_heartbeat()
        return {'pid': 1}
    monkeypatch.setattr(control, 'spawn', spawn)
    with pytest.raises(ValueError, match='superseded'):
        control.action('restart', 1)
    record = ledger.get('deployment')
    assert record['status'] == 'paused' and record['operation_interrupted']
    control.action('stop_process', record['revision'], True)
    assert ledger.get('deployment')['status'] == 'stopped'


def read_lease(identifier='old'):
    ledger.put('read_job', dict(id=identifier, kind='preflight', status='running',
                               deadline=time.time() + 90, account={'ref': 'fixture'}))


def test_late_watchdog_does_not_fail_new_job():
    read_lease('new')
    assert ledger.finish_read('old', 'failed') is False
    assert ledger.get('read_job')['status'] == 'running'


def test_read_scope_rolls_back_late_account_side_effects():
    read_lease()
    with ledger.read_scope('old'):
        with pytest.raises(ValueError, match='expired'):
            with ledger.connect() as db:
                ledger.put('reconciliation_required', False, db)
                current = ledger.get('read_job', db=db)
                current['deadline'] = time.time() - 1
                ledger.put('read_job', current, db)
    assert ledger.get('reconciliation_required') is None


def test_stale_scope_and_account_rotation_cannot_publish():
    read_lease()
    ledger.put('account', {'ref': 'other'})
    with pytest.raises(ValueError, match='expired'):
        with ledger.read_scope('old'):
            ledger.put('preflight', {'bad': True})
    assert ledger.get('preflight') is None


def test_create_uses_observed_funding_checkpoint(monkeypatch):
    monkeypatch.setattr(control.live_evidence, 'audit', lambda *args: {'versions': {}})
    monkeypatch.setattr(control, 'spawn', lambda *args: {'pid': 1})
    checkpoint = int((time.time() - 20) * 1000)
    ledger.put('preflight', dict(account={'ref': 'fixture'}, observed_at=time.time(), flat=True,
        snapshot=dict(history_since=checkpoint, fills=[], deposits=[], withdrawals=[], net_inflows=0,
                      balances={'USDT': {'total': 100}})))
    result = control.create(dict(experiment_id='exp', dryrun_id='dry', limits={}))
    assert result['status'] == 'paused'
    assert ledger.get('history_since') == checkpoint
    assert ledger.get('baseline')['observed_at'] == checkpoint / 1000
    assert ledger.get('daily_risk')['equity'] == 100


def test_read_job_rejects_active_account(monkeypatch):
    setup_record(monkeypatch)
    record = ledger.get('deployment')
    record['status'] = 'active'
    ledger.put('deployment', record)
    with pytest.raises(ValueError, match='Pause'):
        control.read_job('preflight')


def test_pending_operation_blocks_other_lifecycle_mutations(monkeypatch):
    record = setup_record(monkeypatch)
    record['operation'] = 'pending'
    ledger.put('deployment', record)
    with pytest.raises(ValueError, match='in progress'):
        control.action('activate', 1)
