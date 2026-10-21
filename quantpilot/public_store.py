"""Independent persisted public-data cache. Never migrates the experiment database."""
import json
import sqlite3
import time
import uuid
import contextvars
from contextlib import contextmanager
from datetime import datetime, timezone

from . import settings

WORKER_JOB = contextvars.ContextVar('public_worker_job', default=None)


def require_lease(conn):
    identifier = WORKER_JOB.get()
    if identifier and not conn.execute("SELECT 1 FROM jobs WHERE id=? AND status='running' AND lease_until>?", (identifier, time.time())).fetchone():
        raise ValueError('worker_lease_expired')


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def path():
    return settings.HOME / 'runtime' / 'mvp2-data.sqlite3'


@contextmanager
def connect():
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
          started_at TEXT, finished_at TEXT, lease_until REAL, result TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, record TEXT NOT NULL,
          last_success TEXT, last_attempt TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY, source TEXT NOT NULL, item_key TEXT NOT NULL,
          content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL, revision_seen_at TEXT NOT NULL,
          record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS analyses(version_id TEXT NOT NULL, provider TEXT NOT NULL,
          model_key TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(version_id,provider,model_key));
        CREATE TRIGGER IF NOT EXISTS immutable_news_update BEFORE UPDATE ON news
          BEGIN SELECT RAISE(ABORT,'News versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_news_delete BEFORE DELETE ON news
          BEGIN SELECT RAISE(ABORT,'News versions are immutable'); END;
    ''')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def decode_job(row):
    if row is None:
        return None
    value = dict(row)
    value['payload'] = json.loads(value['payload'])
    value['result'] = json.loads(value['result']) if value['result'] else None
    value.pop('lease_until', None)
    return value


def get_job(identifier):
    with connect() as conn:
        return decode_job(conn.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())


def enqueue(kind, source, payload=None):
    payload = payload or {}
    serialized = dumps(payload)
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        # Interrupted jobs are terminal, never automatically repeat a potentially paid request.
        conn.execute("UPDATE jobs SET status='failed',error='interrupted_or_deadline_exceeded',finished_at=? WHERE status='running' AND lease_until<?", (now(), time.time()))
        existing = conn.execute("SELECT * FROM jobs WHERE kind=? AND source=? AND payload=? AND status IN ('queued','running') ORDER BY rowid DESC LIMIT 1", (kind, source, serialized)).fetchone()
        if existing:
            return decode_job(existing)
        if conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 8:
            raise ValueError('Refresh queue is full; wait for an existing job')
        recent = conn.execute('SELECT * FROM jobs WHERE kind=? AND source=? AND payload=? ORDER BY rowid DESC LIMIT 1', (kind, source, serialized)).fetchone()
        if recent and (datetime.now(timezone.utc) - datetime.fromisoformat(recent['created_at'])).total_seconds() < 60:
            return decode_job(recent)
        if kind == 'analysis' and payload.get('provider') == 'deepseek':
            count = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='analysis' AND json_extract(payload,'$.provider')='deepseek' AND created_at>=?", (datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0]
            if count >= 10:
                raise ValueError('Daily DeepSeek request budget reached (10 attempts per UTC day)')
        identifier = str(uuid.uuid4())
        conn.execute('INSERT INTO jobs(id,kind,source,payload,status,created_at) VALUES(?,?,?,?,?,?)', (identifier, kind, source, serialized, 'queued', now()))
        return decode_job(conn.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())


def claim():
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE jobs SET status='failed',error='interrupted_or_deadline_exceeded',finished_at=? WHERE status='running' AND lease_until<?", (now(), time.time()))
        # A single global execution slot across all API processes.
        if conn.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():
            return None
        row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE jobs SET status='running',started_at=?,lease_until=? WHERE id=?", (now(), time.time() + 95, row['id']))
        return decode_job(conn.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())


def finish(identifier, result=None, error=None, cache_key=None):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute("SELECT 1 FROM jobs WHERE id=? AND status='running' AND lease_until>?", (identifier,time.time())).fetchone():
            return False
        if error and cache_key:
            previous = conn.execute('SELECT * FROM cache WHERE key=?',(cache_key,)).fetchone()
            conn.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?)', (cache_key,previous['record'] if previous else 'null',previous['last_success'] if previous else None,now(),error))
        conn.execute("UPDATE jobs SET status=?,finished_at=?,result=?,error=? WHERE id=? AND status='running' AND lease_until>?", ('failed' if error else 'succeeded', now(), dumps(result) if result is not None else None, error, identifier, time.time()))
        return True


def cached(key, ttl=300):
    with connect() as conn:
        row = conn.execute('SELECT * FROM cache WHERE key=?', (key,)).fetchone()
    if not row:
        return {'data': None, 'last_success': None, 'last_attempt': None, 'error': None, 'stale': True, 'availability': 'unverified'}
    value = dict(row)
    value.pop('key')
    value['data'] = json.loads(value.pop('record'))
    value['stale'] = not value['last_success'] or (datetime.now(timezone.utc) - datetime.fromisoformat(value['last_success'])).total_seconds() > ttl
    value['availability'] = 'unavailable' if value['error'] else 'available'
    return value


def save_cache(key, record=None, error=None):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        require_lease(conn)
        previous = conn.execute('SELECT * FROM cache WHERE key=?', (key,)).fetchone()
        conn.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?)', (key, dumps(record) if error is None else previous['record'] if previous else 'null', now() if error is None else previous['last_success'] if previous else None, now(), error))
