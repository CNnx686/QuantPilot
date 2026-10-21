import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from quantpilot import settings, rl_store, rl_api
from quantpilot.data import sha256


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(settings, 'ARTIFACTS', tmp_path / 'artifacts')
    monkeypatch.setattr(rl_api, 'readiness', lambda: {'available': True})
    app = FastAPI()
    app.include_router(rl_api.create_router())
    return TestClient(app)


def test_api_bounds_idempotency_cancel(client):
    assert client.post('/api/rl/jobs', json={'steps': 10000000}, headers={'Idempotency-Key': 'a'}).status_code == 422
    assert client.post('/api/rl/jobs', json={'protocol': 'arbitrary'}, headers={'Idempotency-Key': 'a'}).status_code == 422
    first = client.post('/api/rl/jobs', json={}, headers={'Idempotency-Key': 'a'})
    assert first.status_code == 201
    identifier = first.json()['id']
    assert client.post('/api/rl/jobs', json={}, headers={'Idempotency-Key': 'a'}).status_code == 200
    assert client.post('/api/rl/jobs', json={}, headers={'Idempotency-Key': 'b'}).status_code == 409
    assert client.get(f'/api/rl/jobs/{identifier}/results').status_code == 409
    assert client.post(f'/api/rl/jobs/{identifier}/cancel').json()['status'] == 'cancelled'
    assert client.get('/api/rl/jobs/missing').status_code == 404


def test_artifact_and_result_integrity(client):
    job, _ = rl_store.enqueue('test', 'a')
    rl_store.claim()
    path = rl_store.directory(job['id'])
    path.mkdir(parents=True)
    result = path / 'result.json'
    result.write_text('{"rows": []}')
    (path / 'inventory.json').write_text(json.dumps({'result.json': {'sha256': sha256(result)}}))
    rl_store.finish(job['id'], 'succeeded')
    base = '/api/rl/jobs/' + job['id']
    assert client.get(base + '/results').status_code == 200
    assert client.get(base + '/artifacts/result.json').status_code == 200
    result.write_text('{}')
    assert client.get(base + '/results').status_code == 409
    assert client.get(base + '/artifacts/result.json').status_code == 409
    assert client.get(base + '/artifacts/%2e%2e/%2e%2e/private.txt').status_code == 404
    (path / 'accidental-secret.txt').write_text('synthetic test value')
    assert client.get(base + '/artifacts/accidental-secret.txt').status_code == 404
    assert all(item['path'] != 'accidental-secret.txt' for item in client.get(base + '/artifacts').json())


def test_unavailable_is_honest(client, monkeypatch):
    existing, _ = rl_store.enqueue('same', 'existing')
    monkeypatch.setattr(rl_api, 'readiness', lambda: {'available': False, 'reasons': ['Runtime missing']})
    assert client.post('/api/rl/jobs', json={'name': 'same'}, headers={'Idempotency-Key': 'existing'}).json()['id'] == existing['id']
    response = client.post('/api/rl/jobs', json={}, headers={'Idempotency-Key': 'a'})
    assert response.status_code == 409 and 'Runtime missing' in response.text
