from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from quantpilot import settings, terminal_store as store, terminal_worker as worker
from quantpilot.terminal_api import StrategyInput, create_router


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    app = FastAPI()
    app.include_router(create_router())
    client = TestClient(app)
    closed = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    market = {'symbol': 'BTC/USDT', 'source': 'test public quote', 'observed_at': closed.isoformat(),
        'stale': False, 'error': None, 'ticker': {'last': 100., 'ask': 101., 'bid': 99., 'timestamp': time.time() * 1000},
        'candles': [{'date': (closed - timedelta(hours=1)).isoformat(), 'closed_at': closed.isoformat(),
                     'open': 100., 'high': 105., 'low': 95., 'close': 100., 'volume': 100.}],
        'last_closed_at': closed.isoformat()}
    predicted = {'action': 'buy', 'valid': True, 'reason': 'test_model_signal', 'prediction': None,
                 'decision_at': closed.isoformat(), 'feature_available_at': closed.isoformat()}
    monkeypatch.setattr(worker, 'market_snapshot', lambda *args, **kwargs: deepcopy(market))
    monkeypatch.setattr(worker, 'verify_candidate', lambda candidate: {})
    monkeypatch.setattr(worker, 'prediction_for', lambda *args: deepcopy(predicted))
    return client, market, predicted


def create_candidate(client, name='one', token='train'):
    item = client.post('/api/terminal/strategies', json={'name': name}).json()
    response = client.post(f"/api/terminal/strategies/{item['id']}/runs", headers={'Idempotency-Key': token})
    assert response.status_code == 201
    job = response.json()
    assert store.claim()['id'] == job['id']
    frozen = {'id': store.identifier('model'), 'kind': 'rule', 'symbol': 'BTC/USDT',
              'metrics': {'net_return': -.1, 'max_drawdown': .1, 'trades': 0},
              'artifact_dir': str(store.job_directory(job['id']) / 'model')}
    store.finish(job['id'], candidate=frozen)
    selected = client.post(f"/api/terminal/strategies/{item['id']}/select", json={'candidate_id': frozen['id']})
    assert selected.status_code == 200
    return selected.json(), frozen


def start(client, item, candidate, key='start'):
    response = client.post('/api/terminal/instances', json={'strategy_id': item['id'], 'candidate_id': candidate['id']},
                           headers={'Idempotency-Key': key})
    assert response.status_code == 201, response.text
    return response.json()


def action(client, instance, name, token, mode='wait'):
    return client.post(f"/api/terminal/instances/{instance['id']}/action", json={'action': name, 'mode': mode},
                       headers={'Idempotency-Key': token})


def advance(market, predicted, requested='buy', close=100.):
    old = datetime.fromisoformat(predicted['decision_at'])
    following = old + timedelta(hours=1)
    predicted.update(action=requested, decision_at=following.isoformat(), feature_available_at=following.isoformat())
    market['candles'].append({**market['candles'][-1], 'date': old.isoformat(), 'closed_at': following.isoformat(), 'close': close})
    market['last_closed_at'] = following.isoformat()


def test_inputs_and_empty_bootstrap(environment):
    client, _, _ = environment
    result = client.get('/api/terminal/bootstrap').json()
    assert result['instances'] == result['candidates'] == []
    assert not result['capabilities']['live_trading']
    assert result['capabilities']['execution_engine'] == 'local_paper_v1'
    for config in ({'start': '2025-10-01', 'end': '2026-01-01'}, {'news': True},
                   {'wallet': 1000, 'stake': 1000}, {'parameters': {'fast': 48, 'slow': 12}},
                   {'parameters': {'rl_steps': 999999}}, {'symbol': '../../BTC'}, {'name': ' '}):
        assert client.post('/api/terminal/strategies', json=config).status_code == 422


def test_strategy_creation_retries_are_idempotent_when_key_supplied(environment):
    client, _, _ = environment
    headers = {'Idempotency-Key': 'create-retry'}
    one = client.post('/api/terminal/strategies', json={'name': 'retry'}, headers=headers)
    two = client.post('/api/terminal/strategies', json={'name': 'retry'}, headers=headers)
    assert one.json()['id'] == two.json()['id']
    assert len(store.list_records('strategies')) == 1
    assert client.post('/api/terminal/strategies', json={'name': 'different'}, headers=headers).status_code == 409


def test_create_train_select_start_uses_quote_and_deduplicates(environment):
    client, market, predicted = environment
    item, candidate = create_candidate(client)
    instance = start(client, item, candidate)
    assert instance['status'] == 'running'
    assert instance['position']['entry_price'] == market['ticker']['ask']
    assert instance['cash'] == pytest.approx(8999.)
    assert len(instance['orders']) == len(instance['decisions']) == 1
    again = client.post('/api/terminal/instances', json={'strategy_id': item['id'], 'candidate_id': candidate['id']},
                        headers={'Idempotency-Key': 'start'})
    assert again.status_code == 200
    assert again.json()['id'] == instance['id']
    result = action(client, instance, 'tick', 'tick-a').json()
    assert len(result['orders']) == len(result['decisions']) == 1
    duplicate = client.post('/api/terminal/instances', json={'strategy_id': item['id'], 'candidate_id': candidate['id']},
                            headers={'Idempotency-Key': 'other-start'})
    assert duplicate.status_code == 409
    advance(market, predicted, requested='sell')
    result = action(client, instance, 'tick', 'tick-b').json()
    assert result['position'] is None
    assert result['orders'][-1]['price'] == market['ticker']['bid']
    assert result['equity'] == pytest.approx(result['cash'])
    assert result['realized_pnl'] < 0


def test_pause_preserves_position_and_close_based_risk(environment):
    client, market, predicted = environment
    item, candidate = create_candidate(client)
    instance = start(client, item, candidate)
    paused = action(client, instance, 'pause', 'pause').json()
    assert paused['status'] == 'paused' and paused['position']
    advance(market, predicted, requested='buy', close=90.)
    result = action(client, instance, 'tick', 'risk').json()
    assert result['position'] is None
    assert result['decisions'][-1]['raw_action'] == 'buy'
    assert result['decisions'][-1]['reason'] == 'closed_candle_stop_loss'
    advance(market, predicted)
    result = action(client, instance, 'tick', 'paused-new').json()
    assert result['position'] is None
    assert result['decisions'][-1]['reason'] == 'new_entries_paused'


def test_stop_waits_for_existing_exit_and_does_not_reset_cash(environment):
    client, market, predicted = environment
    item, candidate = create_candidate(client)
    instance = start(client, item, candidate)
    waiting = action(client, instance, 'stop', 'wait').json()
    assert waiting['status'] == 'waiting_stop' and waiting['position']
    assert action(client, instance, 'resume', 'bad-resume').status_code == 409
    advance(market, predicted, requested='sell')
    stopped = action(client, instance, 'tick', 'exit').json()
    assert stopped['status'] == 'stopped' and not stopped['position']
    assert stopped['cash'] != stopped['initial_balance']
    fresh = start(client, item, candidate, 'restart')
    assert fresh['id'] != instance['id']
    assert fresh['initial_balance'] == 10000
    assert client.get(f"/api/terminal/instances/{instance['id']}").json()['status'] == 'stopped'


def test_market_failure_blocks_fills_and_close_retry_is_explicit(environment):
    client, market, predicted = environment
    item, candidate = create_candidate(client)
    instance = start(client, item, candidate)
    market.update(stale=True, error='public_exchange_request_failed')
    blocked = action(client, instance, 'tick', 'blocked').json()
    assert blocked['status'] == 'blocked' and blocked['position']
    assert len(blocked['orders']) == 1
    assert action(client, instance, 'close_position', 'close').status_code == 409
    market.update(stale=False, error=None)
    closed = action(client, instance, 'close_position', 'close').json()
    assert closed['position'] is None and len(closed['orders']) == 2
    assert action(client, instance, 'close_position', 'close').json() == closed
    assert action(client, instance, 'pause', 'close').status_code == 409


def test_two_instances_have_independent_cash_and_control(environment):
    client, _, _ = environment
    first, c1 = create_candidate(client)
    second, c2 = create_candidate(client, 'two', 'train-two')
    one, two = start(client, first, c1), start(client, second, c2, 'start-two')
    stopped = action(client, one, 'stop', 'close-stop', mode='close').json()
    assert stopped['status'] == 'stopped'
    untouched = client.get(f"/api/terminal/instances/{two['id']}").json()
    assert untouched == two
    assert untouched['position'] and untouched['cash'] == 8999


def test_training_cancel_and_revision_api(environment):
    client, _, _ = environment
    item = client.post('/api/terminal/strategies', json={}).json()
    job = client.post(f"/api/terminal/strategies/{item['id']}/runs", headers={'Idempotency-Key': 'train'}).json()
    assert 'process' not in job
    assert client.post(f"/api/terminal/jobs/{job['id']}/cancel").json()['status'] == 'cancelled'
    changed = {**item['config'], 'name': 'revision2'}
    assert client.patch(f"/api/terminal/strategies/{item['id']}", json={'revision': 1, 'config': changed}).json()['revision'] == 2
    assert client.patch(f"/api/terminal/strategies/{item['id']}", json={'revision': 1, 'config': changed}).status_code == 409
    assert store.get('jobs', job['id'])['config']['name'] != 'revision2'


def test_no_timestamp_cannot_start_even_when_network_success(environment):
    client, market, _ = environment
    item, candidate = create_candidate(client)
    market['ticker']['timestamp'] = None
    result = client.post('/api/terminal/instances', json={'strategy_id': item['id'], 'candidate_id': candidate['id']},
                         headers={'Idempotency-Key': 'bad-start'})
    assert result.status_code == 409
    assert store.list_records('instances') == []


def test_close_stop_fetches_quote_even_if_position_opens_after_initial_read(environment, monkeypatch):
    client, market, predicted = environment
    predicted['action'] = 'hold'
    item, candidate = create_candidate(client)
    instance = start(client, item, candidate)
    assert instance['position'] is None
    def intervening_tick(*args, **kwargs):
        with store.connect() as conn:
            current = store.read(conn, 'instances', instance['id'])
            worker.fill(current, 'buy', market, 'concurrent_paper_signal')
            store.save(conn, 'instances', current)
        return deepcopy(market)
    monkeypatch.setattr(worker, 'market_snapshot', intervening_tick)
    result = action(client, instance, 'stop', 'racing-stop', mode='close')
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'stopped'
    assert result.json()['position'] is None
    assert [order['side'] for order in result.json()['orders']] == ['buy', 'sell']


def test_concurrent_ticks_commit_one_decision_and_one_fill(environment):
    client, market, predicted = environment
    item, candidate = create_candidate(client)
    instance = start(client, item, candidate)
    advance(market, predicted, requested='sell')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: worker.tick(instance['id']), range(4)))
    saved = store.get('instances', instance['id'])
    assert len(saved['decisions']) == 2
    assert [order['side'] for order in saved['orders']] == ['buy', 'sell']
    assert saved['position'] is None


def test_strategy_with_any_account_history_must_be_copied_before_edit(environment):
    client, _, _ = environment
    item, candidate = create_candidate(client)
    instance = start(client, item, candidate)
    change = {'revision': item['revision'], 'config': {**item['config'], 'symbol': 'ETH/USDT'}}
    endpoint = f"/api/terminal/strategies/{item['id']}"
    running_edit = client.patch(endpoint, json=change)
    assert running_edit.status_code == 409 and 'copy' in running_edit.json()['detail']
    assert action(client, instance, 'stop', 'stop-for-edit', mode='close').json()['status'] == 'stopped'
    historical_edit = client.patch(endpoint, json=change)
    assert historical_edit.status_code == 409
    assert store.get('strategies', item['id'])['config']['symbol'] == 'BTC/USDT'
    assert store.get('instances', instance['id'])['symbol'] == 'BTC/USDT'
