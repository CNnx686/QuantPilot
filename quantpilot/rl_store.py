"""Atomic persisted single-slot research queue, separate from old experiment/release data."""
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from . import settings
from .rl_config import PROTOCOL, artifact_root


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    path = settings.HOME / 'runtime' / 'rl.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, key TEXT UNIQUE,
        name TEXT NOT NULL, config TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT, started_at TEXT, finished_at TEXT, error TEXT)''')
    if 'process' not in {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}:
        try:
            db.execute('ALTER TABLE jobs ADD COLUMN process TEXT')
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc):
                raise
    try:
        with db:
            yield db
    finally:
        db.close()


def decode(row):
    if row is None:
        return None
    item = dict(row)
    item.pop('key', None)
    item.pop('process', None)
    item['config'] = json.loads(item['config'])
    return item


def directory(identifier):
    if not re.fullmatch(r'rl-[a-f0-9]{32}', identifier):
        raise ValueError('Invalid RL job ID')
    return artifact_root() / identifier


def get(identifier):
    with connect() as db:
        return decode(db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())


def list_jobs():
    with connect() as db:
        return [decode(row) for row in db.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100')]


def find_submission(name, key):
    with connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
        if row and row['name'] != name:
            raise ValueError('Idempotency-Key was already used for another name')
        return decode(row)


def enqueue(name, key):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
        if row:
            if row['name'] != name:
                raise ValueError('Idempotency-Key was already used for another name')
            return decode(row), False
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','cancelling')").fetchone():
            raise ValueError('Only one RL research job may be active')
        identifier = 'rl-' + uuid.uuid4().hex
        db.execute('INSERT INTO jobs(id,key,name,config,status,created_at) VALUES(?,?,?,?,?,?)',
                   (identifier, key, name, json.dumps(PROTOCOL), 'queued', now()))
        return decode(db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()), True


def claim():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('running','cancelling')").fetchone():
            return None
        row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            return None
        db.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (now(), row['id']))
        return decode(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())


def finish(identifier, status, error=None):
    if status not in {'succeeded', 'failed', 'cancelled'}:
        raise ValueError('Invalid terminal status')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status FROM jobs WHERE id=?', (identifier,)).fetchone()
        if row is None or row['status'] not in {'running', 'cancelling'}:
            return
        if row['status'] == 'cancelling':
            status = 'cancelled'
        db.execute('UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?',
                   (status, error, now(), identifier))


def cancel(identifier):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            return None
        if row['status'] in {'queued', 'running', 'cancelling'}:
            path = directory(identifier)
            path.mkdir(parents=True, exist_ok=True)
            (path / 'CANCEL').touch()
            status = 'cancelled' if row['status'] == 'queued' else 'cancelling'
            db.execute('UPDATE jobs SET status=?, finished_at=? WHERE id=?',
                       (status, now() if status == 'cancelled' else None, identifier))
        return decode(db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())


def set_process(identifier, identity):
    with connect() as db:
        db.execute('UPDATE jobs SET process=? WHERE id=? AND status IN (\'running\',\'cancelling\')',
                   (json.dumps(identity), identifier))


def recover():
    # Called under scheduler lock. Never release the slot based on elapsed time alone.
    from .rl_process import recover_owned
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT * FROM jobs WHERE status IN ('running','cancelling')").fetchall()
        for row in rows:
            db.execute("UPDATE jobs SET status='cancelling', error=? WHERE id=?",
                       ('Restart recovery in progress; slot quarantined until owned processes exit', row['id']))
    for row in rows:
        path = directory(row['id'])
        path.mkdir(parents=True, exist_ok=True)
        (path / 'CANCEL').touch()
        exited = recover_owned(row['id'], path, json.loads(row['process']) if row['process'] else None)
        if exited:
            status = 'cancelled' if row['status'] == 'cancelling' else 'failed'
            with connect() as db:
                db.execute('UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=? AND status=\'cancelling\'',
                           (status, 'Interrupted by worker restart; owned processes verified exited; partial artifacts retained', now(), row['id']))
