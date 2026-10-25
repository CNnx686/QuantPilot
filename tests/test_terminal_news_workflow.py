from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from quantpilot import settings, terminal_store as store, terminal_worker as worker, terminal_comparison
from quantpilot.terminal_api import StrategyInput, create_router

REAL_PREDICTION_FOR = worker.prediction_for


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    app = FastAPI()
    app.include_router(create_router())
    closed = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    market = {'symbol': 'BTC/USDT', 'source': 'test', 'observed_at': closed.isoformat(),
        'stale': False, 'error': None, 'ticker': {'last': 100., 'ask': 101., 'bid': 99., 'timestamp': time.time() * 1000},
        'candles': [{'date': (closed-timedelta(hours=1)).isoformat(), 'closed_at': closed.isoformat(),
                    'open': 100., 'high': 105., 'low': 95., 'close': 100., 'volume': 10.}],
        'last_closed_at': closed.isoformat()}
    prediction = {'action': 'buy', 'valid': True, 'reason': 'test_price_news_model', 'prediction': .01,
                  'decision_at': closed.isoformat(), 'feature_available_at': closed.isoformat(),
                  'news': {'schema': 'test', 'coverage': {'status': 'covered'}, 'values': {'news_count_24h': 0.},
                           'event_ids': [], 'cutoff': closed.isoformat()}}
    monkeypatch.setattr(worker, 'market_snapshot', lambda *args, **kwargs: deepcopy(market))
    monkeypatch.setattr(worker, 'verify_candidate', lambda *args: {})
    monkeypatch.setattr(worker, 'prediction_for', lambda *args: deepcopy(prediction))
    return TestClient(app), market, prediction


def saved_strategy(news=True):
    config = StrategyInput(kind='prediction', news=news, start='2025-01-01', end='2025-01-15').model_dump(mode='json')
    return store.create_strategy(config)


def outputs(item, job):
    result = []
    for variant in ('price_only', 'price_news'):
        config = deepcopy(item['config'])
        config['news'] = variant == 'price_news'
        result.append({'id': store.identifier('model'), 'variant': variant, 'kind': config['kind'], 'symbol': config['symbol'],
            'artifact_dir': str(store.job_directory(job['id']) / ('model-news' if config['news'] else 'model-price')),
            'model_manifest': {'config': config, 'features': ['price', 'news'] if config['news'] else ['price'],
                               'files': {'snapshot.feather': 'same-price-snapshot'}},
            'metrics': {'net_return': -.2 if config['news'] else -.1, 'max_drawdown': .2, 'trades': 1},
            'period': {'train': {'start': '2025-01-01', 'end': '2025-01-10'}, 'evaluation': {'start': '2025-01-10', 'end': '2025-01-15'}}})
    return result


def pair(item):
    job, _ = store.enqueue(item['id'], 'compare', 'comparison')
    store.claim()
    candidates = outputs(item, job)
    summary = terminal_comparison.complete(terminal_comparison.initial(item['config']), candidates)
    store.finish(job['id'], candidates=candidates, comparison=summary)
    return job, [store.get('candidates', candidate['id']) for candidate in candidates]


def test_news_inputs_and_comparison_route_are_bounded_and_idempotent(setup):
    client, _, _ = setup
    valid = {'kind': 'rl', 'symbol': 'ETH/USDT', 'news': True, 'start': '2025-01-01', 'end': '2025-01-15'}
    response = client.post('/api/terminal/strategies', json=valid)
    assert response.status_code == 201
    for change in ({'kind': 'rule'}, {'symbol': 'SOL/USDT'}, {'end': '2025-03-01'}):
        assert client.post('/api/terminal/strategies', json={**valid, **change}).status_code == 422
    endpoint = f"/api/terminal/strategies/{response.json()['id']}"
    first = client.post(endpoint+'/comparisons', headers={'Idempotency-Key': 'same'})
    assert first.status_code == 201 and first.json()['mode'] == 'comparison'
    assert client.post(endpoint+'/comparisons', headers={'Idempotency-Key': 'same'}).json()['id'] == first.json()['id']
    assert client.post(endpoint+'/runs', headers={'Idempotency-Key': 'same'}).status_code == 409


def test_complete_pair_publishes_both_configs_and_baseline_starts_without_news(setup):
    _, _, prediction = setup
    item = saved_strategy()
    job, candidates = pair(item)
    result = store.get('jobs', job['id'])
    assert result['status'] == 'succeeded' and len(result['candidate_ids']) == 2
    assert result['comparison']['delta']['net_return'] == pytest.approx(-.1)
    assert candidates[0]['config']['news'] is False and candidates[1]['config']['news'] is True
    assert candidates[0]['source_config_sha256'] == candidates[1]['source_config_sha256'] == store.digest(item['config'])
    store.select_candidate(item['id'], candidates[0]['id'])
    instance, _ = worker.create_instance(item['id'], candidates[0]['id'], 'start-price')
    assert instance['config']['news'] is False
    assert instance['candidate_id'] == candidates[0]['id']
    assert store.get('strategies', item['id'])['config']['news'] is True


def test_partial_or_cancelled_pair_never_publishes_half_a_model_group(setup):
    item = saved_strategy()
    job, _ = store.enqueue(item['id'], 'partial', 'comparison')
    store.claim()
    candidates = outputs(item, job)
    partial = terminal_comparison.initial(item['config'])
    partial['arms']['price_only'] = terminal_comparison.arm_result(candidates[0])
    partial['arms']['price_news'] = {'status': 'failed', 'error': 'news missing'}
    store.finish(job['id'], error='news missing', candidates=candidates[:1], comparison=partial)
    assert store.list_records('candidates') == []
    assert store.get('jobs', job['id'])['comparison']['arms']['price_only']['metrics']['net_return'] == -.1
    job2, _ = store.enqueue(item['id'], 'cancel', 'comparison')
    store.claim()
    store.cancel(job2['id'])
    complete = terminal_comparison.complete(partial, candidates)
    store.finish(job2['id'], candidates=candidates, comparison=complete)
    assert store.list_records('candidates') == []
    assert store.get('jobs', job2['id'])['status'] == 'cancelled'


def test_comparison_rejects_different_prices_and_does_not_publish(setup):
    item = saved_strategy()
    job, _ = store.enqueue(item['id'], 'bad-data', 'comparison')
    store.claim()
    candidates = outputs(item, job)
    candidates[1]['model_manifest']['files']['snapshot.feather'] = 'different'
    summary = terminal_comparison.complete(terminal_comparison.initial(item['config']), candidates)
    assert not summary['comparable'] and summary['delta'] is None
    with pytest.raises(ValueError, match='complete comparable'):
        store.finish(job['id'], candidates=candidates, comparison=summary)
    assert store.list_records('candidates') == []


def test_news_cold_start_and_same_bar_recovery_do_not_fabricate_or_duplicate_fills(setup):
    _, market, prediction = setup
    item = saved_strategy()
    _, candidates = pair(item)
    news = candidates[1]
    store.select_candidate(item['id'], news['id'])
    prediction.update(valid=False, action='hold', reason='news_coverage_missing')
    prediction['news']['coverage'] = {'status': 'missing'}
    instance, _ = worker.create_instance(item['id'], news['id'], 'cold-start')
    assert instance['status'] == 'blocked' and instance['news_blocked']
    assert instance['orders'] == [] and instance['last_decision_at'] is None
    again = worker.tick(instance['id'])
    assert len(again['decisions']) == 1 and again['decisions'][0]['consumed'] is False
    prediction.update(valid=True, action='buy', reason='news_model')
    prediction['news']['coverage'] = {'status': 'covered'}
    recovered = worker.tick(instance['id'])
    assert recovered['status'] == 'running' and not recovered['news_blocked']
    assert len(recovered['orders']) == 1
    assert worker.tick(instance['id'])['orders'] == recovered['orders']


def test_news_failure_still_executes_existing_stop_and_consumes_that_bar(setup):
    _, market, prediction = setup
    item = saved_strategy()
    _, candidates = pair(item)
    store.select_candidate(item['id'], candidates[1]['id'])
    instance, _ = worker.create_instance(item['id'], candidates[1]['id'], 'start')
    next_time = (datetime.fromisoformat(prediction['decision_at']) + timedelta(hours=1)).isoformat()
    prediction.update(valid=False, action='hold', reason='news_source_error', decision_at=next_time)
    prediction['news']['coverage'] = {'status': 'source_error'}
    market['candles'].append({**market['candles'][-1], 'date': market['last_closed_at'], 'closed_at': next_time, 'close': 90.})
    market['last_closed_at'] = next_time
    stopped = worker.tick(instance['id'])
    assert stopped['position'] is None and stopped['status'] == 'blocked'
    assert stopped['orders'][-1]['reason'] == 'closed_candle_stop_loss'
    assert stopped['last_decision_at'] == next_time
    prediction.update(valid=True, action='buy', reason='news_model')
    prediction['news']['coverage'] = {'status': 'covered'}
    restored = worker.tick(instance['id'])
    assert restored['position'] is None and len(restored['orders']) == 2
    assert restored['status'] == 'blocked' and restored['news_resume_required']
    resumed = worker.action(instance['id'], 'resume', 'wait', 'resume-after-news')
    assert resumed['status'] == 'running' and not resumed['news_resume_required']


def test_news_recovery_preserves_explicit_pause(setup):
    _, _, prediction = setup
    item = saved_strategy()
    _, candidates = pair(item)
    store.select_candidate(item['id'], candidates[1]['id'])
    prediction.update(valid=False, action='hold', reason='news_coverage_missing')
    instance, _ = worker.create_instance(item['id'], candidates[1]['id'], 'cold')
    worker.action(instance['id'], 'pause', 'wait', 'pause')
    prediction.update(valid=True, action='buy', reason='news_model')
    recovered = worker.tick(instance['id'])
    assert recovered['status'] == 'paused' and recovered['orders'] == []


def test_after_success_news_failure_requires_manual_resume_after_coverage_recovers(setup):
    _, market, prediction = setup
    item = saved_strategy()
    _, candidates = pair(item)
    store.select_candidate(item['id'], candidates[1]['id'])
    prediction['action'] = 'hold'
    instance, _ = worker.create_instance(item['id'], candidates[1]['id'], 'start')
    assert instance['status'] == 'running' and instance['news_has_run']
    prediction.update(valid=False, reason='news_source_error')
    prediction['news']['coverage'] = {'status': 'source_error'}
    failure = worker.tick(instance['id'])
    assert failure['news_resume_required'] and failure['status'] == 'blocked'
    with pytest.raises(ValueError, match='news_source_error'):
        worker.action(instance['id'], 'resume', 'wait', 'too-soon')
    next_time = (datetime.fromisoformat(prediction['decision_at'])+timedelta(hours=1)).isoformat()
    market['candles'].append({**market['candles'][-1], 'date': market['last_closed_at'], 'closed_at': next_time})
    market['last_closed_at'] = next_time
    prediction.update(valid=True, action='buy', reason='news_model', decision_at=next_time)
    prediction['news']['coverage'] = {'status': 'covered'}
    recovered = worker.tick(instance['id'])
    assert recovered['status'] == 'blocked' and recovered['error'] == 'news_resume_required'
    assert recovered['position'] is None and recovered['orders'] == []
    assert recovered['decisions'][-1]['raw_action'] == 'buy'
    assert recovered['decisions'][-1]['reason'] == 'news_resume_required'
    resumed = worker.action(instance['id'], 'resume', 'wait', 'confirm-recovery')
    assert resumed['status'] == 'running' and resumed['entry_enabled']
    assert not resumed['news_resume_required']


@pytest.mark.parametrize('problem', [ValueError('News factor schema differs'),
    ValueError('Current news feature policy differs'), ValueError('News factor columns differ'), KeyError('values')])
def test_malformed_news_input_requires_manual_resume_and_retains_diagnostic(setup, monkeypatch, problem):
    from quantpilot import terminal_news, terminal_news_engine
    _, _, prediction = setup
    item = saved_strategy()
    _, candidates = pair(item)
    store.select_candidate(item['id'], candidates[1]['id'])
    prediction['action'] = 'hold'
    instance, _ = worker.create_instance(item['id'], candidates[1]['id'], 'start')
    monkeypatch.setattr(worker, 'prediction_for', REAL_PREDICTION_FOR)
    monkeypatch.setattr(terminal_news, 'current_snapshot', lambda **kwargs: {})
    def invalid(*args):
        raise problem
    monkeypatch.setattr(terminal_news_engine, 'predict', invalid)
    failed = worker.tick(instance['id'])
    assert failed['error'] == 'news_input_error'
    assert str(problem) in failed['error_detail']
    assert failed['news_resume_required'] and failed['status'] == 'blocked'
    with pytest.raises(worker.NewsInputError):
        worker.action(instance['id'], 'resume', 'wait', 'invalid-resume')
    monkeypatch.setattr(terminal_news_engine, 'predict', lambda *args: deepcopy(prediction))
    recovered = worker.tick(instance['id'])
    assert recovered['status'] == 'blocked' and recovered['error'] == 'news_resume_required'
    assert recovered['news_resume_required']
    assert str(problem) in recovered['error_detail']
    resumed = worker.action(instance['id'], 'resume', 'wait', 'manual-resume')
    assert resumed['status'] == 'running' and not resumed['news_resume_required']
    assert str(problem) in resumed['news_last_error']['detail']


def test_news_model_hash_errors_keep_their_specific_diagnostic(setup, monkeypatch):
    _, _, prediction = setup
    item = saved_strategy()
    _, candidates = pair(item)
    store.select_candidate(item['id'], candidates[1]['id'])
    prediction['action'] = 'hold'
    instance, _ = worker.create_instance(item['id'], candidates[1]['id'], 'start')
    monkeypatch.setattr(worker, 'prediction_for', REAL_PREDICTION_FOR)
    def damaged(*args):
        raise ValueError('Frozen news model artifact hash mismatch')
    monkeypatch.setattr(worker, 'verify_candidate', damaged)
    failed = worker.tick(instance['id'])
    assert failed['error'] == 'Frozen news model artifact hash mismatch'
    assert failed['error'] != 'news_input_error' and failed['status'] == 'blocked'
