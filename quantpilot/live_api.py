"""Typed local live controls with exact origin, sessions and durable idempotency."""
import hashlib
import json
import re
from typing import Literal
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from . import live_auth as auth, live_store as ledger, live_control as control

router = APIRouter(prefix='/api/live', tags=['live'])


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Login(Strict):
    password: str = Field(min_length=1, max_length=256)


class Limits(Strict):
    capital: float = Field(gt=0, le=10000)
    per_order: float = Field(gt=0, le=1000)
    exposure: float = Field(gt=0, le=10000)
    daily_loss: float = Field(gt=0, le=1000)
    max_spread: float = Field(default=.002, gt=0, le=.01)
    buffer: float = Field(default=.02, ge=.02, le=.1)


class Create(Strict):
    experiment_id: str = Field(pattern=r'^[A-Za-z0-9-]{1,64}$')
    dryrun_id: str = Field(pattern=r'^[A-Za-z0-9-]{1,64}$')
    limits: Limits
    revision: Literal[0] = 0


class Action(Strict):
    revision: int = Field(ge=1)
    acknowledge_local_protection_ceases: bool = False


class ReadJob(Strict):
    kind: Literal['preflight', 'reconcile', 'recover_flat']


def mutate(request, body, operation):
    auth.session(request, unsafe=True)
    key = request.headers.get('idempotency-key', '')
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', key):
        raise HTTPException(422, 'Idempotency-Key required')
    digest = hashlib.sha256((request.url.path + json.dumps(body, sort_keys=True, allow_nan=False)).encode()).hexdigest()
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM requests WHERE key=?', (key,)).fetchone()
        if row:
            if row['digest'] != digest:
                raise HTTPException(409, 'Idempotency body conflict')
            result = json.loads(row['result'])
            if result.get('pending'):
                raise HTTPException(409, 'Operation pending or interrupted; inspect current state')
            if 'error' in result:
                raise HTTPException(409, result['error'])
            return result
        db.execute('INSERT INTO requests VALUES(?,?,?)', (key, digest, '{"pending":true}'))
    try:
        result = operation()
    except Exception as exc:
        result = {'error': str(exc) if isinstance(exc, ValueError) else 'Live operation failed; inspect current state'}
    with ledger.connect() as db:
        db.execute('UPDATE requests SET result=? WHERE key=?', (json.dumps(result), key))
    if 'error' in result:
        raise HTTPException(409, result['error'])
    return result


@router.get('/status')
def status():
    return control.status()


@router.post('/login')
def login(body: Login, request: Request, response: Response):
    auth.origin(request)
    token, csrf = auth.login(body.password)
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite='strict', max_age=auth.TTL, path='/api/live')
    return {'csrf_token': csrf, 'expires_in': auth.TTL}


@router.post('/logout')
def logout(request: Request, response: Response):
    key = auth.session(request, unsafe=True)
    ledger.put(key, None)
    response.delete_cookie(auth.COOKIE, path='/api/live')
    return {'logged_out': True}


@router.get('/account')
def account(request: Request):
    auth.session(request)
    with ledger.connect() as db:
        intents = [dict(row) for row in db.execute('SELECT * FROM intents ORDER BY created DESC LIMIT 100')]
    return {'account': ledger.get('account'), 'preflight': ledger.get('preflight'), 'intents': intents,
            'read_job': ledger.get('read_job'), 'deployment': ledger.get('deployment'),
            'daily_risk': ledger.get('daily_risk'), 'daily_risk_error': ledger.get('daily_risk_error'),
            'reconciliation_required': ledger.get('reconciliation_required', False),
            'engine_ready': ledger.get('engine_ready'), 'last_order_denial': ledger.get('last_order_denial')}


@router.post('/read-jobs')
def read_jobs(body: ReadJob, request: Request):
    return mutate(request, body.model_dump(), lambda: control.read_job(body.kind))


@router.post('/deployment')
def create(body: Create, request: Request):
    return mutate(request, body.model_dump(), lambda: control.create(body.model_dump()))


@router.post('/deployment/{action}')
def action(action: Literal['activate', 'pause', 'stop_process', 'restart'], body: Action, request: Request):
    return mutate(request, body.model_dump(), lambda: control.action(action, body.revision, body.acknowledge_local_protection_ceases))
