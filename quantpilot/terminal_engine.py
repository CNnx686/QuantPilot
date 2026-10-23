"""Frozen rule/LightGBM/PPO candidates and real inference for the personal terminal.

This is a deliberately separate local paper evaluator, not Freqtrade dry-run.
Existing Freqtrade research and its immutable artifacts remain untouched.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time
import uuid

import numpy as np
import pandas as pd
import psutil

from . import settings
from .rl_config import child_environment, python_path
from .terminal_features import (FEATURES, SCHEMA, candles_frame, close_position,
                                execute_bar, features, new_account, normalizer)

MODEL_SCHEMA = 'terminal-model-v1'
ENGINE = 'local_paper_v1'
HOUR = pd.Timedelta(hours=1)


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _code_hashes():
    return {name: _hash(Path(__file__).parent / name) for name in
            ('terminal_engine.py', 'terminal_features.py', 'terminal_rl.py')}


def _versions():
    return {'python': platform.python_version(), **{package: importlib.metadata.version(package)
            for package in ('numpy', 'pandas', 'lightgbm')}}


def _rl_versions():
    """Inspect the isolated runtime's own metadata without importing Torch in the API."""
    executable = python_path()
    if not executable.is_file():
        raise ValueError('Frozen PPO runtime is no longer available')
    environment = executable.parent.parent
    locations = ([environment / 'Lib' / 'site-packages'] if os.name == 'nt' else
                 list((environment / 'lib').glob('python*/site-packages')))
    packages = {}
    for distribution in importlib.metadata.distributions(path=[str(path) for path in locations]):
        name = (distribution.metadata.get('Name') or '').lower().replace('_', '-')
        if name in ('torch', 'stable-baselines3', 'gymnasium', 'numpy'):
            packages[name] = distribution.version
    if len(packages) != 4:
        raise ValueError('Frozen PPO runtime dependencies are unavailable')
    return packages


def _bind_data_provenance(directory, original_frame, config, snapshot):
    source = directory.parent / 'data-provenance.json'
    destination = directory / 'data-provenance.json'
    if source.is_file():
        evidence = _read(source)
        original_file = directory.parent / 'market.feather'
        if (evidence.get('symbol') != config['symbol'] or not original_file.is_file()
                or evidence.get('data_sha256') != _hash(original_file)
                or not candles_frame(pd.read_feather(original_file)).equals(original_frame)):
            raise ValueError('Historical source provenance does not match the supplied training data')
        shutil.copy2(source, destination)
        source_verified = True
    else:
        # Direct callers and unit fixtures have no market-adapter evidence. Do not
        # describe arbitrary OHLCV inputs as verified Binance data.
        evidence = {'source': 'Caller-supplied OHLCV; origin unverified', 'symbol': config['symbol'],
                    'rows': len(original_frame), 'source_verified': False}
        _json(destination, evidence)
        source_verified = False
    return {'source': evidence['source'], 'source_verified': source_verified,
            'source_evidence': evidence, 'source_document_sha256': _hash(destination),
            'snapshot_sha256': _hash(snapshot), 'rows': len(pd.read_feather(snapshot)),
            'timestamp_meaning': 'UTC candle open; OHLCV available one hour later'}


def _number(value, name, lower, upper, integer=False):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be numeric')
    numeric = float(value)
    if not math.isfinite(numeric) or not lower <= numeric <= upper:
        raise ValueError(f'{name} outside [{lower}, {upper}]')
    if integer and numeric != int(numeric):
        raise ValueError(f'{name} must be an integer')
    return int(numeric) if integer else numeric


def normalize_config(config):
    kind = config.get('kind')
    if kind not in ('rule', 'prediction', 'rl'):
        raise ValueError('Unsupported strategy kind')
    symbol = config.get('symbol', '')
    if not isinstance(symbol, str) or not symbol.endswith('/USDT') or not symbol[:-5].isalnum():
        raise ValueError('Select a supported USDT spot symbol')
    if config.get('timeframe', '1h') != '1h':
        raise ValueError('Only hourly candles are supported')
    start, end = pd.Timestamp(config['start'], tz='UTC'), pd.Timestamp(config['end'], tz='UTC')
    if not (pd.Timestamp('2025-01-01', tz='UTC') <= start < end <= pd.Timestamp('2025-10-01', tz='UTC')):
        raise ValueError('Development window must stay in [2025-01-01, 2025-10-01); Q4 remains reserved')
    if start != start.floor('D') or end != end.floor('D') or (end - start).days < 14:
        raise ValueError('Research requires at least 14 complete UTC days')
    source = config.get('parameters') or {}
    parameters = {
        'fast': _number(source.get('fast', 12), 'fast', 2, 64, True),
        'slow': _number(source.get('slow', 48), 'slow', 3, 128, True),
        'horizon': _number(source.get('horizon', 6), 'horizon', 1, 24, True),
        'entry_threshold': _number(source.get('entry_threshold', .003), 'entry_threshold', 0, .2),
        'exit_threshold': _number(source.get('exit_threshold', 0), 'exit_threshold', -.2, .2),
        'stop_loss': _number(source.get('stop_loss', .05), 'stop_loss', .001, .5),
        'seed': _number(source.get('seed', 7), 'seed', 0, 1_000_000, True),
        'rl_steps': _number(source.get('rl_steps', source.get('steps', 2048)), 'rl_steps', 2048, 8192, True),
    }
    if parameters['fast'] >= parameters['slow']:
        raise ValueError('Fast moving average must be shorter than slow')
    if parameters['rl_steps'] not in (2048, 8192):
        raise ValueError('RL budget must be 2048 or 8192 steps')
    if parameters['exit_threshold'] >= parameters['entry_threshold']:
        raise ValueError('Exit threshold must be below entry threshold')
    wallet = _number(config.get('wallet', 10000), 'wallet', 100, 1_000_000_000)
    stake = _number(config.get('stake', 1000), 'stake', 10, wallet)
    fee = _number(config.get('fee', .001), 'fee', 0, .02)
    if stake * (1 + fee) > wallet:
        raise ValueError('Virtual capital must cover stake and entry fee')
    if config.get('news_enabled') or source.get('news_enabled'):
        raise ValueError('News features are not yet supported by this price-only model schema')
    return {'kind': kind, 'symbol': symbol, 'timeframe': '1h', 'start': start.date().isoformat(),
            'end': end.date().isoformat(), 'wallet': wallet, 'stake': stake, 'fee': fee, 'parameters': parameters}


def _period(config):
    start, end = pd.Timestamp(config['start'], tz='UTC'), pd.Timestamp(config['end'], tz='UTC')
    cutoff = start + pd.Timedelta(days=int((end - start).days * .7))
    return {'requested': {'start': start.isoformat(), 'end': end.isoformat()},
            'train': {'start': start.isoformat(), 'end': cutoff.isoformat()},
            'evaluation': {'start': cutoff.isoformat(), 'end': end.isoformat()},
            'final_reserved_not_run': {'start': '2025-10-01T00:00:00+00:00', 'end': '2026-01-01T00:00:00+00:00'}}


def _rl_call(arguments, log_path, timeout):
    executable = python_path()
    if not executable.is_file():
        raise ValueError('The isolated PPO runtime is unavailable; run the local RL setup first')
    command = [str(executable), '-m', 'quantpilot.terminal_rl', *map(str, arguments)]
    with Path(log_path).open('w', encoding='utf-8') as log:
        child = subprocess.Popen(command, cwd=settings.ROOT, env=child_environment(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            # Windows venv launchers can have a Python descendant; kill only this owned tree.
            try:
                parent = psutil.Process(child.pid)
                targets = parent.children(recursive=True)
                for process in reversed(targets):
                    try:
                        process.kill()
                    except psutil.NoSuchProcess:
                        pass
                parent.kill()
                psutil.wait_procs([*targets, parent], timeout=5)
            except psutil.NoSuchProcess:
                pass
            child.wait(timeout=5)
            raise RuntimeError('PPO subprocess timed out') from error
    if code:
        raise RuntimeError(f'PPO subprocess failed ({code}); inspect local engine log')


def _rule_actions(frame, parameters):
    fast = frame['close'].rolling(parameters['fast']).mean()
    slow = frame['close'].rolling(parameters['slow']).mean()
    buy = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    sell = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    return np.where(buy & (frame['volume'] > 0), 1, np.where(sell & (frame['volume'] > 0), 2, 0))


def _prediction_actions(predictions, config):
    parameters = config['parameters']
    return np.where(predictions > parameters['entry_threshold'], 1,
                    np.where(predictions < parameters['exit_threshold'], 2, 0))


def evaluate(frame, actions, config):
    """Fixed-budget paper results, explicit next-open fills and closed-candle stops."""
    frame = frame.reset_index(drop=True)
    if len(frame) != len(actions) or len(frame) < 2:
        raise ValueError('Incomplete evaluation decisions')
    account = new_account(config['wallet'])
    equities = [{'time': frame.iloc[0]['date'].isoformat(), 'value': config['wallet']}]
    for index in range(1, len(frame)):
        _, equity = execute_bar(account, int(actions[index - 1]), frame.iloc[index], config,
                                previous_close=frame.iloc[index - 1]['close'])
        equities.append({'time': (frame.iloc[index]['date'] + HOUR).isoformat(), 'value': equity})
    close_position(account, frame.iloc[-1]['close'], config['fee'], frame.iloc[-1]['date'] + HOUR, 'evaluation_end')
    equities[-1]['value'] = account['cash']
    array = np.asarray([point['value'] for point in equities])
    final = account['cash']
    profits = [trade['profit_abs'] for trade in account['trades']]
    if not math.isclose(config['wallet'] + sum(profits), final, abs_tol=1e-7):
        raise ValueError('Evaluation funds do not reconcile')
    return {'metrics': {'net_return': final / config['wallet'] - 1,
                'max_drawdown': float(np.max(1 - array / np.maximum.accumulate(array))),
                'trades': len(profits), 'profit_abs': final - config['wallet'],
                'initial_balance': config['wallet'], 'final_balance': final,
                'turnover': account['turnover'] / config['wallet'],
                'win_rate': sum(value > 0 for value in profits) / len(profits) if profits else None},
            'equity': equities, 'trades': account['trades']}


def train(config, output_dir, candles):
    """Run in the service's training worker, never an HTTP request handler."""
    submitted_config = json.loads(json.dumps(config, allow_nan=False))
    config = normalize_config(config)
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / 'manifest.json').exists() or (directory / 'result.json').exists():
        raise ValueError('Candidate directory already contains a frozen result')
    started = time.monotonic()
    frame = candles_frame(candles)
    original_frame = frame.copy()
    period = _period(config)
    start, end, cutoff = map(pd.Timestamp, (period['requested']['start'], period['requested']['end'], period['train']['end']))
    warmup = max(64, config['parameters']['slow'] + 1)
    frame = frame.loc[(frame['date'] >= start - warmup * HOUR) & (frame['date'] < end)].reset_index(drop=True)
    expected = pd.date_range(start - warmup * HOUR, end, freq='h', inclusive='left')
    if len(frame) != len(expected) or not pd.DatetimeIndex(frame['date']).equals(expected):
        raise ValueError(f'Research requires complete requested data plus {warmup} warmup candles')
    values = features(frame)
    train_mask = (frame['date'] >= start) & (frame['date'] < cutoff)
    eval_mask = (frame['date'] >= cutoff) & (frame['date'] < end)
    evaluation_frame, evaluation_values = frame.loc[eval_mask], values.loc[eval_mask]
    if not np.isfinite(values.loc[train_mask | eval_mask].to_numpy()).all():
        raise ValueError('Missing research features after warmup')
    frame.to_feather(directory / 'snapshot.feather')
    data_provenance = _bind_data_provenance(directory, original_frame, config, directory / 'snapshot.feather')
    _json(directory / 'config.json', submitted_config)
    _json(directory / 'effective-config.json', config)
    _json(directory / 'progress.json', {'phase': 'training' if config['kind'] != 'rule' else 'backtesting'})
    model_files, training_audit = [], {'rows': 0, 'purged_rows': 0, 'historical_fit_cutoff': cutoff.isoformat()}
    predictions = None
    if config['kind'] == 'rule':
        actions = _rule_actions(frame, config['parameters'])[eval_mask.to_numpy()]
        _json(directory / 'rule.json', {'template': 'sma-cross-v1', 'parameters': config['parameters']})
        model_files.append('rule.json')
    elif config['kind'] == 'prediction':
        from lightgbm import LGBMRegressor, Booster
        horizon = config['parameters']['horizon']
        targets = frame['close'].shift(-horizon) / frame['close'] - 1
        label_available = frame['date'] + (horizon + 1) * HOUR
        allowed = train_mask & (label_available <= cutoff) & np.isfinite(targets)
        model = LGBMRegressor(n_estimators=100, num_leaves=15, max_depth=5, min_child_samples=40,
            learning_rate=.05, random_state=config['parameters']['seed'], n_jobs=2,
            deterministic=True, force_col_wise=True, verbosity=-1)
        model.fit(values.loc[allowed], targets.loc[allowed])
        model.booster_.save_model(str(directory / 'model.txt'))
        predictions = Booster(model_file=str(directory / 'model.txt')).predict(evaluation_values)
        if not np.isfinite(predictions).all():
            raise ValueError('Model produced invalid evaluation predictions')
        actions = _prediction_actions(predictions, config)
        training_audit.update(rows=int(allowed.sum()), purged_rows=int(train_mask.sum() - allowed.sum()),
            model_label_max_at=label_available.loc[allowed].max().isoformat(), horizon=horizon,
            target='close[t+horizon]/close[t]-1; label known at candle t+horizon close')
        model_files.append('model.txt')
    else:
        fitted = normalizer(values.loc[train_mask])
        _json(directory / 'normalizer.json', {**fitted, 'features': FEATURES, 'fit_period': period['train']})
        frame.loc[train_mask].reset_index(drop=True).to_feather(directory / 'rl-train.feather')
        np.save(directory / 'rl-train-values.npy', values.loc[train_mask].to_numpy(), allow_pickle=False)
        np.save(directory / 'evaluation-values.npy', evaluation_values.to_numpy(), allow_pickle=False)
        _rl_call(['train', directory], directory / 'rl-engine.log', timeout=600)
        actions = np.load(directory / 'evaluation-actions.npy', allow_pickle=False)
        if len(actions) != len(evaluation_frame) or not np.isin(actions, (0, 1, 2)).all():
            raise ValueError('PPO evaluation actions are invalid')
        training_audit.update(rows=int(train_mask.sum()), model_label_max_at=cutoff.isoformat(),
                              **_read(directory / 'rl-training.json'))
        model_files.extend(['model.zip', 'normalizer.json', 'rl-training.json'])
    _json(directory / 'training-audit.json', training_audit)
    _json(directory / 'progress.json', {'phase': 'backtesting'})
    result = evaluate(evaluation_frame, actions, config)
    decision_rows = []
    for index, (_, candle) in enumerate(evaluation_frame.iterrows()):
        decision_rows.append({'decision_at': (candle['date'] + HOUR).isoformat(),
            'feature_available_at': (candle['date'] + HOUR).isoformat(),
            'historical_fit_cutoff': cutoff.isoformat(), 'action': int(actions[index]),
            'prediction': float(predictions[index]) if predictions is not None else None})
    _json(directory / 'evaluation-decisions.json', decision_rows)
    _json(directory / 'evaluation.json', result)
    file_names = ['config.json', 'effective-config.json', 'snapshot.feather', 'data-provenance.json',
                  'training-audit.json', 'evaluation-decisions.json', 'evaluation.json', *model_files]
    manifest = {'schema': MODEL_SCHEMA, 'feature_schema': SCHEMA, 'id': 'model-' + uuid.uuid4().hex,
        'kind': config['kind'], 'symbol': config['symbol'], 'timeframe': '1h', 'engine': ENGINE,
        'config': submitted_config, 'effective_config': config,
        'config_sha256': _config_hash(submitted_config), 'features': FEATURES,
        'files': {name: _hash(directory / name) for name in file_names}, 'code_sha256': _code_hashes(),
        'versions': _versions(),
        'period': period, 'training': training_audit, 'created_at': pd.Timestamp.now(tz='UTC').isoformat(),
        'training_seconds': time.monotonic() - started,
        'data_provenance': data_provenance}
    _json(directory / 'manifest.json', manifest)
    candidate = {'id': manifest['id'], 'kind': config['kind'], 'symbol': config['symbol'],
        'artifact_dir': str(directory), 'model_manifest': manifest, 'metrics': result['metrics'],
        'period': period, 'equity': result['equity'], 'trades': result['trades'],
        'checks': [{'name': 'Data continuity and immutable snapshot', 'status': 'passed'},
                   {'name': 'Chronological split and training-only preprocessing', 'status': 'passed'},
                   {'name': 'Known-label cutoff', 'status': 'passed' if config['kind'] == 'prediction' else 'not_applicable'},
                   {'name': 'Paper cash/trade reconciliation', 'status': 'passed'},
                   {'name': 'Untouched final evaluation', 'status': 'unverified', 'detail': '2025Q4 reserved and not used'},
                   {'name': 'Profitability', 'status': 'not_required', 'detail': 'Losses and zero trades do not block simulation'}],
        'assumptions': {'engine': ENGINE, 'execution': 'Local paper evaluator, not Freqtrade or exchange fills',
            'fill': 'Closed candle decision; next candle open execution; final position closes at evaluation end',
            'risk': 'Stop triggers on a closed candle close, fills next open; no intrabar low simulation',
            'stop_loss': config['parameters']['stop_loss'], 'fee_per_side': config['fee'],
            'slippage': 'Not modeled in historical evaluation', 'equity': 'Hourly marked-to-market equity including fees',
            'evaluation': 'Development/validation only; fixed 70% train and 30% evaluation; not an untouched test',
            'fit_time_semantics': 'Historical fitting cutoff is idealized; actual training performed at created_at',
            'current_paper': 'Current closed-bar inference with current public bid/ask virtual fills; not identical historical fills'}}
    _json(directory / 'result.json', candidate)
    _json(directory / 'progress.json', {'phase': 'succeeded'})
    return candidate


def verify_model(model_dir):
    directory = Path(model_dir).resolve()
    manifest = _read(directory / 'manifest.json')
    if manifest.get('schema') != MODEL_SCHEMA or manifest.get('feature_schema') != SCHEMA or manifest.get('features') != FEATURES:
        raise ValueError('Unsupported frozen model or feature schema')
    if manifest.get('config_sha256') != _config_hash(manifest['config']):
        raise ValueError('Frozen model configuration hash mismatch')
    normalized = normalize_config(manifest['config'])
    if normalized != manifest.get('effective_config') or manifest['kind'] != normalized['kind'] or manifest['symbol'] != normalized['symbol']:
        raise ValueError('Frozen model metadata is inconsistent')
    if manifest.get('code_sha256') != _code_hashes():
        raise ValueError('Model code version is incompatible; train a new version')
    if manifest.get('versions') != _versions():
        raise ValueError('Model dependency versions changed; restore the frozen runtime or train a new version')
    if manifest['kind'] == 'rl' and manifest.get('training', {}).get('versions') != _rl_versions():
        raise ValueError('PPO dependency versions changed; restore the frozen runtime or train a new version')
    required = {'rule': ['rule.json'], 'prediction': ['model.txt'], 'rl': ['model.zip', 'normalizer.json', 'rl-training.json']}[manifest['kind']]
    if not set(['config.json', 'effective-config.json', 'snapshot.feather', 'data-provenance.json', 'training-audit.json', *required]).issubset(manifest.get('files', {})):
        raise ValueError('Frozen model artifact list is incomplete')
    for relative, expected in manifest['files'].items():
        location = (directory / relative).resolve()
        if Path(relative).is_absolute() or not location.is_relative_to(directory) or not location.is_file() or _hash(location) != expected:
            raise ValueError('Frozen model artifact missing, outside workspace, or hash mismatch')
    if _read(directory / 'config.json') != manifest['config']:
        raise ValueError('Frozen configuration file does not match manifest')
    if _read(directory / 'effective-config.json') != normalized:
        raise ValueError('Frozen effective configuration does not match manifest')
    provenance = manifest.get('data_provenance', {})
    if (provenance.get('source_document_sha256') != manifest['files']['data-provenance.json']
            or provenance.get('source_evidence') != _read(directory / 'data-provenance.json')
            or provenance.get('snapshot_sha256') != manifest['files']['snapshot.feather']):
        raise ValueError('Frozen data provenance does not match model package')
    if manifest['kind'] == 'rl':
        fitted = _read(directory / 'normalizer.json')
        if fitted.get('features') != FEATURES or len(fitted.get('mean', [])) != 6 or len(fitted.get('scale', [])) != 6:
            raise ValueError('PPO normalizer feature contract mismatch')
    return manifest


def predict(model_dir, candles):
    """Infer from the actual selected model and latest available closed hourly bar."""
    directory = Path(model_dir).resolve()
    manifest = verify_model(directory)
    config = manifest['effective_config']
    frame = candles_frame(candles)
    now = pd.Timestamp.now(tz='UTC')
    frame = frame.loc[frame['date'] + HOUR <= now].reset_index(drop=True)
    if len(frame) < max(49, config['parameters']['slow'] + 1):
        raise ValueError('Insufficient complete warmup candles for current inference')
    last = frame.iloc[-1]
    available = last['date'] + HOUR
    base = {'model_id': manifest['id'], 'decision_at': available.isoformat(),
            'feature_available_at': available.isoformat(), 'model_created_at': manifest['created_at'],
            'action': 'hold', 'prediction': None, 'valid': False, 'feature_schema': SCHEMA}
    # Old historical bars must not be advertised as a current live inference.
    if now - available > pd.Timedelta(hours=2):
        return {**base, 'reason': 'closed_candles_stale'}
    values = features(frame).iloc[-1:].to_numpy()
    if not np.isfinite(values).all() or float(last['volume']) <= 0:
        return {**base, 'reason': 'unavailable_price_features_or_zero_volume'}
    if config['kind'] == 'rule':
        action = int(_rule_actions(frame, config['parameters'])[-1])
        reason = 'sma_cross' if action else 'no_sma_cross'
    elif config['kind'] == 'prediction':
        from lightgbm import Booster
        value = float(Booster(model_file=str(directory / 'model.txt')).predict(values)[0])
        if not math.isfinite(value):
            return {**base, 'reason': 'nonfinite_model_prediction'}
        base['prediction'] = value
        action = int(_prediction_actions(np.asarray([value]), config)[0])
        reason = 'lightgbm_return_prediction'
    else:
        # No action cache, historical action CSV or replay is consulted here.
        with tempfile.TemporaryDirectory(prefix='quantpilot-current-infer-') as temporary:
            folder = Path(temporary)
            _json(folder / 'input.json', {'features': values.tolist()})
            _rl_call(['infer', directory, folder / 'input.json', folder / 'output.json'], folder / 'infer.log', timeout=90)
            action = int(_read(folder / 'output.json')['actions'][0])
        reason = 'ppo_current_closed_candle'
    if action not in (0, 1, 2):
        return {**base, 'reason': 'invalid_model_action'}
    return {**base, 'valid': True, 'action': ('hold', 'buy', 'sell')[action], 'raw_action': action,
            'reason': reason, 'features': dict(zip(FEATURES, map(float, values[0]), strict=True))}
