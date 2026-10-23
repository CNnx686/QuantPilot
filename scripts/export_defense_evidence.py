"""Export read-only, credential-free defense evidence from the local service."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts' / 'defense-pack-20261023'
SMA = 'f37527ed-c2f9-4dfc-a75c-1a511a3a2133'
ML = '7dbbf255-99bc-45b4-bd29-ab47e80f2c65'
ML3 = '0394258d-f92c-45a8-9318-cc1a61f4469e'
RL = 'rl-00b1bad30cca4c9a9a909e9744e13aad'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    receipts = {}
    def fetch(path, name):
        with urlopen('http://127.0.0.1:8765' + path, timeout=120) as response:
            content = response.read()
            assert response.status == 200
        (OUT / name).write_bytes(content)
        receipts[name] = {'endpoint': path, 'sha256': hashlib.sha256(content).hexdigest()}
        return json.loads(content) if name.endswith('.json') else content

    system = fetch('/api/system', 'system.json')
    assert system['core_ready'] and system['worker']['alive']
    assert all(row['state'] == 'stopped' for row in system['deployments']['records'])
    for label, identifier in [('sma-q2', SMA), ('freqai-q2', ML), ('freqai-q3', ML3)]:
        report = fetch(f'/api/experiments/{identifier}/report/download?format=json', label + '.json')
        fetch(f'/api/experiments/{identifier}/report/download?format=html', label + '.html')
        assert report['diagnostic_status'] == 'passed_sampled'
        if label.startswith('freqai'):
            assert any('前视检查仅抽样通过' in text for text in report['narrative'])
        receipts[label + '.json']['metrics'] = report['metrics']
    job = fetch(f'/api/experiments/{SMA}', 'sma-job.json')
    assert job['status'] == 'succeeded'
    original = fetch('/api/experiments/f8f8185a-5875-4756-802b-b0b4dc19f4f3/report', 'sma-original.json')
    assert original['metrics'] == job['result']['metrics']
    compare = fetch(f'/api/comparison?ids={SMA},{ML}', 'comparison-q2.json')
    assert compare['comparable']
    mismatch = fetch(f'/api/comparison?ids={SMA},{ML3}', 'comparison-mismatched.json')
    assert not mismatch['comparable']
    replay = fetch(f'/api/experiments/{SMA}/replay', 'sma-replay.json')
    releases = fetch('/api/releases', 'releases.json')
    assert {'approved', 'insufficient_evidence'} <= {r['status'] for r in releases if r['experiment_id'] == SMA}
    fetch('/api/deployments/bf7cf2e7-c6a3-4299-9bc1-97a414449fce', 'simulation-stopped.json')
    fetch('/api/data/exchanges', 'markets.json')
    fetch('/api/news/sources', 'news-sources.json')
    fetch('/api/news/a357eb4e-c57a-40b0-8382-e057373295fb', 'news-example.json')
    fetch('/api/research/d59c141a-a0e0-5bc5-8a6d-e26786b2a9d3', 'news-research.json')
    public_jobs = json.loads((ROOT / 'runtime/defense-public-jobs.json').read_text(encoding='utf-8-sig'))
    public_jobs += [{'id': '9c22d544-4a1e-4add-8d15-d060f2c383ba'}, {'id': '524c8920-b0ed-4455-895a-15e936a61f77'}]
    for item in public_jobs:
        result = fetch('/api/data/jobs/' + item['id'], 'public-job-' + item['id'] + '.json')
        receipts['public-job-' + item['id'] + '.json']['status'] = result['status']
    fetch(f'/api/rl/jobs/{RL}/results', 'rl-results.json')
    ready = fetch('/api/rl/readiness', 'rl-readiness.json')
    assert ready['available']
    fetch('/api/live/status', 'live-public-status.json')
    acceptance = dict(verified_at=datetime.now(timezone.utc).isoformat(), files=receipts,
                      checks='Assertions passed; no private endpoints or trading writes invoked by this exporter')
    (OUT / 'index.json').write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'exports': len(receipts), 'output': str(OUT), 'sma_metrics': job['result']['metrics'],
                      'public_status': {k: v['status'] for k, v in receipts.items() if 'status' in v}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
