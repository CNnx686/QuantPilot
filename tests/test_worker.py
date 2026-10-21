from quantpilot import settings, store, worker


def test_controlled_stop_marks_active_job_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB", tmp_path / "queue.sqlite3")
    job, _ = store.enqueue("interrupt fixture", {}, "interrupt")

    def interrupt(job, owner):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "execute", interrupt)
    worker.main()
    assert store.get(job["id"])["status"] == "failed"
    assert not store.worker_status()["alive"]


def test_engine_failure_is_persisted_without_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB", tmp_path / "queue.sqlite3")
    monkeypatch.setattr(settings, "ARTIFACTS", tmp_path / "artifacts")
    store.acquire_worker("failure-test")
    job, _ = store.enqueue("failure fixture", {}, "failure")
    claimed = store.claim("failure-test")

    def fail(job, directory):
        (directory / "engine.log").write_text("intentional test failure")
        raise RuntimeError("Engine unavailable fixture")

    monkeypatch.setattr("quantpilot.engine.run_backtest", fail)
    worker.execute(claimed, "failure-test")
    failed = store.get(job["id"])
    assert failed["status"] == "failed"
    assert failed["result"] is None
    assert failed["error"] == "Engine unavailable fixture"
    assert (settings.ARTIFACTS / job["id"] / "engine.log").exists()
