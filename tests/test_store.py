"""Queue persistence and lease tests; no trading engine is mocked or run here."""
import time

import pytest

from quantpilot import settings, store


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB", tmp_path / "queue.sqlite3")


def test_persisted_idempotency_and_conflict():
    job, created = store.enqueue("baseline", {"fee": 0.001}, "request-1")
    assert created and store.get(job["id"])["status"] == "queued"
    duplicate, created = store.enqueue("baseline", {"fee": 0.001}, "request-1")
    assert not created and duplicate["id"] == job["id"]
    with pytest.raises(ValueError):
        store.enqueue("baseline", {"fee": 0.002}, "request-1")
    assert len(store.list_jobs()) == 1


def test_single_worker_and_crash_recovery():
    job, _ = store.enqueue("baseline", {}, "request-1")
    assert store.acquire_worker("first")
    assert not store.acquire_worker("second")
    assert store.claim("first")["id"] == job["id"]
    assert store.claim("first") is None
    with store.connect() as conn:
        conn.execute("UPDATE worker SET heartbeat=?", (time.time() - 60,))
    assert store.acquire_worker("second")
    assert store.get(job["id"])["status"] == "failed"
    with pytest.raises(RuntimeError, match="lease lost"):
        store.finish(job["id"], "first", result={"fake": True})
    assert store.get(job["id"])["result"] is None


def test_claim_and_finish_are_persisted():
    job, _ = store.enqueue("baseline", {}, "request-1")
    assert store.acquire_worker("worker")
    assert store.claim("worker")["status"] == "running"
    store.finish(job["id"], "worker", result={"trades": 0})
    completed = store.get(job["id"])
    assert completed["status"] == "succeeded"
    assert completed["result"] == {"trades": 0}
    assert completed["started_at"] and completed["finished_at"]
