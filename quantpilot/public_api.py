"""Public data API. POST queues bounded work; GET only reads persisted state."""
import json
import uuid
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from . import exchange_data, news_data, news_analysis, public_store, store
from .research import ResearchSpec


class NewsRefresh(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source: Literal['federal_reserve','ecb','coindesk']


class AnalyzeInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    provider: Literal['local','deepseek'] = 'local'


def require_news(identifier):
    record = news_data.get(identifier)
    if not record:
        raise HTTPException(404, 'News version not found')
    return record


def queue(kind, source, payload=None):
    try:
        return public_store.enqueue(kind, source, payload)
    except ValueError as exc:
        raise HTTPException(429, str(exc)) from None


def create_router(dataset_provider):
    router = APIRouter(prefix='/api')

    @router.get('/data/exchanges')
    def exchanges():
        return [exchange_data.view(venue) for venue in exchange_data.VENUES]

    @router.get('/data/exchanges/{venue}')
    def exchange(venue: Literal['binance','okx','kraken']):
        return exchange_data.view(venue)

    @router.post('/data/exchanges/{venue}/refresh', status_code=202)
    def exchange_refresh(venue: Literal['binance','okx','kraken']):
        return queue('exchange',venue)

    @router.get('/data/jobs/{identifier}')
    def job(identifier: str):
        found = public_store.get_job(identifier)
        if not found:
            raise HTTPException(404, 'Public data job not found')
        return found

    @router.get('/news/sources')
    def sources():
        return news_data.sources()

    @router.post('/news/refresh', status_code=202)
    def news_refresh(body: NewsRefresh):
        return queue('news',body.source)

    @router.get('/news')
    def news(source: Literal['federal_reserve','ecb','coindesk'] | None = None,
             asset: Literal['BTC','ETH'] | None = None,
             event: Literal['monetary_policy','regulation','security','market','other'] | None = None,
             limit: int = Query(default=50,ge=1,le=100)):
        return news_data.list_items(source,asset,event,limit)

    @router.get('/news/{identifier}')
    def news_detail(identifier: str):
        item = require_news(identifier)
        return {**item, 'analyses': [result for provider in ('local','deepseek') if (result := news_analysis.cached(identifier,provider))]}

    @router.post('/news/{identifier}/analyze', status_code=202)
    def analyze(identifier: str, body: AnalyzeInput):
        require_news(identifier)
        # The explicit provider selection is the only paid-call entry point.
        return queue('analysis',identifier, {'provider':body.provider})

    @router.post('/news/{identifier}/research', status_code=201)
    def research(identifier: str):
        item = require_news(identifier)
        manifest = dataset_provider()
        if not manifest or not manifest.get('available', True):
            raise HTTPException(409, 'Historical dataset unavailable')
        research_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'quantpilot:news:' + identifier + ':' + manifest['id']))
        spec = ResearchSpec(dataset_id=manifest['id'], name=('新闻假设 · ' + item['title'])[:100],
            pairs=[asset + '/USDT' for asset in item['assets']] or ['BTC/USDT','ETH/USDT'],
            hypothesis=('Contemporary news context: ' + item['title'] + '. Explore the existing trend baseline independently. This news was unavailable to the 2025 dataset and is not a backtest feature; no causal or performance claim.')[:2000])
        evidence = {key:item[key] for key in ('id','source','url','title','content_hash','published_at','first_seen_at','revision_seen_at','available_at')}
        evidence.update(historical_feature_eligible=False, usage='hypothesis_context_only')
        record = {'id': research_id, 'spec':spec.model_dump(mode='json'), 'source':'manual', 'prompt':'', 'suggestion_id':None,
            'status':'draft','revision':1,'experiment_id':None,'created_at':store.now(),'confirmed_at':None,'generator':None,'news_evidence':evidence}
        with store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT record FROM research WHERE id=?',(research_id,)).fetchone()
            if previous:
                return json.loads(previous['record'])
            conn.execute('INSERT INTO research VALUES(?,?)',(research_id,public_store.dumps(record)))
        return record

    return router
