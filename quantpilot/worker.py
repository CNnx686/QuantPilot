"""Run separately: python -m quantpilot.worker."""
import json
import os
import threading
import uuid

from . import settings, store


def execute(job, owner):
    from .engine import run_backtest
    directory = settings.ARTIFACTS / job["id"]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        result = run_backtest(job, directory)
        temporary = directory / "result.json.tmp"
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(directory / "result.json")
        store.finish(job["id"], owner, result=result)
    except Exception as exc:
        store.finish(job["id"], owner, error=str(exc)[:2000] or type(exc).__name__)


def main():
    owner = os.environ.get("QUANTPILOT_WORKER_OWNER") or str(uuid.uuid4())
    if not store.acquire_worker(owner):
        raise SystemExit("A worker is already active. Wait 30 seconds after a crashed worker before restarting.")
    stop = threading.Event()

    def pulse():
        while not stop.wait(5):
            try:
                if not store.heartbeat(owner):
                    stop.set()
            except Exception:
                stop.set()

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    print("QuantPilot worker ready", flush=True)
    job = None
    try:
        while not stop.is_set():
            job = store.claim(owner)
            if job:
                execute(job, owner)
            else:
                stop.wait(1)
    except KeyboardInterrupt:
        if job:
            store.finish(job["id"], owner, error="Worker stopped by operator; experiment interrupted.")
    finally:
        stop.set()
        thread.join(timeout=6)
        store.release_worker(owner)


if __name__ == "__main__":
    main()
