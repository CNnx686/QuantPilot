"""SQLite-backed queue and worker lease. Each operation owns its connection."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from . import settings

LEASE_SECONDS = 30


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    settings.DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, config TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            result TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS worker (
            slot INTEGER PRIMARY KEY CHECK(slot=1), owner TEXT NOT NULL,
            heartbeat REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS research (
            id TEXT PRIMARY KEY, record TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases (
            id TEXT PRIMARY KEY, record TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS suggestions (
            id TEXT PRIMARY KEY, record TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS releases_no_update BEFORE UPDATE ON releases
        BEGIN SELECT RAISE(ABORT, 'Release records are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS releases_no_delete BEFORE DELETE ON releases
        BEGIN SELECT RAISE(ABORT, 'Release records are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS confirmed_research_no_update BEFORE UPDATE ON research
        WHEN json_extract(OLD.record, '$.status') = 'confirmed'
        BEGIN SELECT RAISE(ABORT, 'Confirmed research is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS confirmed_research_no_delete BEFORE DELETE ON research
        WHEN json_extract(OLD.record, '$.status') = 'confirmed'
        BEGIN SELECT RAISE(ABORT, 'Confirmed research is immutable'); END;
    """)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def decode(row):
    if row is None:
        return None
    result = dict(row)
    result.pop("idempotency_key", None)
    result["config"] = json.loads(result["config"])
    result["result"] = json.loads(result["result"]) if result["result"] else None
    return result


def enqueue(name, config, key):
    payload = json.dumps(config, sort_keys=True, allow_nan=False)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM experiments WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            if existing["name"] != name or existing["config"] != payload:
                raise ValueError("Idempotency-Key already belongs to a different request")
            return decode(existing), False
        job_id = str(uuid.uuid4())
        conn.execute("INSERT INTO experiments(id,idempotency_key,name,config,status,created_at) VALUES(?,?,?,?,?,?)",
                     (job_id, key, name, payload, "queued", now()))
        return decode(conn.execute("SELECT * FROM experiments WHERE id=?", (job_id,)).fetchone()), True


def get(job_id):
    with connect() as conn:
        return decode(conn.execute("SELECT * FROM experiments WHERE id=?", (job_id,)).fetchone())


def find_submission(name, config, key, *, infer_dataset=False):
    payload = json.dumps(config, sort_keys=True, allow_nan=False)
    with connect() as conn:
        existing = conn.execute("SELECT * FROM experiments WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            saved = json.loads(existing["config"])
            if infer_dataset and "dataset_id" not in config:
                saved.pop("dataset_id", None)
            if existing["name"] != name or json.dumps(saved, sort_keys=True, allow_nan=False) != payload:
                raise ValueError("Idempotency-Key already belongs to a different request")
            return decode(existing)
    return None


def list_jobs():
    with connect() as conn:
        return [decode(row) for row in conn.execute("SELECT * FROM experiments ORDER BY created_at DESC")]


def worker_status():
    with connect() as conn:
        row = conn.execute("SELECT * FROM worker WHERE slot=1").fetchone()
    return {"alive": bool(row and time.time() - row["heartbeat"] < LEASE_SECONDS),
            "heartbeat_at": datetime.fromtimestamp(row["heartbeat"], timezone.utc).isoformat() if row else None}


def acquire_worker(owner):
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM worker WHERE slot=1").fetchone()
        if row and time.time() - row["heartbeat"] < LEASE_SECONDS:
            return False
        conn.execute("UPDATE experiments SET status='failed',finished_at=?,error=? WHERE status='running'",
                     (now(), "Worker interrupted; run was not completed. Submit a new experiment to retry."))
        conn.execute("INSERT OR REPLACE INTO worker VALUES(1,?,?)", (owner, time.time()))
        return True


def heartbeat(owner):
    with connect() as conn:
        return conn.execute("UPDATE worker SET heartbeat=? WHERE slot=1 AND owner=?", (time.time(), owner)).rowcount == 1


def release_worker(owner):
    with connect() as conn:
        conn.execute("DELETE FROM worker WHERE owner=?", (owner,))


def claim(owner):
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute("SELECT * FROM worker WHERE owner=? AND heartbeat>?", (owner, time.time() - LEASE_SECONDS)).fetchone()
        if not lease:
            raise RuntimeError("Worker lease lost")
        row = conn.execute("SELECT * FROM experiments WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE experiments SET status='running',started_at=? WHERE id=?", (now(), row["id"]))
        return decode(conn.execute("SELECT * FROM experiments WHERE id=?", (row["id"],)).fetchone())


def finish(job_id, owner, result=None, error=None):
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM worker WHERE owner=? AND heartbeat>?", (owner, time.time() - LEASE_SECONDS)).fetchone():
            raise RuntimeError("Worker lease lost; refusing to publish result")
        conn.execute("UPDATE experiments SET status=?,result=?,error=?,finished_at=? WHERE id=? AND status='running'",
                     ("failed" if error else "succeeded", json.dumps(result, allow_nan=False) if result is not None else None, error, now(), job_id))
