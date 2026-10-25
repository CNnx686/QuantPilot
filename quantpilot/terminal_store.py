"""Versioned terminal workflow records, isolated from historical research databases."""
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from . import settings


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix):
    return prefix + '-' + uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


@contextmanager
def connect():
    target = settings.HOME / 'data' / 'terminal.sqlite3'
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY, record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS instances(id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, request TEXT NOT NULL, record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operations(token TEXT PRIMARY KEY, operation TEXT NOT NULL, payload TEXT NOT NULL, response TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS frozen_candidates_update BEFORE UPDATE ON candidates
          BEGIN SELECT RAISE(ABORT, 'Frozen candidates cannot be changed'); END;
        CREATE TRIGGER IF NOT EXISTS frozen_candidates_delete BEFORE DELETE ON candidates
          BEGIN SELECT RAISE(ABORT, 'Frozen candidates cannot be deleted'); END;
    ''')
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
    finally:
        conn.close()


def read(conn, table, key):
    if table not in {'strategies', 'jobs', 'candidates', 'instances'}:
        raise ValueError('Unknown terminal collection')
    row = conn.execute(f'SELECT record FROM {table} WHERE id=?', (key,)).fetchone()
    if row is None:
        raise KeyError(f'{table}: record not found')
    return json.loads(row['record'])


def save(conn, table, value):
    if table not in {'strategies', 'jobs', 'instances'}:
        raise ValueError('Collection is immutable or unknown')
    conn.execute(f'UPDATE {table} SET record=? WHERE id=?', (dumps(value), value['id']))


def rows(conn, table):
    if table not in {'strategies', 'jobs', 'candidates', 'instances'}:
        raise ValueError('Unknown terminal collection')
    return [json.loads(row['record']) for row in conn.execute(f'SELECT record FROM {table} ORDER BY rowid DESC')]


def get(table, key):
    with connect() as conn:
        return read(conn, table, key)


def list_records(table):
    with connect() as conn:
        return rows(conn, table)


def create_strategy(config, token=None):
    item = {'id': identifier('strategy'), 'revision': 1, 'config': config,
            'selected_candidate_id': None, 'created_at': now(), 'updated_at': now()}
    with connect() as conn:
        if token:
            previous = operation(conn, token, 'create_strategy', config)
            if previous is not None:
                return previous
        conn.execute('INSERT INTO strategies VALUES(?,?)', (item['id'], dumps(item)))
        if token:
            complete_operation(conn, token, 'create_strategy', config, item)
    return item


def update_strategy(key, revision, config):
    with connect() as conn:
        item = read(conn, 'strategies', key)
        if item['revision'] != revision:
            raise ValueError('Strategy changed; refresh before saving this revision')
        if any(instance['strategy_id'] == key for instance in rows(conn, 'instances')):
            raise ValueError('This strategy already has a simulated account history; copy it into a new strategy before editing its configuration')
        item.update(config=config, revision=revision + 1, selected_candidate_id=None, updated_at=now())
        save(conn, 'strategies', item)
        return item


def job_directory(key):
    if not re.fullmatch(r'tjob-[a-f0-9]{32}', key):
        raise ValueError('Invalid terminal job identifier')
    return settings.HOME / 'artifacts' / 'terminal' / key


def enqueue(strategy_id, token, mode='single'):
    if mode not in {'single', 'comparison'}:
        raise ValueError('Unknown experiment mode')
    with connect() as conn:
        strategy = read(conn, 'strategies', strategy_id)
        previous = conn.execute('SELECT record FROM jobs WHERE token=?', (token,)).fetchone()
        if previous:
            item = json.loads(previous['record'])
            if item['strategy_id'] != strategy_id or item.get('mode', 'single') != mode:
                raise ValueError('Idempotency-Key belongs to another request')
            return item, False
        if sum(item['status'] in {'queued', 'running', 'cancelling'} for item in rows(conn, 'jobs')) >= 8:
            raise ValueError('Training queue is full; wait or cancel a queued task')
        item = {'id': identifier('tjob'), 'strategy_id': strategy_id,
                'revision': strategy['revision'], 'config': strategy['config'],
                'config_sha256': digest(strategy['config']), 'status': 'queued', 'phase': 'queued',
                'created_at': now(), 'started_at': None, 'finished_at': None,
                'candidate_id': None, 'candidate_ids': [], 'mode': mode,
                'comparison': None, 'error': None, 'process': None}
        conn.execute('INSERT INTO jobs VALUES(?,?,?)', (item['id'], token, dumps(item)))
        return item, True


def claim():
    with connect() as conn:
        jobs = rows(conn, 'jobs')
        if any(j['status'] in {'running', 'cancelling'} for j in jobs):
            return None
        for item in reversed(jobs):
            if item['status'] == 'queued':
                item.update(status='running', phase='preparing_data', started_at=now())
                save(conn, 'jobs', item)
                return item
    return None


def progress(key, phase, process=None, comparison=None):
    with connect() as conn:
        item = read(conn, 'jobs', key)
        if item['status'] != 'running':
            raise ValueError('Task no longer running')
        item['phase'] = phase
        if process:
            item['process'] = process
        if comparison is not None:
            item['comparison'] = comparison
        save(conn, 'jobs', item)


def cancel(key):
    with connect() as conn:
        item = read(conn, 'jobs', key)
        if item['status'] in {'queued', 'running', 'cancelling'}:
            queued = item['status'] == 'queued'
            item.update(status='cancelled' if queued else 'cancelling',
                        phase='cancelled' if queued else 'cancelling',
                        finished_at=now() if queued else None)
            directory = job_directory(key)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'CANCEL').touch()
            save(conn, 'jobs', item)
        return item


def finish(key, candidate=None, error=None, candidates=None, comparison=None):
    with connect() as conn:
        item = read(conn, 'jobs', key)
        if item['status'] not in {'running', 'cancelling'}:
            return item
        cancelled = item['status'] == 'cancelling'
        completed = candidates if candidates is not None else [candidate] if candidate is not None else []
        status = 'cancelled' if cancelled else 'failed' if error or not completed else 'succeeded'
        if item.get('mode') == 'comparison' and status == 'succeeded':
            if (len(completed) != 2 or {value.get('variant') for value in completed} != {'price_only', 'price_news'}
                    or not comparison or not comparison.get('comparable')):
                raise ValueError('Comparison requires two complete comparable arms before publication')
        if status == 'succeeded':
            identifiers = []
            for output in completed:
                actual = output.get('config') or output.get('model_manifest', {}).get('config') or item['config']
                expected = dict(item['config'])
                if item.get('mode') == 'comparison':
                    expected['news'] = output.get('variant') == 'price_news'
                    if output.get('variant') not in {'price_only', 'price_news'}:
                        raise ValueError('Unknown comparison arm')
                if actual != expected:
                    raise ValueError('Training output differs from the frozen task configuration')
                frozen = {**output, 'strategy_id': item['strategy_id'], 'job_id': key,
                          'revision': item['revision'], 'config': actual,
                          'config_sha256': digest(actual), 'source_config_sha256': item['config_sha256'],
                          'created_at': now()}
                conn.execute('INSERT INTO candidates VALUES(?,?)', (frozen['id'], dumps(frozen)))
                identifiers.append(frozen['id'])
            item['candidate_ids'] = identifiers
            item['candidate_id'] = identifiers[0] if len(identifiers) == 1 else None
        if comparison is not None:
            item['comparison'] = comparison
        if item.get('comparison') and status != 'succeeded':
            item['comparison'] = {**item['comparison'], 'status': status, 'comparable': False, 'delta': None}
            for arm in item['comparison'].get('arms', {}).values():
                if arm.get('status') in {'running', 'pending'}:
                    arm.update(status=('not_run' if arm['status'] == 'pending' else
                                       'cancelled' if status == 'cancelled' else 'failed'),
                               error=error or ('Comparison cancelled' if status == 'cancelled' else 'Comparison interrupted'))
        item.update(status=status, phase=status, error=error, finished_at=now(), process=None)
        save(conn, 'jobs', item)
        return item


def select_candidate(strategy_id, candidate_id):
    with connect() as conn:
        strategy = read(conn, 'strategies', strategy_id)
        candidate = read(conn, 'candidates', candidate_id)
        if candidate['strategy_id'] != strategy_id:
            raise ValueError('Candidate belongs to another strategy')
        if candidate.get('source_config_sha256', candidate['config_sha256']) != digest(strategy['config']):
            raise ValueError('Candidate belongs to an earlier configuration; train this saved revision first')
        strategy.update(selected_candidate_id=candidate_id, updated_at=now())
        save(conn, 'strategies', strategy)
        return strategy


def operation(conn, token, name, payload):
    row = conn.execute('SELECT * FROM operations WHERE token=?', (token,)).fetchone()
    if row:
        if row['operation'] != name or row['payload'] != dumps(payload):
            raise ValueError('Idempotency-Key belongs to another request')
        return json.loads(row['response'])
    return None


def complete_operation(conn, token, name, payload, response):
    conn.execute('INSERT INTO operations VALUES(?,?,?,?)', (token, name, dumps(payload), dumps(response)))


def public_job(item):
    result = {key: value for key, value in item.items() if key != 'process'}
    if item['status'] in {'running', 'cancelling'}:
        try:
            path = job_directory(item['id']) / 'news-progress.json'
            if path.is_file():
                result['news_progress'] = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    return result
