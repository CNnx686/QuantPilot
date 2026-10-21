"""Local service launcher. Only controls processes it started and recorded."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import psutil

from quantpilot import settings, store

RUNTIME = settings.HOME / "runtime"
STATE = RUNTIME / "services.json"


def managed_process(record):
    try:
        process = psutil.Process(record["pid"])
        if abs(process.create_time() - record["created"]) > 0.05:
            return None
        if Path(process.exe()).resolve() != Path(record["exe"]).resolve():
            return None
        if process.cmdline() != record["cmdline"]:
            return None
        return process
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def stop():
    from quantpilot.live_control import shutdown_guard
    shutdown_guard()
    from quantpilot import deployments
    deployments.stop_all()
    if not STATE.exists():
        print("No managed QuantPilot services.")
        return
    records = json.loads(STATE.read_text(encoding="utf-8"))
    owned_worker = next((record.get("worker_owner") for record in records if record["name"] == "worker"), None)
    for record in records:
        process = managed_process(record)
        if process:
            children = process.children(recursive=True)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
            _, alive = psutil.wait_procs(children + [process], timeout=5)
            for remaining in alive:
                try:
                    remaining.kill()
                except psutil.NoSuchProcess:
                    pass
    # All our engine children have stopped; active experiments must not appear successful.
    if owned_worker:
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM worker WHERE owner=?", (owned_worker,)).fetchone():
                conn.execute("UPDATE experiments SET status='failed',finished_at=?,error=? WHERE status='running'", (store.now(), "Local services stopped; experiment interrupted."))
                conn.execute("DELETE FROM worker WHERE owner=?", (owned_worker,))
    STATE.unlink()
    print("QuantPilot services stopped. Saved experiments preserved.")


def start(port=8765):
    if STATE.exists():
        old = json.loads(STATE.read_text(encoding="utf-8"))
        if any(managed_process(record) for record in old):
            print("QuantPilot already has running services. Use stop first to restart.")
            return
    if not (settings.ROOT / "apps/web/dist/index.html").is_file():
        raise SystemExit("Frontend missing. Run npm.cmd ci and npm.cmd run build in apps/web first.")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise SystemExit(f"Port {port} is in use; select --port or stop the existing application.") from error
    if store.worker_status()["alive"]:
        raise SystemExit("Another worker is active; stop it before launching managed services.")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    commands = [("worker", [sys.executable, "-m", "quantpilot.worker"]),
                ("api", [sys.executable, "-m", "uvicorn", "quantpilot.api:app", "--host", "127.0.0.1", "--port", str(port)])]
    records = []
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    worker_owner = str(uuid.uuid4())
    try:
        for name, command in commands:
            with (RUNTIME / f"{name}.log").open("ab") as log:
                child_env = dict(env, QUANTPILOT_WORKER_OWNER=worker_owner) if name == "worker" else env
                child = subprocess.Popen(command, cwd=settings.ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=child_env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            process = psutil.Process(child.pid)
            records.append({"name": name, "pid": child.pid, "created": process.create_time(), "exe": process.exe(), "cmdline": process.cmdline(), "port": port})
            if name == "worker":
                records[-1]["worker_owner"] = worker_owner
            STATE.write_text(json.dumps(records, indent=2), encoding="utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(60):
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                    status = json.load(response)
                if status["worker"]["alive"]:
                    print(f"QuantPilot ready: http://127.0.0.1:{port}")
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError("Startup timed out; see runtime/api.log and worker.log")
    except BaseException:
        stop()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    start(args.port) if args.action == "start" else stop()
