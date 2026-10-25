"""Local terminal workflow API. All business state is server-owned and persisted."""
import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import terminal_store as store, terminal_worker as worker


class Parameters(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    fast: int = Field(default=12, ge=2, le=64)
    slow: int = Field(default=48, ge=3, le=128)
    horizon: int = Field(default=6, ge=1, le=24)
    entry_threshold: float = Field(default=.003, gt=0, le=.2)
    rl_steps: Literal[2048, 8192] = 2048
    seed: int = Field(default=7, ge=0, le=1000000)
    stop_loss: float = Field(default=.05, ge=.001, le=.5)

    @model_validator(mode='after')
    def ordering(self):
        if self.fast >= self.slow:
            raise ValueError('Fast SMA must be shorter than slow SMA')
        return self


class StrategyInput(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    name: str = Field(default='我的策略', min_length=1, max_length=100)
    kind: Literal['rule', 'rl', 'prediction'] = 'rule'
    symbol: str = Field(default='BTC/USDT', pattern=r'^[A-Z0-9]{2,16}/USDT$')
    timeframe: Literal['1h'] = '1h'
    start: date = date(2025, 1, 1)
    end: date = date(2025, 4, 1)
    wallet: float = Field(default=10000, ge=100, le=1e9)
    stake: float = Field(default=1000, ge=10, le=1e9)
    fee: float = Field(default=.001, ge=0, le=.02)
    parameters: Parameters = Field(default_factory=Parameters)
    news: bool = False

    @model_validator(mode='after')
    def validate_strategy(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError('Name must not be blank')
        if not 14 <= (self.end - self.start).days <= 273:
            raise ValueError('Select between 14 and 273 days; end date is exclusive')
        if self.end > date.today():
            raise ValueError('Historical training cannot include future or unfinished days')
        if self.start < date(2025, 1, 1) or self.end > date(2025, 10, 1):
            raise ValueError('This development workflow supports [2025-01-01, 2025-10-01); other periods remain unavailable')
        if self.start < date(2026, 1, 1) and self.end > date(2025, 10, 1):
            raise ValueError('2025 Q4 remains reserved and cannot be used for development training')
        if self.stake * (1 + self.fee) > self.wallet:
            raise ValueError('Stake including entry fee must fit the virtual wallet')
        if self.news:
            validate_news_configuration(self.model_dump(mode='json'))
        return self


def validate_news_configuration(config):
    if config['kind'] not in {'rl', 'prediction'} or config['symbol'] not in {'BTC/USDT', 'ETH/USDT'}:
        raise ValueError('News factors currently support BTC/ETH prediction and RL strategies')
    days = (date.fromisoformat(config['end']) - date.fromisoformat(config['start'])).days
    if not 14 <= days <= 31:
        raise ValueError('News experiments require a bounded 14 to 31 day development interval')


class EditInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    config: StrategyInput


class SelectInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str = Field(min_length=1, max_length=100)


class InstanceInput(SelectInput):
    strategy_id: str = Field(min_length=1, max_length=100)


class ActionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['pause', 'resume', 'close_position', 'stop', 'tick']
    mode: Literal['wait', 'close'] = 'wait'


def token(value):
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', value):
        raise HTTPException(422, 'Invalid Idempotency-Key')
    return value


def invoke(function, *args):
    try:
        return function(*args)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from None


def bootstrap():
    with store.connect() as conn:
        collections = {name: store.rows(conn, name) for name in ('strategies', 'jobs', 'candidates', 'instances')}
    collections['jobs'] = [store.public_job(job) for job in collections['jobs']]
    return {**collections, 'server_time': store.now(),
        'capabilities': {'kinds': ['rule', 'rl', 'prediction'], 'exchange': 'binance', 'timeframes': ['1h'],
            'news': True, 'news_symbols': ['BTC/USDT', 'ETH/USDT'], 'news_kinds': ['rl', 'prediction'],
            'news_schema': 'gdelt-sampled-6h-news4-v1', 'news_max_days': 31,
            'news_defaults': {'start': '2025-01-01', 'end': '2025-01-15'},
            'news_risk_rules': False, 'live_trading': False, 'execution_engine': 'local_paper_v1',
            'max_instances': worker.MAX_INSTANCES, 'max_active_jobs': 8,
            'rl_steps': {'min': 2048, 'max': 8192, 'values': [2048, 8192]},
            'date_bounds': {'start': '2025-01-01', 'end': '2025-10-01', 'minimum_days': 14},
            'reserved_period': ['2025-10-01', '2026-01-01'],
            'model_switch': False, 'paper_poll_seconds': 30,
            'data_mode': 'public_market_data', 'fill_assumption': 'Current public bid/ask or last; fees applied; no order book impact'},
        'defaults': StrategyInput().model_dump(mode='json')}


def create_router():
    router = APIRouter(prefix='/api/terminal', tags=['Personal strategy terminal'])

    @router.get('/bootstrap')
    def state():
        return bootstrap()

    @router.get('/strategies')
    def strategies():
        return store.list_records('strategies')

    @router.post('/strategies', status_code=201)
    def create_strategy(body: StrategyInput, idempotency_key: str | None = Header(default=None, min_length=1, max_length=100)):
        return invoke(store.create_strategy, body.model_dump(mode='json'), token(idempotency_key) if idempotency_key else None)

    @router.get('/strategies/{identifier}')
    def strategy(identifier: str):
        return invoke(store.get, 'strategies', identifier)

    @router.patch('/strategies/{identifier}')
    def edit_strategy(identifier: str, body: EditInput):
        return invoke(store.update_strategy, identifier, body.revision, body.config.model_dump(mode='json'))

    @router.post('/strategies/{identifier}/runs')
    def run(identifier: str, response: Response, idempotency_key: str = Header(min_length=1, max_length=100)):
        job, created = invoke(store.enqueue, identifier, token(idempotency_key))
        response.status_code = 201 if created else 200
        return store.public_job(job)

    @router.post('/strategies/{identifier}/comparisons')
    def comparison(identifier: str, response: Response, idempotency_key: str = Header(min_length=1, max_length=100)):
        strategy = invoke(store.get, 'strategies', identifier)
        invoke(validate_news_configuration, strategy['config'])
        job, created = invoke(store.enqueue, identifier, token(idempotency_key), 'comparison')
        response.status_code = 201 if created else 200
        return store.public_job(job)

    @router.get('/jobs/{identifier}')
    def job(identifier: str):
        return store.public_job(invoke(store.get, 'jobs', identifier))

    @router.get('/jobs/{identifier}/comparison')
    def comparison_result(identifier: str):
        job = invoke(store.get, 'jobs', identifier)
        if job.get('mode') != 'comparison':
            raise HTTPException(409, 'This task is not a paired comparison')
        return job.get('comparison')

    @router.get('/news')
    def news(symbol: str = 'BTC/USDT', cutoff: str | None = None):
        from .terminal_news import status
        return invoke(status, symbol, cutoff, False)

    @router.post('/news/refresh')
    def refresh_news(symbol: str = 'BTC/USDT'):
        from .terminal_news import status
        return invoke(status, symbol, None, True)

    @router.post('/jobs/{identifier}/cancel')
    def cancel(identifier: str):
        return store.public_job(invoke(store.cancel, identifier))

    @router.get('/jobs/{identifier}/logs')
    def logs(identifier: str):
        invoke(store.get, 'jobs', identifier)
        path = store.job_directory(identifier) / 'worker.log'
        if not path.is_file():
            return {'text': ''}
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 64000))
            return {'text': stream.read().decode('utf-8', errors='replace')}

    @router.get('/strategies/{identifier}/candidates')
    def candidates(identifier: str):
        invoke(store.get, 'strategies', identifier)
        return [item for item in store.list_records('candidates') if item['strategy_id'] == identifier]

    @router.post('/strategies/{identifier}/select')
    def select(identifier: str, body: SelectInput):
        candidate = invoke(store.get, 'candidates', body.candidate_id)
        invoke(worker.verify_candidate, candidate)
        return invoke(store.select_candidate, identifier, body.candidate_id)

    @router.post('/instances')
    def create_instance(body: InstanceInput, response: Response,
                        idempotency_key: str = Header(min_length=1, max_length=100)):
        instance, created = invoke(worker.create_instance, body.strategy_id, body.candidate_id, token(idempotency_key))
        response.status_code = 201 if created else 200
        return instance

    @router.get('/instances/{identifier}')
    def instance(identifier: str):
        return invoke(store.get, 'instances', identifier)

    @router.post('/instances/{identifier}/action')
    def action(identifier: str, body: ActionInput, idempotency_key: str = Header(min_length=1, max_length=100)):
        return invoke(worker.action, identifier, body.action, body.mode, token(idempotency_key))

    return router
