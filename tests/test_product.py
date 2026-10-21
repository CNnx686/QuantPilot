"""Product report tests use synthetic Week3 evidence and isolated storage."""
import json
import pytest
from test_workflow import client, evidence, release
from quantpilot import settings, store


def report_url(evidence):
    return '/api/experiments/' + evidence[0]['id'] + '/report'


def test_report_matches_evidence_and_links(client, evidence):
    saved = release(client, evidence)
    response = client.get(report_url(evidence))
    assert response.status_code == 200
    value = response.json()
    assert value['schema_version'] == '1'
    assert value['metrics'] == store.get(evidence[0]['id'])['result']['metrics']
    assert value['trades'][0]['id'] == 'trade:0'
    assert value['trades'][0]['open_date'] == '2025-04-02 00:00:00+00:00'
    assert len(value['trades']) == 30
    assert value['research'][0]['experiment_id'] == evidence[0]['id']
    assert value['releases'][0]['id'] == saved['id']
    assert 'final holdout' in ' '.join(value['limitations'])
    assert value['narrative'] == client.get(report_url(evidence)).json()['narrative']


def test_news_lineage_survives_report_and_html_is_escaped(client, evidence):
    news = dict(id='immutable-version', source='ecb', title='<script>untrusted</script>',
                url='https://www.ecb.europa.eu/press/example.html', content_hash='a' * 64,
                published_at='2026-09-30T12:00:00Z', first_seen_at='2026-10-15T06:00:00Z',
                revision_seen_at='2026-10-15T06:00:00Z', available_at='2026-10-15T06:00:00Z',
                historical_feature_eligible=False, usage='hypothesis_context_only')
    with store.connect() as conn:
        row = conn.execute('SELECT id,record FROM research').fetchone()
        record = json.loads(row['record'])
        record.update(id='news-research', news_evidence=news, private_extra='private-sentinel')
        conn.execute('INSERT INTO research VALUES(?,?)', (record['id'], json.dumps(record)))
    response = client.get(report_url(evidence))
    assert response.status_code == 200
    value = response.json()
    assert next(item for item in value['research'] if item['id'] == 'news-research')['news_evidence'] == news
    assert 'private-sentinel' not in response.text
    assert any('未作为历史特征' in sentence for sentence in value['narrative'])
    assert any('不能解释' in sentence for sentence in value['limitations'])
    exported = client.get(report_url(evidence) + '/download?format=html').text
    assert '<script>' not in exported and '&lt;script&gt;' in exported
    assert news['content_hash'] in exported and news['available_at'] in exported


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'failed', 'mismatch'])
def test_report_rejects_bad_evidence(client, evidence, damage):
    job, folder, _ = evidence
    if damage == 'missing':
        (folder / 'result.json').unlink()
    elif damage == 'corrupt':
        (folder / 'backtest-result-test.zip').write_bytes(b'broken')
    elif damage == 'mismatch':
        (folder / 'result.json').write_text('{}')
    else:
        with store.connect() as conn:
            conn.execute("UPDATE experiments SET status='failed' WHERE id=?", (job['id'],))
    assert client.get(report_url(evidence)).status_code == 409
    assert client.get(report_url(evidence) + '/download').status_code == 409


def test_download_escape_and_errors(client, evidence):
    with store.connect() as conn:
        conn.execute('UPDATE experiments SET name=? WHERE id=?', ('<script>alert(1)</script>', evidence[0]['id']))
    response = client.get(report_url(evidence) + '/download?format=html')
    assert response.status_code == 200
    assert '<script>' not in response.text and '&lt;script&gt;' in response.text
    assert 'trade:29' in response.text and '<table' in response.text
    assert 'attachment' in response.headers['content-disposition']
    assert client.get(report_url(evidence) + '/download?format=json').json()['metrics']['trades'] == 30
    assert client.get(report_url(evidence) + '/download?format=pdf').status_code == 422
    assert client.get('/api/experiments/unknown/report').status_code == 404


def test_system_missing_db_read_only_and_secret_free(client, monkeypatch, tmp_path):
    monkeypatch.setenv('QUANTPILOT_LLM_API_KEY', 'secret-sentinel')
    monkeypatch.setenv('QUANTPILOT_LLM_BASE_URL', 'https://secret-user:secret-password@example.invalid')
    monkeypatch.setenv('QUANTPILOT_LLM_MODEL', 'secret-model')
    response = client.get('/api/system')
    assert response.status_code == 200
    value = response.json()
    assert value['database'] == {'exists': False, 'readable': False}
    assert not settings.DB.exists()
    assert not (tmp_path / 'runtime').exists()
    assert value['llm'] == {'configured': True, 'optional': True}
    assert 'secret-' not in response.text
    assert value['deployments']['live_verified'] is False


def test_system_counts_worker_and_doctor(client, evidence, monkeypatch):
    from quantpilot import doctor
    store.acquire_worker('test-owner')
    value = client.get('/api/system').json()
    assert value['worker']['alive'] is True
    assert value['saved_counts'] == {'experiments': 1, 'research': 1, 'releases': 0}
    assert value['version'] == doctor.VERSION
    assert isinstance(doctor.snapshot()['checklist'], list)
