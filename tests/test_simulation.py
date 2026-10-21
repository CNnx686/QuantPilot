"""Synthetic fixtures verify safety boundaries; never launch exchange processes."""
import time
import json
import threading
from pathlib import Path

import pytest

from quantpilot import deployments, deployment_rpc, settings, store
from quantpilot import deployment_store as db


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(settings, 'DB', tmp_path / 'data/qp.sqlite3')
    monkeypatch.setattr(settings, 'ARTIFACTS', tmp_path / 'artifacts')
    return tmp_path


def test_readiness_requires_real_fresh_processing():
    cfg = dict(dry_run=True, runmode='dry_run', strategy='QuantPilotSmaDryRun',
               force_entry_enable=False, state='paused')
    assert deployment_rpc.ready(cfg, {'last_process_ts': int(time.time())})
    assert not deployment_rpc.ready(cfg, {'last_process_ts': None})
    assert not deployment_rpc.ready(cfg, {'last_process_ts': time.time() - 100})
    assert not deployment_rpc.ready(dict(cfg, dry_run=False), {'last_process_ts': time.time()})
    assert not deployment_rpc.ready(dict(cfg, state='stopped'), {'last_process_ts': time.time()})


def test_environment_removes_engine_and_llm_secrets(monkeypatch):
    monkeypatch.setenv('freqtrade__dry_run', 'false')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'secret')
    monkeypatch.setenv('QUANTPILOT_LLM_API_KEY', 'secret')
    env = deployments.child_environment()
    assert not any(k.upper().startswith('FREQTRADE__') for k in env)
    assert 'DEEPSEEK_API_KEY' not in env and 'QUANTPILOT_LLM_API_KEY' not in env


def test_ml_rejected_independently(isolated, monkeypatch):
    monkeypatch.setattr(deployments, 'verify_release', lambda _: ({'status': 'approved'}, {'config': {'strategy': 'FreqaiTrend'}}))
    with pytest.raises(ValueError, match='SmaTrend'):
        deployments.create('release', 'token')


def test_service_stop_calls_deployments_without_service_state(isolated, monkeypatch):
    from quantpilot import service
    monkeypatch.setattr(service, 'STATE', isolated / 'missing.json')
    calls = []
    monkeypatch.setattr(deployments, 'stop_all', lambda: calls.append(True))
    service.stop()
    assert calls == [True]


def saved_record(**changes):
    return dict(id='synthetic', release_id='release', experiment_id='experiment', mode='dry_run',
                status='starting', launch_at=time.time(), created_at=store.now(), updated_at=store.now(),
                ready=False, snapshot=None, snapshot_stale=True, rpc={'password': 'private'}, **changes)


def insert(record, token='token'):
    with db.connect() as conn:
        conn.execute('INSERT INTO deployments VALUES(?,?,?)', (record['id'], token, json.dumps(record)))


def test_pending_launch_blocks_stop_and_other_create(isolated, monkeypatch):
    record = saved_record()
    insert(record)
    monkeypatch.setattr(deployments, 'verify_release', lambda _: ({'versions': {'engine_version': '2026.9'}}, {'config': {'strategy': 'SmaTrend'}}))
    with pytest.raises(ValueError, match='active'):
        deployments.create('other', 'other-token')
    with pytest.raises(ValueError, match='launch'):
        deployments.action(record['id'], 'stop')
    same, created = deployments.create('release', 'token')
    assert not created and same['id'] == record['id']
    assert 'rpc' not in same and 'private' not in json.dumps(same)
    with pytest.raises(ValueError, match='different'):
        deployments.create('changed', 'token')


def test_outage_saved_snapshot_stale_distinct_from_stopped(isolated, monkeypatch):
    record = saved_record(process={'pid': 42})
    record.update(status='running', launch_at=time.time()-500, snapshot={'balance': {'total': 100}})
    monkeypatch.setattr(deployments, 'owned', lambda _: object())
    monkeypatch.setattr(deployment_rpc, 'call', lambda *a, **k: (_ for _ in ()).throw(deployment_rpc.Unavailable()))
    deployments.reconcile(record)
    assert record['status'] == 'unavailable' and record['snapshot_stale'] and not record['ready']
    assert record['snapshot']['balance']['total'] == 100


def test_exact_create_retry_reconciles_dead_process_and_persists_staleness(isolated, monkeypatch):
    record = saved_record(process={'pid': 42})
    record.update(status='running', ready=True, snapshot_stale=False, snapshot={'balance': {'total': 100}})
    insert(record)
    monkeypatch.setattr(deployments, 'recover', lambda _: None)
    monkeypatch.setattr(deployments, 'owned', lambda _: None)
    result, created = deployments.create('release', 'token')
    assert not created and result['status'] == 'failed'
    assert result['snapshot_stale'] and not result['ready']
    with db.connect() as conn:
        persisted = db.get(conn, record['id'])
    assert persisted['status'] == 'failed' and persisted['snapshot_stale']


def test_process_identity_rejects_reused_pid(monkeypatch):
    class Process:
        def create_time(self): return 200
        def exe(self): raise AssertionError('birth mismatch must reject first')
    monkeypatch.setattr(deployments.psutil, 'Process', lambda _: Process())
    assert deployments.owned({'process': {'pid': 9, 'created': 100}}) is None


@pytest.fixture
def frozen(isolated, monkeypatch):
    from quantpilot.engine import engine_config, write_json
    from quantpilot.data import sha256
    job = {'id': 'experiment', 'config': {'strategy': 'SmaTrend', 'pairs': ['BTC/USDT'], 'wallet': 10000, 'stake': 1000, 'fee': .001}}
    root = settings.ARTIFACTS / job['id']
    (root / 'strategies').mkdir(parents=True)
    (root / 'strategies/SmaTrend.py').write_text('# synthetic frozen baseline')
    write_json(root / 'effective-config.json', engine_config(job['config'], root))
    release = {'id': 'release', 'versions': {'engine_version': '2026.9',
                'strategy_sha256': sha256(root / 'strategies/SmaTrend.py'),
                'config_sha256': sha256(root / 'effective-config.json')}}
    monkeypatch.setattr(deployments, 'verify_release', lambda _: (release, job))
    return release, job


def test_prepare_preserves_frozen_parameters_and_private_config(isolated, frozen):
    release, job = frozen
    record = saved_record()
    command = deployments.prepare(record, release, job)
    config = json.loads((deployments.directory(record) / 'engine-config.json').read_text())
    assert config['dry_run'] is True and config['initial_state'] == 'paused'
    assert config['force_entry_enable'] is False
    assert config['fee'] == .001 and config['stake_amount'] == 1000 and config['dry_run_wallet'] == 10000
    assert config['exchange']['key'] == config['exchange']['secret'] == ''
    assert config['exchange']['ccxt_config']['requests_trust_env'] is True
    assert config['api_server']['listen_ip_address'] == '127.0.0.1'
    # Upstream validates Telegram's required fields even when disabled.
    from jsonschema import validate
    from freqtrade.config_schema import CONF_SCHEMA
    validate(config['telegram'], CONF_SCHEMA['properties']['telegram'])
    assert str(deployments.directory(record) / 'engine-config.json') in command
    assert record['versions']['base_strategy_sha256'] == release['versions']['strategy_sha256']


def test_post_copy_tampering_rejected(isolated, frozen, monkeypatch):
    release, job = frozen
    original = deployments.shutil.copy2
    def corrupt(source, target):
        original(source, target)
        Path(target).write_text('tampered')
    monkeypatch.setattr(deployments.shutil, 'copy2', corrupt)
    with pytest.raises(ValueError, match='hash'):
        deployments.prepare(saved_record(), release, job)


def test_installed_version_must_match_release(isolated, frozen, monkeypatch):
    monkeypatch.setattr(deployments.importlib.metadata, 'version', lambda _: '2026.10')
    with pytest.raises(ValueError, match='version'):
        deployments.create('release', 'wrong-version')


def test_resume_requires_fresh_readiness(isolated, monkeypatch):
    record = saved_record(process={'pid': 42})
    insert(record)
    monkeypatch.setattr(deployments, 'recover', lambda _: None)
    monkeypatch.setattr(deployments, 'owned', lambda _: object())
    calls = []
    cfg = dict(dry_run=True, runmode='dry_run', strategy='QuantPilotSmaDryRun',
               force_entry_enable=False, state='paused')
    def call(private, endpoint, method='GET'):
        calls.append((endpoint, method))
        return cfg if endpoint == 'show_config' else {'last_process_ts': None}
    monkeypatch.setattr(deployment_rpc, 'call', call)
    with pytest.raises(ValueError, match='not ready'):
        deployments.action(record['id'], 'resume')
    assert ('start', 'POST') not in calls


def test_launch_race_does_not_start_second_process(isolated, frozen, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    class Child:
        pid = 101
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout): return 0
    class Process:
        def create_time(self): return time.time()
        def exe(self): return deployments.sys.executable
        def cmdline(self): return ['synthetic']
    def launch(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return Child()
    monkeypatch.setattr(deployments.subprocess, 'Popen', launch)
    monkeypatch.setattr(deployments.psutil, 'Process', lambda _: Process())
    monkeypatch.setattr(deployments, 'recover', lambda _: None)
    results = []
    thread = threading.Thread(target=lambda: results.append(deployments.create('release', 'first')))
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(ValueError, match='active'):
            deployments.create('release', 'second')
        with db.connect() as conn:
            identifier = db.records(conn)[0]['id']
        with pytest.raises(ValueError, match='launch'):
            deployments.action(identifier, 'stop')
    finally:
        finish.set()
        thread.join(5)
    assert len(results) == 1 and results[0][0]['status'] == 'starting'


def test_wrapper_is_observation_only_and_contains_io_failure(tmp_path, monkeypatch):
    import sys
    import pandas as pd
    monkeypatch.syspath_prepend(str(settings.ROOT / 'strategies'))
    from QuantPilotSmaDryRun import QuantPilotSmaDryRun
    strategy = QuantPilotSmaDryRun({'user_data_dir': str(tmp_path / 'userdata')})
    frame = pd.DataFrame({'date': pd.to_datetime(['2026-01-01T00:00:00Z']),
                          'sma_fast': [float('nan')], 'sma_slow': [float('inf')], 'volume': [1],
                          'enter_long': [float('nan')]})
    assert strategy.populate_exit_trend(frame, {'pair': 'BTC/USDT'}) is frame
    (tmp_path / 'snapshot.json').write_text('{}')
    assert strategy.populate_exit_trend(frame, {'pair': 'BTC/USDT'}) is frame
    record = json.loads((tmp_path / 'decisions.jsonl').read_text())
    assert record['signal_valid'] is False and record['enter_long'] is False
    assert record['risk_state'] is None and record['order_reference'] is None
    assert record['available_at'] == '2026-01-01T01:00:00+00:00'
    strategy.populate_exit_trend(frame, {'pair': 'BTC/USDT'})
    assert len((tmp_path / 'decisions.jsonl').read_text().splitlines()) == 1


def test_stop_terminates_owned_python_descendants():
    import os
    import subprocess
    import sys
    import psutil
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    process = psutil.Process(child.pid)
    record = {'process': {'pid': child.pid, 'created': process.create_time(),
                           'exe': process.exe(), 'cmdline': process.cmdline()}}
    descendants = []
    try:
        if os.name == 'nt':
            for _ in range(20):
                descendants = process.children(recursive=True)
                if any(p.name().lower() == 'python.exe' for p in descendants):
                    break
                time.sleep(.05)
        deployments.stop_owned(record)
        assert child.wait(timeout=5) is not None
        assert all(not p.is_running() for p in descendants)
    finally:
        for p in descendants + [process]:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
