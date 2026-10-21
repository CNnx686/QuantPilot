"""Workflow API contracts. All fixtures are synthetic and use isolated storage."""
import json

import pytest
from fastapi.testclient import TestClient

from quantpilot import api, settings, store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(settings, 'DB', tmp_path / 'queue.sqlite3')
    monkeypatch.setattr(settings, 'ARTIFACTS', tmp_path / 'artifacts')
    monkeypatch.setattr(api, 'get_dataset', lambda: {
        'id': 'test-data', 'available': True, 'start': '2025-01-01', 'end': '2025-10-01',
        'available_start': '2025-01-01', 'available_end': '2025-10-01'})
    return TestClient(api.app)


def spec(**changes):
    return dict(name='Test research', hypothesis='Trend baseline', pairs=['BTC/USDT'],
                start='2025-04-01', end='2025-07-01', wallet=10000, stake=1000,
                fee=0.001, strategy='SmaTrend', dataset_id='test-data',
                timeframe='1h', schema_version='1', **changes)


def draft(client, value=None):
    response = client.post('/api/research', json={'spec': value or spec(), 'source': 'manual'})
    assert response.status_code == 201
    return response.json()


def test_confirm_is_atomic_persistent_and_snapshot_conflicts(client, monkeypatch):
    saved = draft(client)
    body = {'revision': saved['revision'], 'spec': saved['spec']}
    path = '/api/research/' + saved['id']
    first = client.post(path + '/confirm', json=body)
    assert first.status_code == 201
    job = first.json()['experiment']
    assert job['config']['dataset_id'] == 'test-data'
    assert 'hypothesis' not in job['config']
    monkeypatch.setattr(api, 'get_dataset', lambda: None)
    retry = client.post(path + '/confirm', json=body)
    assert retry.status_code == 200
    assert retry.json()['experiment']['id'] == job['id']
    assert len(store.list_jobs()) == 1
    body['spec']['fee'] = .002
    assert client.post(path + '/confirm', json=body).status_code == 409
    assert client.put(path, json={'spec': saved['spec'], 'revision': 1}).status_code == 409
    assert client.get(path).json()['experiment_id'] == job['id']


def test_draft_edit_and_stale_confirmation(client):
    saved = draft(client)
    path = '/api/research/' + saved['id']
    changed = dict(saved['spec'], fee=.002)
    edited = client.put(path, json={'spec': changed, 'revision': 1})
    assert edited.status_code == 200
    assert edited.json()['revision'] == 2
    assert client.post(path + '/confirm', json={'revision': 1, 'spec': saved['spec']}).status_code == 409
    assert client.post(path + '/confirm', json={'revision': 2, 'spec': changed}).status_code == 201


@pytest.mark.parametrize('change', [
    {'strategy': 'ArbitraryCode'}, {'code': 'import os'}, {'timeframe': '5m'},
    {'start': '2025-01-01'}, {'end': '2026-01-01'}, {'wallet': 'NaN'},
    {'wallet': '10000'}, {'fee': True}, {'pairs': ['SOL/USDT']}, {'dataset_id': ''},
])
def test_invalid_research_specs(client, change):
    assert client.post('/api/research', json={'spec': dict(spec(), **change)}).status_code == 422
    assert client.get('/api/research').json() == []


def test_local_suggestion_is_explicit_and_rejects_unsupported(client):
    response = client.post('/api/research/suggest', json={'prompt': 'BTC SMA trend baseline'})
    assert response.status_code == 200
    assert response.json()['source'] == 'local_template'
    assert response.json()['spec']['strategy'] == 'SmaTrend'
    assert client.post('/api/research/suggest', json={'prompt': 'SOL 5m RSI strategy'}).status_code == 422
    metadata = client.get('/api/research/templates').json()
    assert metadata['online_llm']['configured'] is False


@pytest.fixture
def evidence(client, tmp_path, monkeypatch):
    import csv
    import hashlib
    import zipfile
    from datetime import datetime, timezone
    from quantpilot.data import sha256
    from quantpilot.engine import engine_config, parse_result, write_json

    root = tmp_path / 'source'
    monkeypatch.setattr(settings, 'ROOT', root)
    (root / 'strategies').mkdir(parents=True)
    (root / 'strategies' / 'SmaTrend.py').write_text('# synthetic strategy\n')
    record = draft(client)
    job = client.post('/api/research/' + record['id'] + '/confirm',
                      json={'revision': 1, 'spec': record['spec']}).json()['experiment']
    folder = settings.ARTIFACTS / job['id']
    (folder / 'strategies').mkdir(parents=True)
    (folder / 'market').mkdir()
    (folder / 'strategies' / 'SmaTrend.py').write_text('# synthetic strategy\n')
    market = folder / 'market' / 'BTC_USDT-1h.feather'
    market.write_bytes(b'synthetic market file')
    files = [{'pair': 'BTC/USDT', 'path': 'data/BTC_USDT-1h.feather', 'sha256': sha256(market)}]
    manifest = {'id': 'test-data', 'files': files,
                'sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    write_json(folder / 'manifest.json', manifest)
    write_json(folder / 'effective-config.json', engine_config(job['config'], folder))
    provenance = {'dataset_id': 'test-data', 'data_sha256': manifest['sha256'],
                  'strategy_sha256': sha256(folder / 'strategies' / 'SmaTrend.py'),
                  'config_sha256': sha256(folder / 'effective-config.json'),
                  'adapter_sha256': 'synthetic-adapter-hash', 'engine_version': '2026.9'}
    write_json(folder / 'provenance.json', provenance)
    trades = [dict(pair='BTC/USDT', open_date='2025-04-02 00:00:00+00:00', close_date='2025-04-03 00:00:00+00:00',
                   open_rate=100, close_rate=101, profit_abs=1, profit_ratio=.001, exit_reason='roi',
                   stake_amount=1000, amount=10, fee_open=.001, fee_close=.001) for _ in range(30)]
    timestamp = lambda value: int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)
    stats = {'trades': trades, 'starting_balance': 10000, 'final_balance': 10030, 'max_drawdown_account': .03,
             'pairlist': ['BTC/USDT'], 'timeframe': '1h', 'stake_currency': 'USDT', 'total_trades': 30,
             'backtest_start_ts': timestamp('2025-04-01'), 'backtest_end_ts': timestamp('2025-07-01') - 3600000,
             'backtest_start': '2025-04-01', 'backtest_end': '2025-06-30', 'profit_total_abs': 30}
    archive = folder / 'backtest-result-test.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('backtest.json', json.dumps({'strategy': {'SmaTrend': stats}}))
    result = parse_result(archive, job['config'], provenance)
    write_json(folder / 'result.json', result)
    with store.connect() as conn:
        conn.execute("UPDATE experiments SET status='succeeded',result=? WHERE id=?", (json.dumps(result), job['id']))
    diag = settings.ARTIFACTS / 'diagnostic-synthetic'
    (diag / 'strategies').mkdir(parents=True)
    (diag / 'strategies' / 'SmaTrend.py').write_text('# synthetic strategy\n')
    rows = [{'strategy': 'SmaTrend', 'has_bias': 'False', 'total_signals': '20', 'biased_entry_signals': '0',
             'biased_exit_signals': '0', 'biased_indicators': ''}]
    with (diag / 'lookahead.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (diag / 'lookahead.log').write_text('synthetic successful diagnostic')
    report = {'job_id': job['id'], 'strategy': 'SmaTrend', 'source_result_sha256': sha256(folder / 'result.json'),
              'status': 'passed_sampled', 'exit_code': 0, 'rows': rows, 'log_sha256': sha256(diag / 'lookahead.log')}
    write_json(diag / 'diagnostic.json', report)
    return job, folder, diag


def release(client, evidence):
    response = client.post('/api/releases', json={'experiment_id': evidence[0]['id']})
    assert response.status_code == 201
    return response.json()


def test_replay_is_read_only_chronological_and_reconciles(client, evidence):
    job = store.get(evidence[0]['id'])
    before = json.dumps(job, sort_keys=True)
    response = client.get('/api/experiments/' + job['id'] + '/replay')
    assert response.status_code == 200
    replay = response.json()
    assert replay['mode'] == 'historical_replay'
    assert len(replay['events']) == 60
    assert replay['events'][0]['type'] == 'trade_open'
    assert replay['events'][0]['realized_balance'] == 10000
    assert replay['events'][-1]['realized_balance'] == replay['final_balance'] == 10030
    assert all(event['risk_state'] is None and event['decision_evidence'] is None for event in replay['events'])
    assert json.dumps(store.get(job['id']), sort_keys=True) == before
    (evidence[1] / 'backtest-result-test.zip').write_bytes(b'tampered')
    assert client.get('/api/experiments/' + job['id'] + '/replay').status_code == 409


def test_deployment_route_rejects_unapproved_release(client, evidence):
    response = client.post('/api/deployments', json={'release_id': 'missing'}, headers={'Idempotency-Key': 'test'})
    assert response.status_code == 409
    assert client.get('/api/deployments').json() == []


def test_replay_preserves_only_result_bound_diagnostic_status(client, evidence):
    path = evidence[2] / 'diagnostic.json'
    diagnostic = json.loads(path.read_text())
    diagnostic['status'] = 'flagged'
    diagnostic['rows'][0]['has_bias'] = 'True'
    path.write_text(json.dumps(diagnostic))
    endpoint = '/api/experiments/' + evidence[0]['id'] + '/replay'
    replay = client.get(endpoint).json()
    assert replay['diagnostic_status'] == 'flagged'
    assert replay['diagnostics'][0]['status'] == 'flagged'
    assert replay['diagnostics'][0]['rows'][0]['has_bias'] == 'True'
    assert any('flagged' in limitation for limitation in replay['limitations'])
    diagnostic['source_result_sha256'] = 'different-result'
    path.write_text(json.dumps(diagnostic))
    replay = client.get(endpoint).json()
    assert replay['diagnostic_status'] == 'unverified' and replay['diagnostics'] == []


def test_release_pass_and_immutable_snapshot(client, evidence):
    audit = release(client, evidence)
    assert audit['status'] == 'approved'
    assert audit['policy']['mode'] == 'teaching_simulation'
    assert next(c for c in audit['checks'] if c['name'] == 'final_holdout')['status'] == 'unverified'
    assert audit['versions']['result_sha256']
    from quantpilot.releases import verify_release
    frozen, job = verify_release(audit['id'])
    assert frozen['id'] == audit['id'] and job['id'] == evidence[0]['id']
    (evidence[1] / 'result.json').write_text('{}')
    assert client.get('/api/releases/' + audit['id']).json() == audit
    assert client.put('/api/releases/' + audit['id'], json={}).status_code == 405
    with pytest.raises(ValueError):
        verify_release(audit['id'])
    assert release(client, evidence)['status'] == 'rejected'


@pytest.mark.parametrize('filename', ['effective-config.json', 'manifest.json', 'strategies/SmaTrend.py', 'backtest-result-test.zip', 'market/BTC_USDT-1h.feather'])
def test_release_hash_tampering(client, evidence, filename):
    target = evidence[1] / filename
    if filename == 'manifest.json':
        manifest = json.loads(target.read_text())
        manifest['id'] = 'tampered-dataset'
        target.write_text(json.dumps(manifest))
    else:
        target.write_bytes(target.read_bytes() + b' ')
    assert release(client, evidence)['status'] == 'rejected'


def test_missing_diagnostic_is_insufficient(client, evidence):
    (evidence[2] / 'diagnostic.json').unlink()
    assert release(client, evidence)['status'] == 'insufficient_evidence'


def test_freqai_blocked_even_if_report_claims_pass(client, evidence):
    job = evidence[0]
    with store.connect() as conn:
        config = dict(job['config'], strategy='FreqaiTrend')
        conn.execute('UPDATE experiments SET config=? WHERE id=?', (json.dumps(config), job['id']))
    audit = release(client, evidence)
    assert audit['status'] == 'rejected'
    check = next(c for c in audit['checks'] if c['name'] == 'simulation_strategy')
    assert check['status'] == 'failed'
    assert 'historical' in check['detail'] and 'flagged' in check['detail']


def test_diagnostic_sample_and_binding(client, evidence):
    path = evidence[2] / 'diagnostic.json'
    report = json.loads(path.read_text())
    report['source_result_sha256'] = 'wrong-result'
    path.write_text(json.dumps(report))
    assert release(client, evidence)['status'] == 'insufficient_evidence'


def test_missing_result_is_insufficient(client, evidence):
    path = evidence[1] / 'result.json'
    path.unlink()
    assert release(client, evidence)['status'] == 'insufficient_evidence'


def test_corrupt_archive_returns_rejected_audit_not_server_error(client, evidence):
    from quantpilot.data import sha256
    job, folder, _ = evidence
    archive = folder / 'backtest-result-test.zip'
    archive.write_bytes(b'not a zip archive')
    result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    result['provenance']['raw_sha256'] = sha256(archive)
    (folder / 'result.json').write_text(json.dumps(result))
    with store.connect() as conn:
        conn.execute('UPDATE experiments SET result=? WHERE id=?', (json.dumps(result), job['id']))
    assert release(client, evidence)['status'] == 'rejected'


def test_malformed_nested_provenance_returns_rejected_audit(client, evidence):
    job, folder, _ = evidence
    result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    result['provenance'] = None
    (folder / 'result.json').write_text(json.dumps(result))
    with store.connect() as conn:
        conn.execute('UPDATE experiments SET result=? WHERE id=?', (json.dumps(result), job['id']))
    assert release(client, evidence)['status'] == 'rejected'


def test_malformed_nested_raw_strategy_returns_rejected_audit(client, evidence):
    import zipfile
    from quantpilot.data import sha256
    job, folder, _ = evidence
    archive = folder / 'backtest-result-test.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('backtest.json', json.dumps({'strategy': {'SmaTrend': []}}))
    result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    result['provenance']['raw_sha256'] = sha256(archive)
    (folder / 'result.json').write_text(json.dumps(result))
    with store.connect() as conn:
        conn.execute('UPDATE experiments SET result=? WHERE id=?', (json.dumps(result), job['id']))
    audit = release(client, evidence)
    assert audit['status'] == 'rejected'
    assert next(c for c in audit['checks'] if c['name'] == 'raw_artifact_integrity')['status'] == 'failed'


@pytest.mark.parametrize('signals,bias,expected', [('19', 'False', 'insufficient_evidence'), ('20', 'True', 'rejected')])
def test_diagnostic_sample_and_raw_bias(client, evidence, signals, bias, expected):
    import csv
    path = evidence[2] / 'diagnostic.json'
    report = json.loads(path.read_text())
    report['rows'][0].update(total_signals=signals, has_bias=bias)
    path.write_text(json.dumps(report))
    with (evidence[2] / 'lookahead.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(report['rows'][0]))
        writer.writeheader()
        writer.writerows(report['rows'])
    assert release(client, evidence)['status'] == expected


def test_concurrent_confirmation_creates_one_job(client):
    from concurrent.futures import ThreadPoolExecutor
    saved = draft(client)
    def confirm(_):
        return client.post('/api/research/' + saved['id'] + '/confirm', json={'revision': 1, 'spec': saved['spec']})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(confirm, range(2)))
    assert sorted(r.status_code for r in responses) == [200, 201]
    assert len({r.json()['experiment']['id'] for r in responses}) == 1
    assert len(store.list_jobs()) == 1


def test_llm_suggestion_validated_and_provenance_cannot_be_forged(client, monkeypatch):
    import httpx
    from quantpilot import llm
    monkeypatch.setenv('QUANTPILOT_LLM_BASE_URL', 'https://provider.example/v1')
    monkeypatch.setenv('QUANTPILOT_LLM_MODEL', 'configured-model')
    monkeypatch.setenv('QUANTPILOT_LLM_API_KEY', 'never-exposed-test-key')
    def response(request):
        assert request.url.path == '/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer never-exposed-test-key'
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(spec())}}]})
    monkeypatch.setattr(llm, 'client_factory', lambda: httpx.Client(transport=httpx.MockTransport(response)))
    result = client.post('/api/research/suggest', json={'prompt': 'research BTC', 'provider': 'llm'})
    assert result.status_code == 200
    suggestion = result.json()
    assert suggestion['source'] == 'llm'
    assert 'never-exposed-test-key' not in result.text
    record = client.post('/api/research', json={'spec': suggestion['spec'], 'source': 'llm', 'suggestion_id': suggestion['suggestion_id']})
    assert record.status_code == 201
    assert record.json()['generator']['provider'] == 'llm'
    assert client.post('/api/research', json={'spec': spec(), 'source': 'llm'}).status_code == 422
    edited = dict(spec(), fee=.005)
    assert client.post('/api/research', json={'spec': edited, 'source': 'llm', 'suggestion_id': suggestion['suggestion_id']}).status_code == 422
    manual = client.post('/api/research', json={'spec': edited, 'source': 'manual', 'suggestion_id': suggestion['suggestion_id']})
    assert manual.status_code == 201 and manual.json()['generator']['provider'] == 'llm'


def test_llm_unconfigured_and_remote_errors_sanitized(client, monkeypatch):
    import httpx
    from quantpilot import llm
    for name in ('BASE_URL', 'MODEL', 'API_KEY'):
        monkeypatch.delenv('QUANTPILOT_LLM_' + name, raising=False)
    assert client.post('/api/research/suggest', json={'prompt': 'BTC', 'provider': 'llm'}).status_code == 409
    for name, value in [('BASE_URL', 'https://provider.example/v1'), ('MODEL', 'model'), ('API_KEY', 'secret')]:
        monkeypatch.setenv('QUANTPILOT_LLM_' + name, value)
    monkeypatch.setattr(llm, 'client_factory', lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, text='secret response'))))
    result = client.post('/api/research/suggest', json={'prompt': 'BTC', 'provider': 'llm'})
    assert result.status_code == 502 and 'secret' not in result.text
    monkeypatch.setattr(llm, 'client_factory', lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(dict(spec(), code='arbitrary code'))}}]}))))
    result = client.post('/api/research/suggest', json={'prompt': 'BTC', 'provider': 'llm'})
    assert result.status_code == 422 and 'arbitrary code' not in result.text


def test_local_template_source_requires_server_suggestion(client):
    assert client.post('/api/research', json={'spec': spec(), 'source': 'local_template'}).status_code == 422
    suggestion = client.post('/api/research/suggest', json={'prompt': 'BTC SMA trend baseline'}).json()
    body = {'spec': suggestion['spec'], 'source': 'local_template', 'suggestion_id': suggestion['suggestion_id']}
    created = client.post('/api/research', json=body)
    assert created.status_code == 201
    saved = created.json()
    assert saved['generator']['provider'] == 'local_template'
    assert client.put('/api/research/' + saved['id'], json={'spec': saved['spec'], 'source': 'local_template', 'revision': 1}).status_code == 422
    body['spec'] = dict(body['spec'], fee=.003)
    assert client.post('/api/research', json=body).status_code == 422
    body['source'] = 'manual'
    assert client.post('/api/research', json=body).status_code == 201


@pytest.mark.parametrize('header_delay', [0, 21])
def test_llm_total_deadline_aborts_stream_and_sanitizes(client, monkeypatch, header_delay):
    import httpx
    from quantpilot import llm
    for name, value in [('BASE_URL', 'https://provider.example/v1'), ('MODEL', 'model'), ('API_KEY', 'test-secret')]:
        monkeypatch.setenv('QUANTPILOT_LLM_' + name, value)
    clock = [0.0]
    monkeypatch.setattr(llm, 'monotonic', lambda: clock[0], raising=False)
    payload = json.dumps({'choices': [{'message': {'content': json.dumps(spec())}}]}).encode()

    class Trickle(httpx.SyncByteStream):
        received = 0
        closed = False

        def __iter__(self):
            for index in range(0, len(payload), 16):
                clock[0] += 8
                self.received += 1
                yield payload[index:index + 16]

        def close(self):
            self.closed = True

    stream = Trickle()
    def respond(request):
        clock[0] += header_delay
        return httpx.Response(200, stream=stream)
    monkeypatch.setattr(llm, 'client_factory', lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    response = client.post('/api/research/suggest', json={'prompt': 'BTC', 'provider': 'llm'})
    assert response.status_code == 502
    assert 'test-secret' not in response.text
    assert stream.closed
    assert stream.received == (0 if header_delay else 3)
