import sys
import threading
import subprocess
import time
import json
import psutil
import pytest
from quantpilot.rl_runner import bounded
from quantpilot.rl_config import child_environment
from quantpilot import settings, rl_store, rl_process


def test_real_child_cancelled_and_reaped(tmp_path):
    timer = threading.Timer(.25, lambda: (tmp_path / 'CANCEL').touch())
    timer.start()
    try:
        with pytest.raises(RuntimeError, match='Cancelled'):
            bounded([sys.executable, '-c', 'import time; time.sleep(30)'], tmp_path / 'child.log', tmp_path, 10, child_environment())
    finally:
        timer.cancel()


def test_real_child_deadline_enforced(tmp_path):
    with pytest.raises(RuntimeError, match='deadline'):
        bounded([sys.executable, '-c', 'import time; time.sleep(30)'], tmp_path / 'child.log', tmp_path, .2, child_environment())


def test_restart_reaps_verified_coordinator_and_child_before_releasing_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(settings, 'ARTIFACTS', tmp_path / 'artifacts')
    job, _ = rl_store.enqueue('orphan test', 'orphan')
    rl_store.claim()
    package = tmp_path / 'quantpilot'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'rl_runner.py').write_text("import subprocess,sys,time,json\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\nPath('child.json').write_text(json.dumps({'pid':p.pid}))\ntime.sleep(30)\n")
    process = subprocess.Popen([sys.executable, '-m', 'quantpilot.rl_runner', job['id']], cwd=tmp_path,
                               env=child_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        rl_store.set_process(job['id'], rl_process.identity(process.pid))
        deadline = time.monotonic() + 8
        while not (tmp_path / 'child.json').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        child_pid = json.loads((tmp_path / 'child.json').read_text())['pid']
        rl_store.recover()
        process.wait(timeout=5)
        assert not psutil.pid_exists(child_pid)
        assert rl_store.get(job['id'])['status'] == 'failed'
        assert rl_store.enqueue('next safe job', 'next')[1]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_uncertain_recovery_quarantines_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(settings, 'ARTIFACTS', tmp_path / 'artifacts')
    job, _ = rl_store.enqueue('uncertain', 'a')
    rl_store.claim()
    monkeypatch.setattr(rl_process, 'find_owned', lambda *_: ([], True))
    rl_store.recover()
    assert rl_store.get(job['id'])['status'] == 'cancelling'
    with pytest.raises(ValueError, match='Only one'):
        rl_store.enqueue('must not start', 'b')


def test_recovery_matches_only_exact_job_commands(tmp_path):
    assert rl_process.matches_command([sys.executable, '-m', 'quantpilot.rl_runner', 'rl-test'], 'rl-test', tmp_path)
    assert not rl_process.matches_command([sys.executable, '-m', 'quantpilot.rl_runner', 'rl-other'], 'rl-test', tmp_path)
    assert not rl_process.matches_command([sys.executable, '-m', 'quantpilot.rl_train', str(tmp_path.parent)], 'rl-test', tmp_path)


def test_readiness_timeout_reaps_actual_venv_launcher_descendant(tmp_path):
    from quantpilot.rl_api import compatibility_process
    from quantpilot.rl_config import python_path
    if not python_path().is_file():
        pytest.skip('The actual isolated venv is not installed')
    receipt = tmp_path / 'probe-process.json'
    # No Torch fit: an actual venv Python child writes its identity and sleeps.
    code = "import os,time,json; from pathlib import Path; Path(" + repr(str(receipt)) + ").write_text(json.dumps({'pid':os.getpid()})); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        compatibility_process([str(python_path()), '-c', code], timeout=2)
    assert receipt.is_file(), 'The actual venv descendant must start before timing out'
    child_pid = json.loads(receipt.read_text())['pid']
    assert not psutil.pid_exists(child_pid), 'Readiness timeout leaked a venv descendant'
