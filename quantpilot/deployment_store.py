"""Private persistent process ownership, serialized with SQLite write transactions."""
import json
import sqlite3
from contextlib import contextmanager

from . import settings


@contextmanager
def connect():
    path = settings.HOME / 'runtime' / 'deployments.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS deployments (id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, record TEXT NOT NULL)')
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            yield conn
    finally:
        conn.close()


def records(conn):
    return [json.loads(r['record']) for r in conn.execute('SELECT record FROM deployments ORDER BY rowid DESC')]


def save(conn, record):
    conn.execute('UPDATE deployments SET record=? WHERE id=?',
                 (json.dumps(record, allow_nan=False), record['id']))


def get(conn, identifier):
    row = conn.execute('SELECT record FROM deployments WHERE id=?', (identifier,)).fetchone()
    if row is None:
        raise KeyError('Deployment not found')
    return json.loads(row['record'])
