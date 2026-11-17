from copy import deepcopy
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from quantpilot import settings, terminal_store as store, terminal_engine, terminal_market, terminal_metrics
from quantpilot.terminal_api import create_router
from quantpilot.terminal_robustness import run


def test_study_is_bounded_idempotent_and_retains_failed_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    app=FastAPI(); app.include_router(create_router()); client=TestClient(app)
    s=client.post('/api/terminal/strategies',json={'kind':'prediction','symbol':'BTC/USDT','start':'2025-01-01','end':'2025-01-15'}).json()
    url='/api/terminal/strategies/'+s['id']+'/robustness'
    body={'windows':[{'start':'2025-01-01','end':'2025-01-15'},{'start':'2025-02-01','end':'2025-02-15'}], 'seeds':[7,42]}
    headers={'Idempotency-Key':'study'}
    r=client.post(url,json=body,headers=headers); assert r.status_code==201,r.text
    assert client.post(url,json=body,headers=headers).json()['id']==r.json()['id']
    assert client.post(url,json={**body,'seeds':[7,43]},headers=headers).status_code==409
    invalid=deepcopy(body); invalid['windows'][1]={'start':'2025-10-01','end':'2025-10-15'}
    assert client.post(url,json=invalid,headers={'Idempotency-Key':'invalid'}).status_code==422
    invalid=deepcopy(body); invalid['windows'][1]={'start':'2025-01-10','end':'2025-01-24'}
    assert client.post(url,json=invalid,headers={'Idempotency-Key':'invalid'}).status_code==422
    assert store.claim()['id']==r.json()['id']
    monkeypatch.setattr(terminal_market,'prepare_dataset',lambda config, directory: object())
    def train(config,directory,candles):
        if config['parameters']['seed']==42: raise ValueError('deliberate failure')
        return {'id':'model-test','metrics':{'net_return':.1},'period':{},'model_manifest':{'files':{'snapshot.feather':'hash'},'data_provenance':{'source_verified':True}}}
    monkeypatch.setattr(terminal_engine,'train',train)
    monkeypatch.setattr(terminal_metrics,'evaluate_candidate',lambda candidate:{'risk':{'sharpe':1},'prediction':None})
    run(store.get('jobs',r.json()['id']))
    report=json.loads((store.job_directory(r.json()['id'])/'robustness-completion.json').read_text(encoding='utf-8'))['comparison']
    assert report['status']=='partial' and report['summary']['completed']==2 and report['summary']['failed']==2
    assert all(row['error']=='deliberate failure' for row in report['rows'] if row['status']=='failed')
    store.finish(r.json()['id'],comparison=report)
    assert store.get('jobs',r.json()['id'])['status']=='succeeded'
    assert store.list_records('candidates')==[]


def test_cancelled_study_keeps_partial_evidence(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'HOME',tmp_path)
    from quantpilot.terminal_api import StrategyInput
    config=StrategyInput(kind='prediction').model_dump(mode='json')
    strategy=store.create_strategy(config)
    job,_=store.enqueue(strategy['id'],'cancel-study','robustness',{'windows':[],'seeds':[]})
    store.claim()
    report={'status':'running','arms':['price_only'], 'rows':[{'status':'succeeded'},{'status':'running'},{'status':'pending'}]}
    store.progress(job['id'],'robustness_training',comparison=report)
    store.cancel(job['id']); result=store.finish(job['id'],error='cancelled')
    assert result['status']=='cancelled'
    assert [r['status'] for r in result['comparison']['rows']]==['succeeded','cancelled','cancelled']
