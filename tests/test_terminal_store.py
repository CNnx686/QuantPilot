import sqlite3

import pytest

from quantpilot import settings, terminal_store as store
from quantpilot.terminal_api import StrategyInput


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)


def strategy(**values):
    return store.create_strategy(StrategyInput(**values).model_dump(mode='json'))


def candidate(item, token='job'):
    job, _ = store.enqueue(item['id'], token)
    assert store.claim()['id'] == job['id']
    value = {'id': store.identifier('candidate'), 'kind': item['config']['kind'],
             'symbol': item['config']['symbol'], 'metrics': {'net_return': -.2, 'max_drawdown': .2, 'trades': 0}}
    store.finish(job['id'], candidate=value)
    return store.get('candidates', value['id'])


def test_job_freezes_config_and_retry_key_does_not_duplicate():
    item = strategy()
    job, created = store.enqueue(item['id'], 'same')
    assert created
    changed = {**item['config'], 'name': 'changed'}
    updated = store.update_strategy(item['id'], 1, changed)
    assert updated['revision'] == 2
    assert store.get('jobs', job['id'])['config']['name'] != 'changed'
    assert store.enqueue(item['id'], 'same') == (job, False)
    with pytest.raises(ValueError, match='another request'):
        store.enqueue(strategy(name='other')['id'], 'same')
    with pytest.raises(ValueError, match='refresh'):
        store.update_strategy(item['id'], 1, changed)


def test_cancel_fences_candidate_publication_and_keeps_partial_artifacts():
    item = strategy()
    job, _ = store.enqueue(item['id'], 'queued')
    assert store.cancel(job['id'])['status'] == 'cancelled'
    assert store.claim() is None
    job, _ = store.enqueue(item['id'], 'running')
    store.claim()
    assert store.cancel(job['id'])['status'] == 'cancelling'
    store.finish(job['id'], candidate={'id': 'must-not-exist'})
    assert store.get('jobs', job['id'])['status'] == 'cancelled'
    assert store.list_records('candidates') == []
    assert (store.job_directory(job['id']) / 'CANCEL').is_file()


def test_candidates_are_immutable_negative_results_selectable_and_owned():
    item, other = strategy(), strategy(name='other')
    frozen = candidate(item)
    selected = store.select_candidate(item['id'], frozen['id'])
    assert selected['selected_candidate_id'] == frozen['id']
    assert frozen['metrics']['net_return'] < 0
    with pytest.raises(ValueError, match='another strategy'):
        store.select_candidate(other['id'], frozen['id'])
    with store.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute('UPDATE candidates SET record=? WHERE id=?', ('{}', frozen['id']))
    store.update_strategy(item['id'], 1, {**item['config'], 'wallet': 20000})
    with pytest.raises(ValueError, match='earlier configuration'):
        store.select_candidate(item['id'], frozen['id'])


def test_database_is_separate_and_job_paths_are_bounded(tmp_path):
    strategy()
    assert (tmp_path / 'data' / 'terminal.sqlite3').is_file()
    assert not (tmp_path / 'data' / 'quantpilot.sqlite3').exists()
    with pytest.raises(ValueError):
        store.job_directory('../../escape')


def test_worker_single_slot_and_failure_allows_retry():
    item = strategy()
    first, _ = store.enqueue(item['id'], 'first')
    second, _ = store.enqueue(item['id'], 'second')
    assert store.claim()['id'] == first['id']
    assert store.claim() is None
    store.finish(first['id'], error='download failed')
    assert store.claim()['id'] == second['id']
    assert store.get('jobs', first['id'])['error'] == 'download failed'


def test_restart_marks_interrupted_job_failed_without_requeue():
    from quantpilot import terminal_worker
    item = strategy()
    job, _ = store.enqueue(item['id'], 'interrupted')
    store.claim()
    terminal_worker.recover_training()
    saved = store.get('jobs', job['id'])
    assert saved['status'] == 'failed'
    assert 'restart' in saved['error']
    assert store.claim() is None
