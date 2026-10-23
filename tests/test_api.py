"""API contract tests with a mocked dataset manifest; not real backtest evidence."""
import pytest
from fastapi.testclient import TestClient

from quantpilot import api, settings


@pytest.mark.parametrize('failure', ['startup', 'shutdown'])
def test_lifespan_cleans_up_all_started_services_on_failure(monkeypatch, failure):
    import asyncio
    from quantpilot import live_control
    calls = []

    def operation(name, fail=False):
        def run():
            calls.append(name)
            if fail:
                raise RuntimeError('fixture lifecycle failure')
        return run

    monkeypatch.setattr(live_control, 'start_heartbeat', operation('live-start'))
    monkeypatch.setattr(live_control, 'stop_heartbeat', operation('live-stop'))
    monkeypatch.setattr(api.public_worker, 'start', operation('public-start', failure == 'startup'))
    monkeypatch.setattr(api.public_worker, 'stop', operation('public-stop'))
    monkeypatch.setattr(api.rl_worker, 'start', operation('rl-start'))
    monkeypatch.setattr(api.rl_worker, 'stop', operation('rl-stop', failure == 'shutdown'))

    async def run():
        async with api.lifespan(api.app):
            calls.append('served')

    with pytest.raises(RuntimeError, match='fixture lifecycle failure'):
        asyncio.run(run())
    if failure == 'startup':
        assert calls == ['live-start', 'public-start', 'public-stop', 'live-stop']
    else:
        assert calls == ['live-start', 'public-start', 'rl-start', 'served',
                         'rl-stop', 'public-stop', 'live-stop']


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB", tmp_path / "queue.sqlite3")
    monkeypatch.setattr(settings, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(api, "get_dataset", lambda: {
        "id": "test-only", "start": "2024-12-01", "end": "2025-04-01",
        "available_start": "2025-01-01", "available_end": "2025-04-01"})
    return TestClient(api.app)


def submit(client, body=None, key="request-1"):
    return client.post("/api/experiments", json=body or {}, headers={"Idempotency-Key": key})


def test_enqueue_idempotency_and_unknown_id(client):
    first = submit(client)
    assert first.status_code == 201
    job = first.json()
    assert job["status"] == "queued"
    assert submit(client).json()["id"] == job["id"]
    assert submit(client, {"fee": 0.002}).status_code == 409
    assert client.get("/api/experiments").json() == [job]
    assert client.get("/api/experiments/unknown").status_code == 404
    assert client.get("/api/does-not-exist").status_code == 404


@pytest.mark.parametrize("body", [
    {"pairs": ["DOGE/USDT"]}, {"pairs": ["BTC/USDT", "BTC/USDT"]},
    {"start": "invalid"}, {"start": "2025-03-01", "end": "2025-02-01"},
    {"start": "2023-01-01"}, {"start": "2024-12-01"},
    {"wallet": 10, "stake": 20}, {"wallet": "NaN"}, {"stake": "Infinity"},
    {"fee": "NaN"}, {"fee": -0.01}, {"strategy": "ArbitraryCode"}, {"name": "  "},
])
def test_invalid_configuration(client, body):
    assert submit(client, body).status_code == 422
    assert client.get("/api/experiments").json() == []


def test_missing_data_key_and_external_origin(client, monkeypatch):
    assert client.post("/api/experiments", json={}).status_code == 422
    assert client.post("/api/experiments", json={}, headers={"Origin": "https://evil.example", "Idempotency-Key": "key"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
    monkeypatch.setattr(api, "get_dataset", lambda: None)
    assert submit(client).status_code == 409


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_numbers_are_rejected(client, value):
    response = client.post("/api/experiments", content='{"wallet":' + value + '}',
                           headers={"Content-Type": "application/json", "Idempotency-Key": "nonfinite"})
    assert response.status_code == 422


def test_artifacts_allowlist_and_logs(client):
    job = submit(client).json()
    directory = settings.ARTIFACTS / job["id"]
    directory.mkdir(parents=True)
    (directory / "engine.log").write_text("engine diagnostic", encoding="utf-8")
    (directory / "secret.txt").write_text("not exported", encoding="utf-8")
    (directory / "raw-result.zip").write_bytes(b"test bytes")
    prefix = f"/api/experiments/{job['id']}"
    assert client.get(prefix + "/logs").json() == {"text": "engine diagnostic"}
    files = client.get(prefix + "/artifacts").json()
    assert {item["name"] for item in files} == {"engine.log", "raw-result.zip"}
    assert all(len(item["sha256"]) == 64 for item in files)
    assert client.get(prefix + "/artifacts/secret.txt").status_code == 404
    assert client.get(prefix + "/artifacts/%2e%2e%2fsecret.txt").status_code == 404
    assert client.get(prefix + "/artifacts/raw-result.zip").content == b"test bytes"


def test_unavailable_existing_manifest(client, monkeypatch):
    monkeypatch.setattr(api, "get_dataset", lambda: {"id": "missing-files", "available": False})
    assert client.get("/api/health").json()["dataset"]["available"] is False
    assert submit(client).status_code == 409


def test_provenance_is_downloadable(client):
    job = submit(client).json()
    directory = settings.ARTIFACTS / job["id"]
    directory.mkdir(parents=True)
    (directory / "provenance.json").write_text('{"source":"synthetic-test"}')
    prefix = f"/api/experiments/{job['id']}/artifacts"
    assert client.get(prefix).json()[0]["name"] == "provenance.json"
    assert client.get(prefix + "/provenance.json").json()["source"] == "synthetic-test"


def test_retry_returns_existing_job_even_after_data_disappears(client, monkeypatch):
    job = submit(client).json()
    monkeypatch.setattr(api, "get_dataset", lambda: None)
    retry = submit(client)
    assert retry.status_code == 200
    assert retry.json()["id"] == job["id"]
    assert submit(client, {"fee": 0.002}).status_code == 409


def test_bound_diagnostic_exposes_comparison_evidence_only(client):
    import hashlib
    import json
    job = submit(client).json()
    directory = settings.ARTIFACTS / job['id']
    directory.mkdir(parents=True)
    result = b'{}'
    (directory / 'result.json').write_bytes(result)
    diag = settings.ARTIFACTS / 'diagnostic-boundary-test'
    diag.mkdir()
    (diag / 'diagnostic.json').write_text(json.dumps({'job_id': job['id'],
        'source_result_sha256': hashlib.sha256(result).hexdigest()}))
    (diag / 'comparison-boundaries.jsonl').write_text('{"in_window_difference_rows":0}\n')
    prefix = f"/api/experiments/{job['id']}/diagnostics"
    files = client.get(prefix).json()[0]['files']
    assert 'comparison-boundaries.jsonl' in {f['name'] for f in files}
    assert client.get(prefix + '/diagnostic-boundary-test/comparison-boundaries.jsonl').status_code == 200
    assert client.get(prefix + '/diagnostic-boundary-test/effective-config.json').status_code == 404
