"""Bounded local research API; does not expose arbitrary models, data paths or code."""
import importlib.metadata
import json
import os
import re
import subprocess
import threading
import time
from typing import Literal
from pathlib import PurePosixPath
from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from . import settings, rl_store
from .data import get_dataset, sha256
from .rl_config import PROTOCOL, child_environment, python_path
from .public_worker import terminate_owned

_cached = None
_cache_at = 0
_lock = threading.Lock()


class RLInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='PPO 五种子研究', min_length=1, max_length=100)
    protocol: Literal['ppo-spot-v1'] = 'ppo-spot-v1'


def compatibility_process(command, timeout=90):
    """The Windows venv launcher owns another Python process; reap the entire tree."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, encoding='utf-8', cwd=settings.ROOT,
        env=child_environment(), start_new_session=os.name != 'nt',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except BaseException:
        if process.poll() is None:
            terminate_owned(process)
        # Drain inherited pipe handles only after the owned descendants are terminated.
        process.communicate(timeout=10)
        raise


def readiness():
    global _cached, _cache_at
    with _lock:
        active = any(job['status'] in {'queued', 'running', 'cancelling'} for job in rl_store.list_jobs())
        if active:
            if _cached:
                return {**_cached, 'probe_deferred_while_active': True}
            return {'available': False, 'reasons': ['Active research job; compatibility probe deferred until slot idle'],
                'versions': {}, 'setup_command': 'powershell -ExecutionPolicy Bypass -File scripts/setup-rl.ps1',
                'protocol': PROTOCOL, 'mode': 'historical_research_only', 'release_eligible': False,
                'implementation': 'Standalone SB3 PPO + Freqtrade CSV replay', 'probe_deferred_while_active': True}
        if _cached and time.monotonic() - _cache_at < 60:
            return _cached
        reasons, versions = [], {}
        path = python_path()
        if not path.is_file():
            reasons.append('Isolated RL runtime is missing')
        else:
            probe = "import json,torch,gymnasium as g,importlib.metadata as m; from stable_baselines3 import PPO; torch.set_num_threads(2); e=g.make('CartPole-v1'); p=PPO('MlpPolicy',e,n_steps=8,batch_size=8,n_epochs=1,device='cpu',seed=7); p.learn(8); e.close(); print(json.dumps({k:m.version(k) for k in ('torch','stable-baselines3','gymnasium','numpy','pandas','pyarrow')}))"
            try:
                completed = compatibility_process([str(path), '-c', probe])
                if completed.returncode:
                    reasons.append('RL import/training compatibility probe failed')
                else:
                    versions = json.loads(completed.stdout.strip().splitlines()[-1])
                    expected = {'torch': '2.14.1+cpu', 'stable-baselines3': '2.9.0', 'gymnasium': '1.4.0',
                                'numpy': '2.5.4', 'pandas': '3.0.6', 'pyarrow': '26.0.0'}
                    if versions != expected:
                        reasons.append('RL runtime differs from tested dependency lock')
            except (OSError, ValueError, subprocess.TimeoutExpired):
                reasons.append('RL runtime compatibility probe unavailable')
        try:
            versions['freqtrade'] = importlib.metadata.version('freqtrade')
            if versions['freqtrade'] != '2026.9':
                reasons.append('Expected Freqtrade 2026.9')
        except importlib.metadata.PackageNotFoundError:
            reasons.append('Main Freqtrade environment unavailable')
        manifest = get_dataset()
        try:
            if not manifest or not manifest['available'] or not all(sha256(settings.ROOT / f['path']) == f['sha256'] for f in manifest['files']):
                reasons.append('Dataset missing or hash mismatch')
            elif not (manifest.get('available_start', manifest['start']) <= PROTOCOL['train'][0]
                      and manifest.get('available_end', manifest['end']) >= PROTOCOL['development_evaluation'][1]
                      and PROTOCOL['pair'] in manifest['pairs']):
                reasons.append('Dataset does not cover the frozen development protocol')
        except OSError:
            reasons.append('Dataset unavailable')
        _cached = {'available': not reasons, 'reasons': reasons, 'versions': versions,
            'setup_command': 'powershell -ExecutionPolicy Bypass -File scripts/setup-rl.ps1',
            'protocol': PROTOCOL, 'mode': 'historical_research_only', 'release_eligible': False,
            'implementation': 'Standalone SB3 PPO + Freqtrade CSV replay; not native FreqAI RL'}
        _cache_at = time.monotonic()
        return _cached


def require(identifier):
    job = rl_store.get(identifier)
    if job is None:
        raise HTTPException(404, 'RL job not found')
    return job


def allowed_artifact(relative):
    parts = PurePosixPath(relative).parts
    if not parts or '..' in parts:
        return False
    top = {'worker.log', 'training.log', 'manifest.json', 'protocol.json', 'provenance.json',
           'normalizer.json', 'rl-versions.json', 'progress.json', 'training-complete.json',
           'partial-results.json', 'result.json', 'inventory.json'}
    if len(parts) == 1:
        return parts[0] in top
    if len(parts) == 2 and parts[0] in {f'seed-{seed}' for seed in PROTOCOL['seeds']}:
        return parts[1] in {'model.zip', 'training.json', 'validation-actions.csv', 'validation-gym.json',
                            'development_evaluation-actions.csv', 'development_evaluation-gym.json'}
    if parts == ('market', 'BTC_USDT-1h.feather'):
        return True
    if len(parts) == 2 and parts[0] == 'source':
        return parts[1] in {'rl_config.py', 'rl_env.py', 'rl_train.py', 'rl_runner.py', 'engine.py',
                           'RLActionReplay.py', 'RLResearchSma.py', 'RLResearchBuyHold.py', 'requirements-rl.lock'}
    if len(parts) >= 4 and parts[0] == 'replays' and parts[1] in {'validation', 'development_evaluation'} and parts[2] in ({f'seed-{seed}' for seed in PROTOCOL['seeds']} | {'RLResearchSma', 'RLResearchBuyHold'}):
        if len(parts) == 4:
            return parts[3] in {'effective-config.json', 'provenance.json', 'result.json', 'engine.log'} or bool(re.fullmatch(r'backtest-result-[\d_-]+\.(zip|meta\.json)', parts[3]))
        return len(parts) == 5 and parts[3] == 'strategies' and parts[4] in {'RLActionReplay.py', 'RLResearchSma.py', 'RLResearchBuyHold.py', 'actions.csv'}
    return False


def create_router():
    router = APIRouter(prefix='/api/rl', tags=['RL research'])

    @router.get('/readiness')
    def ready():
        return readiness()

    @router.get('/jobs')
    def jobs():
        return rl_store.list_jobs()

    @router.post('/jobs')
    def create(body: RLInput, response: Response, idempotency_key: str = Header(min_length=1, max_length=100)):
        if not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', idempotency_key):
            raise HTTPException(422, 'Invalid Idempotency-Key')
        try:
            existing = rl_store.find_submission(body.name, idempotency_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if existing:
            return existing
        if not readiness()['available']:
            raise HTTPException(409, readiness())
        try:
            job, created = rl_store.enqueue(body.name, idempotency_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response.status_code = 201 if created else 200
        return job

    @router.get('/jobs/{identifier}')
    def status(identifier: str):
        job = require(identifier)
        path = rl_store.directory(identifier) / 'progress.json'
        try:
            job['progress'] = json.loads(path.read_text()) if path.exists() else None
        except (OSError, ValueError):
            job['progress'] = None
        return job

    @router.post('/jobs/{identifier}/cancel')
    def cancel(identifier: str):
        require(identifier)
        return rl_store.cancel(identifier)

    @router.get('/jobs/{identifier}/results')
    def results(identifier: str):
        job = require(identifier)
        if job['status'] != 'succeeded':
            raise HTTPException(409, {'status': job['status'], 'error': job['error']})
        directory = rl_store.directory(identifier)
        inventory = json.loads((directory / 'inventory.json').read_text(encoding='utf-8'))
        if sha256(directory / 'result.json') != inventory['result.json']['sha256']:
            raise HTTPException(409, 'Result integrity check failed')
        return json.loads((directory / 'result.json').read_text(encoding='utf-8'))

    @router.get('/jobs/{identifier}/logs')
    def logs(identifier: str):
        require(identifier)
        directory = rl_store.directory(identifier)
        output = {}
        for name in ('worker.log', 'training.log'):
            path = directory / name
            if path.is_file():
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 32000))
                    output[name] = stream.read().decode('utf-8', errors='replace')
        return output

    @router.get('/jobs/{identifier}/artifacts')
    def artifacts(identifier: str):
        require(identifier)
        directory = rl_store.directory(identifier)
        return [{'path': p.relative_to(directory).as_posix(), 'bytes': p.stat().st_size,
                 'url': f'/api/rl/jobs/{identifier}/artifacts/{p.relative_to(directory).as_posix()}'}
                for p in directory.rglob('*') if p.is_file() and not p.is_symlink()
                and p.resolve().is_relative_to(directory.resolve()) and allowed_artifact(p.relative_to(directory).as_posix())]

    @router.get('/jobs/{identifier}/artifacts/{artifact_path:path}')
    def artifact(identifier: str, artifact_path: str):
        job = require(identifier)
        directory = rl_store.directory(identifier).resolve()
        path = (directory / artifact_path).resolve()
        if not path.is_relative_to(directory) or not path.is_file() or not allowed_artifact(artifact_path):
            raise HTTPException(404, 'Artifact not found')
        if job['status'] == 'succeeded' and path.name not in {'inventory.json', 'progress.json'}:
            inventory = json.loads((directory / 'inventory.json').read_text(encoding='utf-8'))
            entry = inventory.get(path.relative_to(directory).as_posix())
            if not entry or sha256(path) != entry['sha256']:
                raise HTTPException(409, 'Artifact integrity check failed')
        return FileResponse(path, filename=path.name)

    return router
