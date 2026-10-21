"""Public data invariants: isolated storage and synthetic network/provider fixtures."""
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from quantpilot import api, settings, public_store as ps, news_data as news, exchange_data as ex, news_analysis as analysis, public_worker as worker


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'HOME', tmp_path)
    monkeypatch.setattr(settings, 'DB', tmp_path / 'old.sqlite3')
    monkeypatch.setattr(api, 'get_dataset', lambda: {'id':'historical-fixture','available':True,'start':'2025-01-01','end':'2025-10-01'})
    yield
    ps.WORKER_JOB.set(None)


def feed(title='Bitcoin monetary policy', description='Safe excerpt', guid='stable', date='Thu, 15 Oct 2026 00:00:00 GMT'):
    return f'<rss><channel><item><title>{title}</title><description><![CDATA[{description}]]></description><link>https://www.federalreserve.gov/newsevents/example.htm</link><guid>{guid}</guid><pubDate>{date}</pubDate></item></channel></rss>'.encode()


def version():
    news.ingest('federal_reserve',feed())
    return news.list_items()[0]


def test_versions_immutable_dedup_guid_revision_and_reversion():
    first=version()
    assert news.ingest('federal_reserve',feed(guid='changed-guid'))['new_versions']==0
    assert news.ingest('federal_reserve',feed(description='Changed'))['new_versions']==1
    second=news.list_items()[0]
    assert second['id']!=first['id'] and second['content_hash']!=first['content_hash']
    assert second['first_seen_at']==first['first_seen_at']
    assert second['previous_version_id']==first['id']
    assert news.get(first['id'])==first
    assert news.ingest('federal_reserve',feed())['new_versions']==1
    with pytest.raises(sqlite3.IntegrityError):
        with ps.connect() as conn:
            conn.execute('UPDATE news SET content_hash=? WHERE id=?',('forged',first['id']))
    assert news.list_items(asset='BTC',event='monetary_policy')
    assert news.list_items(asset='ETH')==[]


def test_observation_time_future_publication_and_safe_content():
    news.ingest('federal_reserve',feed(description='<script>steal()</script><b>Hello</b> &amp; world',date='Fri, 15 Oct 2038 00:00:00 GMT'))
    item=news.list_items()[0]
    assert item['publisher_timestamp_in_future'] is True
    assert item['available_at']==item['published_at']
    assert 'steal' not in item['summary'] and '<b>' not in item['summary']
    assert item['first_seen_at']<=item['revision_seen_at']
    assert news.safe_link('https://www.federalreserve.gov.evil.test/x','federal_reserve') is None
    assert news.safe_link('javascript:alert(1)','federal_reserve') is None


@pytest.mark.parametrize('raw',[
    b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///secret">]><rss/>',
    b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss/>',
    '<!DOCTYPE rss><rss/>'.encode('utf-16'), b'x'*512001,
], ids=['external-entity', 'internal-entity', 'utf16-doctype', 'oversized'])
def test_unsafe_xml_rejected(raw):
    with pytest.raises((ValueError,UnicodeError)):
        news.parse('federal_reserve',raw)


def test_feed_ssrf_and_redirect_rejected(monkeypatch):
    monkeypatch.setattr(news.socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError): news.public_addresses('www.federalreserve.gov')
    monkeypatch.setattr(news,'public_addresses',lambda host:['8.8.8.8'])
    class Redirect:
        status=302
    class Connection:
        def __init__(self,*a): pass
        def request(self,*a,**kw): pass
        def getresponse(self): return Redirect()
        def close(self): pass
    monkeypatch.setattr(news,'PinnedHTTPS',Connection)
    with pytest.raises(ValueError,match='feed_http_failure'): news.fetch('federal_reserve')
    with pytest.raises(ValueError): news.fetch('https://127.0.0.1')


def test_gets_never_fetch_and_invalid_sources(client=None):
    client=TestClient(api.app)
    assert client.get('/api/data/exchanges').json()[0]['availability']=='unverified'
    assert client.post('/api/data/exchanges/evil/refresh').status_code==422
    assert client.post('/api/news/refresh',json={'source':'https://127.0.0.1'}).status_code==422
    assert client.post('/api/news/refresh',json={'source':'ecb','url':'http://localhost'}).status_code==422
    assert client.get('/api/news?asset=SOL').status_code==422
    first=client.post('/api/data/exchanges/kraken/refresh').json()
    assert first['status']=='queued'
    assert client.post('/api/data/exchanges/kraken/refresh').json()['id']==first['id']
    assert client.get('/api/data/jobs/'+first['id']).json()['status']=='queued'


def test_source_separation_failure_preserves_last_success_and_no_secrets(monkeypatch):
    ps.save_cache('exchange:binance',{'source':'binance','markets':[]})
    before=ex.view('binance')
    monkeypatch.setattr(ex,'snapshot',lambda venue: (_ for _ in ()).throw(RuntimeError('secret-key-123')))
    with pytest.raises(ValueError): ex.refresh('binance')
    after=ex.view('binance')
    assert after['data']==before['data'] and after['last_success']==before['last_success']
    assert after['availability']=='unavailable'
    assert ex.view('okx')['data'] is None
    assert 'secret-key' not in json.dumps(after)


def test_exchange_real_adapter_shape_partial_and_market_metadata():
    class Fake:
        precisionMode=4
        def load_markets(self): return {'BTC/USDT':{'spot':True,'id':'XBTUSDT','active':True,'precision':{'amount':.00001},'limits':{'cost':{'min':5}}}}
        def fetch_ticker(self,symbol): return {'timestamp':1,'last':float('nan'),'bid':2}
        def fetch_ohlcv(self,*args,**kwargs): return [[0,1,2,1,2,3],[7200000,1,2,1,2,3]]
    result=ex.snapshot('kraken',lambda venue:Fake())
    assert result['source']=='kraken' and result['markets'][0]['limits']['cost']['min']==5
    assert result['markets'][0]['ticker']['last'] is None
    assert result['markets'][0]['missing_interval_count']==1
    assert result['markets'][1]['availability']=='unavailable'
    assert result['capabilities']['live_trading']=='unverified'
    ps.save_cache('exchange:kraken',result)
    cached=ex.view('kraken')
    assert cached['stale'] is False and cached['data']['markets'][0]['ticker_stale'] is True


def test_job_lease_recovery_queue_bounds_and_fencing():
    first=ps.enqueue('news','ecb')
    claimed=ps.claim()
    assert claimed['id']==first['id'] and ps.claim() is None
    with ps.connect() as conn: conn.execute('UPDATE jobs SET lease_until=0 WHERE id=?',(first['id'],))
    ps.WORKER_JOB.set(first['id'])
    with pytest.raises(ValueError): ps.save_cache('news:ecb',{'forged':True})
    with pytest.raises(ValueError): news.ingest('federal_reserve',feed())
    ps.WORKER_JOB.set(None)
    second=ps.enqueue('news','federal_reserve')
    assert ps.get_job(first['id'])['status']=='failed'
    assert ps.claim()['id']==second['id']
    for i in range(7): ps.enqueue('analysis',str(i),{'provider':'local'})
    with pytest.raises(ValueError,match='full'): ps.enqueue('analysis','overflow',{'provider':'local'})


def test_analysis_local_cache_schema_model_cache_and_budget(monkeypatch):
    item=version()
    first=analysis.analyze(item['id'],'local')
    assert first['analysis']['sentiment']=='unknown' and first['model'] is None
    assert analysis.analyze(item['id'],'local')['id']==first['id']
    assert first['historical_feature_eligible'] is False
    model=['deepseek-flash']
    monkeypatch.setattr(analysis.llm,'configuration',lambda:dict(zip(analysis.llm.NAMES,['https://api.deepseek.com',model[0],'private-key'])))
    calls=[]
    async def transport(url,key,payload):
        calls.append(payload)
        return json.dumps({'choices':[{'message':{'content':json.dumps(first['analysis'])}}]}).encode()
    monkeypatch.setattr(analysis.llm,'request_configuration',transport)
    a=analysis.analyze(item['id'],'deepseek')
    assert analysis.analyze(item['id'],'deepseek')['id']==a['id'] and len(calls)==1
    model[0]='deepseek-new'
    assert analysis.analyze(item['id'],'deepseek')['id']!=a['id'] and len(calls)==2
    assert 'tools' not in calls[0] and calls[0]['max_tokens']==1200
    assert 'private-key' not in json.dumps(a)
    for i in range(10):
        job=ps.enqueue('analysis',str(i),{'provider':'deepseek'})
        ps.claim()
        ps.finish(job['id'],error='failed')
    with pytest.raises(ValueError,match='budget'): ps.enqueue('analysis','budget11',{'provider':'deepseek'})


def test_provider_failures_are_sanitized(monkeypatch):
    item=version()
    monkeypatch.setattr(analysis.llm,'configuration',lambda:dict(zip(analysis.llm.NAMES,['https://api.deepseek.com','deepseek-flash','private-key'])))
    async def failure(*args): raise RuntimeError('private-key upstream response')
    monkeypatch.setattr(analysis.llm,'request_configuration',failure)
    with pytest.raises(ValueError,match='deepseek_request_or_schema_failed') as error: analysis.analyze(item['id'],'deepseek')
    assert 'private-key' not in str(error.value)


def test_saved_research_linkage_survives_edit_and_is_idempotent():
    item=version()
    client=TestClient(api.app)
    draft=client.post('/api/news/'+item['id']+'/research').json()
    again=client.post('/api/news/'+item['id']+'/research').json()
    assert draft['id']==again['id'] and draft['status']=='draft'
    assert draft['news_evidence']['content_hash']==item['content_hash']
    assert 'not a backtest feature' in draft['spec']['hypothesis']
    edited=dict(draft['spec'],name='Edited')
    result=client.put('/api/research/'+draft['id'],json={'spec':edited,'source':'manual','revision':1})
    assert result.status_code==200 and result.json()['news_evidence']==draft['news_evidence']
    assert client.get('/api/experiments').json()==[]


def test_worker_environment_excludes_trading_credentials(monkeypatch):
    monkeypatch.setenv('BINANCE_API_KEY','private-exchange-key')
    monkeypatch.setenv('QUANTPILOT_LLM_API_KEY','private-llm-key')
    env=worker.child_environment({'kind':'news','payload':{}})
    assert 'BINANCE_API_KEY' not in env and 'QUANTPILOT_LLM_API_KEY' not in env
    env=worker.child_environment({'kind':'analysis','payload':{'provider':'deepseek'}})
    assert 'BINANCE_API_KEY' not in env and env['QUANTPILOT_LLM_API_KEY']=='private-llm-key'
