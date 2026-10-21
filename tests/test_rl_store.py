import pytest
from quantpilot import settings, rl_store
from quantpilot.rl_config import child_environment


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(settings, 'ARTIFACTS', tmp_path / 'artifacts')


def test_single_slot_idempotency_and_recovery():
    first, created = rl_store.enqueue('trial', 'one')
    assert created
    assert rl_store.enqueue('trial', 'one') == (first, False)
    with pytest.raises(ValueError):
        rl_store.enqueue('different', 'one')
    with pytest.raises(ValueError):
        rl_store.enqueue('second', 'two')
    assert rl_store.claim()['id'] == first['id']
    assert rl_store.claim() is None
    rl_store.recover()
    assert rl_store.get(first['id'])['status'] == 'failed'
    assert (rl_store.directory(first['id']) / 'CANCEL').exists()
    rl_store.finish(first['id'], 'succeeded')
    assert rl_store.get(first['id'])['status'] == 'failed'
    assert rl_store.enqueue('second', 'two')[1]


def test_cancellation_is_terminal_and_fences_late_success():
    job, _ = rl_store.enqueue('trial', 'one')
    assert rl_store.cancel(job['id'])['status'] == 'cancelled'
    assert rl_store.claim() is None
    job, _ = rl_store.enqueue('trial', 'two')
    rl_store.claim()
    assert rl_store.cancel(job['id'])['status'] == 'cancelling'
    rl_store.finish(job['id'], 'succeeded')
    assert rl_store.get(job['id'])['status'] == 'cancelled'


def test_child_environment_removes_credentials_and_overrides(monkeypatch):
    for key in ['OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'BINANCE_SECRET', 'FREQTRADE__EXCHANGE__KEY', 'PYTHONPATH', 'HTTPS_PROXY']:
        monkeypatch.setenv(key, 'test-only-value')
        assert key not in child_environment()
    assert child_environment()['OMP_NUM_THREADS'] == '2'
    with pytest.raises(ValueError):
        rl_store.directory('../../escape')


def test_readiness_never_starts_parallel_training_probe(monkeypatch):
    from quantpilot import rl_api
    rl_store.enqueue('active', 'a')
    monkeypatch.setattr(rl_api, '_cached', None)
    def forbidden(*args, **kwargs):
        raise AssertionError('Readiness must not launch a second process during active research')
    monkeypatch.setattr(rl_api.subprocess, 'run', forbidden)
    monkeypatch.setattr(rl_api.subprocess, 'Popen', forbidden)
    assert rl_api.readiness()['probe_deferred_while_active']
