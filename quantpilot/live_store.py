"""SQLite live control ledger. Transactions serialize account ownership and reservations."""
import json
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from . import settings

_read_token = ContextVar('live_read_token', default=None)


def check_read(identifier, db):
    job = get('read_job', {}, db)
    if (job.get('id') != identifier or job.get('status') != 'running'
            or time.time() > job.get('deadline', 0)
            or job.get('account') != get('account', db=db)):
        raise ValueError('Read job lease expired or superseded')
    return job


@contextmanager
def read_scope(identifier):
    token = _read_token.set(identifier)
    try:
        with connect() as db:
            check_read(identifier, db)
        yield
    finally:
        _read_token.reset(token)


def finish_read(identifier, status, fields=None):
    token = _read_token.set(None)
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = get('read_job', {}, db)
            if job.get('id') != identifier or job.get('status') != 'running':
                return False
            if status == 'succeeded':
                check_read(identifier, db)
            job.update(fields or {})
            job.update(status=status, completed=time.time())
            put('read_job', job, db)
            return True
    finally:
        _read_token.reset(token)


def path():
    return settings.HOME / 'runtime' / 'live' / 'ledger.sqlite3'


@contextmanager
def connect():
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS intents (
      id TEXT PRIMARY KEY, pair TEXT NOT NULL, side TEXT NOT NULL,
      amount REAL NOT NULL, reserve REAL NOT NULL, state TEXT NOT NULL,
      remote_id TEXT, filled REAL NOT NULL DEFAULT 0, created REAL NOT NULL,
      managed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS requests (
      key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, intent TEXT NOT NULL, payload TEXT NOT NULL);
    ''')
    if 'transport_started' not in {row[1] for row in db.execute('PRAGMA table_info(intents)')}:
        db.execute('ALTER TABLE intents ADD COLUMN transport_started INTEGER NOT NULL DEFAULT 1')
        db.commit()
    try:
        if _read_token.get():
            db.execute('BEGIN IMMEDIATE')
            check_read(_read_token.get(), db)
            db.commit()
        yield db
        if _read_token.get():
            # Acquire the write lock before checking even for a read-only scope.
            if not db.in_transaction:
                db.execute('BEGIN IMMEDIATE')
            check_read(_read_token.get(), db)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def get(key, default=None, db=None):
    if db is None:
        with connect() as conn:
            return get(key, default, conn)
    row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(key, value, db=None):
    if db is None:
        with connect() as conn:
            put(key, value, conn)
        return
    db.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)',
               (key, json.dumps(value, allow_nan=False, sort_keys=True)))


def uncertain(db):
    return db.execute("SELECT 1 FROM intents WHERE state IN ('intent','unknown') OR (remote_id IS NOT NULL AND managed=0) LIMIT 1").fetchone() is not None


def mark_unknown(identifier):
    with connect() as db:
        db.execute("UPDATE intents SET state='unknown' WHERE id=?", (identifier,))
        put('reconciliation_required', True, db)
