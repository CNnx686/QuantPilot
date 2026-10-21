import json

from quantpilot import service, settings, store


def test_stale_service_state_does_not_invalidate_replacement_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HOME", tmp_path)
    monkeypatch.setattr(settings, "DB", tmp_path / "queue.sqlite3")
    state = tmp_path / "services.json"
    monkeypatch.setattr(service, "STATE", state)
    monkeypatch.setattr(service, "managed_process", lambda record: None)
    state.write_text(json.dumps([{"name": "worker", "worker_owner": "old-owner"}]))
    assert store.acquire_worker("new-owner")
    job, _ = store.enqueue("replacement", {}, "replacement")
    store.claim("new-owner")
    service.stop()
    assert store.get(job["id"])["status"] == "running"
    assert store.worker_status()["alive"]
    assert store.heartbeat("new-owner")


def test_failed_start_cleans_own_claimed_job_even_if_worker_already_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HOME", tmp_path)
    monkeypatch.setattr(settings, "DB", tmp_path / "queue.sqlite3")
    state = tmp_path / "services.json"
    monkeypatch.setattr(service, "STATE", state)
    monkeypatch.setattr(service, "managed_process", lambda record: None)
    state.write_text(json.dumps([{"name": "worker", "worker_owner": "owned-before-ready"}]))
    store.acquire_worker("owned-before-ready")
    job, _ = store.enqueue("startup failure", {}, "startup-failure")
    store.claim("owned-before-ready")
    service.stop()
    assert store.get(job["id"])["status"] == "failed"
    assert not store.worker_status()["alive"]
