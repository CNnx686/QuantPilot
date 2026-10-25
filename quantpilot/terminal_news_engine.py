"""Actual price-plus-news training and inference; v1 price models remain unchanged."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

import numpy as np
import pandas as pd
import psutil

from . import settings, terminal_engine as base
from .rl_config import child_environment, python_path
from .terminal_features import FEATURES as PRICE_FEATURES, candles_frame, features, normalizer

MODEL_SCHEMA = 'terminal-model-news-v1'
ENGINE = base.ENGINE
HOUR = base.HOUR


def news_api():
    from . import terminal_news_features
    return terminal_news_features


def feature_names():
    return [*PRICE_FEATURES, *news_api().NEWS_FEATURES]


def _code_hashes():
    return {**base._code_hashes(), **{name: base._hash(Path(__file__).parent / name) for name in
        ('terminal_news_engine.py', 'terminal_news_rl.py', 'terminal_news_features.py', 'terminal_news.py')}}


def normalize_config(config):
    if config.get('kind') not in ('prediction', 'rl') or config.get('news') is not True:
        raise ValueError('News inputs require a prediction or RL strategy with news enabled')
    if config.get('symbol') not in ('BTC/USDT', 'ETH/USDT'):
        raise ValueError('News inputs currently support BTC/USDT and ETH/USDT only')
    normalized = base.normalize_config({**config, 'news_enabled': False})
    if (pd.Timestamp(normalized['end']) - pd.Timestamp(normalized['start'])).days > 31:
        raise ValueError('News research is bounded to 14–31 days per task')
    return {**normalized, 'news': True}


def _validate_snapshot(snapshot):
    api = news_api()
    if not isinstance(snapshot, dict) or snapshot.get('schema') != api.SCHEMA:
        raise ValueError('Unsupported news snapshot schema')
    if not isinstance(snapshot.get('policy'), dict) or not snapshot.get('mode'):
        raise ValueError('News snapshot has no declared timing/coverage policy')


def checked_factor(snapshot, symbol, cutoff):
    _validate_snapshot(snapshot)
    api = news_api()
    factor = api.factor_at(snapshot, symbol, pd.Timestamp(cutoff))
    if factor.get('schema') != api.SCHEMA or pd.Timestamp(factor['cutoff']) != pd.Timestamp(cutoff):
        raise ValueError('News factor schema or cutoff does not match the decision')
    if factor.get('coverage', {}).get('status') != 'covered':
        return factor
    values = factor.get('values', {})
    if set(values) != set(api.NEWS_FEATURES):
        raise ValueError('News factor columns differ from the frozen definition')
    if any(isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0 for value in values.values()):
        raise ValueError('News factor values must be finite nonnegative log counts')
    maximum = factor.get('max_event_available_at')
    if maximum and pd.Timestamp(maximum) > pd.Timestamp(cutoff):
        raise ValueError('News event was not available at the feature cutoff')
    return factor


def _rl_call(arguments, log_path, timeout):
    executable = python_path()
    if not executable.is_file():
        raise ValueError('The isolated PPO runtime is unavailable')
    command = [str(executable), '-m', 'quantpilot.terminal_news_rl', *map(str, arguments)]
    with Path(log_path).open('w', encoding='utf-8') as log:
        child = subprocess.Popen(command, cwd=settings.ROOT, env=child_environment(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
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
            raise RuntimeError('News PPO subprocess timed out') from error
    if code:
        raise RuntimeError(f'News PPO subprocess failed ({code}); inspect the local task log')


def train(config, output_dir, candles, news_snapshot):
    """Called by the isolated job worker with one frozen, shared comparison input."""
    submitted = json.loads(json.dumps(config, allow_nan=False))
    config = normalize_config(config)
    _validate_snapshot(news_snapshot)
    if news_snapshot['mode'] != 'historical_archive_assumption':
        raise ValueError('Historical training requires the declared archive-availability assumption')
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / 'manifest.json').exists() or (directory / 'result.json').exists():
        raise ValueError('Candidate directory already contains a frozen result')
    started = time.monotonic()
    original = candles_frame(candles)
    period = base._period(config)
    start, end, cutoff = map(pd.Timestamp, (period['requested']['start'], period['requested']['end'], period['train']['end']))
    warmup = max(64, config['parameters']['slow'] + 1)
    frame = original.loc[(original['date'] >= start - warmup * HOUR) & (original['date'] < end)].reset_index(drop=True)
    expected = pd.date_range(start - warmup * HOUR, end, freq='h', inclusive='left')
    if len(frame) != len(expected) or not pd.DatetimeIndex(frame['date']).equals(expected):
        raise ValueError('Complete hourly market coverage and warmup are required')
    columns = feature_names()
    values = features(frame)
    for name in news_api().NEWS_FEATURES:
        values[name] = np.nan
    train_mask = (frame['date'] >= start) & (frame['date'] < cutoff)
    eval_mask = (frame['date'] >= cutoff) & (frame['date'] < end)
    audits = []
    for index, candle in frame.loc[train_mask | eval_mask].iterrows():
        factor = checked_factor(news_snapshot, config['symbol'], candle['date'] + HOUR)
        if factor.get('coverage', {}).get('status') != 'covered':
            raise ValueError(f'News coverage missing at {(candle["date"] + HOUR).isoformat()}; no zero-fill or price fallback')
        for name in news_api().NEWS_FEATURES:
            values.loc[index, name] = float(factor['values'][name])
        audits.append({'candle_at': candle['date'].isoformat(), 'cutoff': factor['cutoff'],
            'coverage': factor['coverage'], 'event_ids': factor.get('event_ids', []),
            'max_event_available_at': factor.get('max_event_available_at'), 'snapshot_id': factor.get('snapshot_id')})
    values = values[columns]
    if not np.isfinite(values.loc[train_mask | eval_mask].to_numpy()).all():
        raise ValueError('Non-finite price/news training inputs')
    frame.to_feather(directory / 'snapshot.feather')
    provenance = base._bind_data_provenance(directory, original, config, directory / 'snapshot.feather')
    base._json(directory / 'news-snapshot.json', news_snapshot)
    base._json(directory / 'news-feature-audit.json', audits)
    matrix = values.loc[train_mask | eval_mask].copy()
    matrix.insert(0, 'date', frame.loc[train_mask | eval_mask, 'date'])
    matrix.reset_index(drop=True).to_feather(directory / 'feature-matrix.feather')
    base._json(directory / 'config.json', submitted)
    base._json(directory / 'effective-config.json', config)
    base._json(directory / 'progress.json', {'phase': 'training'})
    evaluation_frame, evaluation_values = frame.loc[eval_mask], values.loc[eval_mask]
    model_files, predictions = [], None
    training = {'historical_fit_cutoff': cutoff.isoformat(), 'features': columns,
                'news_features': list(news_api().NEWS_FEATURES)}
    if config['kind'] == 'prediction':
        from lightgbm import Booster, LGBMRegressor
        horizon = config['parameters']['horizon']
        target = frame['close'].shift(-horizon) / frame['close'] - 1
        label_at = frame['date'] + (horizon + 1) * HOUR
        allowed = train_mask & (label_at <= cutoff) & np.isfinite(target)
        model = LGBMRegressor(n_estimators=100, num_leaves=15, max_depth=5, min_child_samples=40,
            learning_rate=.05, random_state=config['parameters']['seed'], n_jobs=2,
            deterministic=True, force_col_wise=True, verbosity=-1)
        model.fit(values.loc[allowed], target.loc[allowed])
        model.booster_.save_model(str(directory / 'model.txt'))
        predictions = Booster(model_file=str(directory / 'model.txt')).predict(evaluation_values)
        if not np.isfinite(predictions).all():
            raise ValueError('News model produced invalid evaluation predictions')
        actions = base._prediction_actions(predictions, config)
        training.update(rows=int(allowed.sum()), purged_rows=int(train_mask.sum() - allowed.sum()),
            model_label_max_at=label_at.loc[allowed].max().isoformat(), horizon=horizon,
            target='close[t+horizon]/close[t]-1; label available at the target candle close')
        model_files.append('model.txt')
    else:
        fitted = normalizer(values.loc[train_mask])
        base._json(directory / 'normalizer.json', {**fitted, 'features': columns, 'fit_period': period['train']})
        frame.loc[train_mask].reset_index(drop=True).to_feather(directory / 'rl-train.feather')
        np.save(directory / 'rl-train-values.npy', values.loc[train_mask].to_numpy(), allow_pickle=False)
        np.save(directory / 'evaluation-values.npy', evaluation_values.to_numpy(), allow_pickle=False)
        _rl_call(['train', directory], directory / 'rl-engine.log', 600)
        actions = np.load(directory / 'evaluation-actions.npy', allow_pickle=False)
        if len(actions) != len(evaluation_frame) or not np.isin(actions, (0, 1, 2)).all():
            raise ValueError('Invalid news PPO evaluation actions')
        training.update(rows=int(train_mask.sum()), purged_rows=0, model_label_max_at=cutoff.isoformat(),
                        **base._read(directory / 'rl-training.json'))
        model_files.extend(['model.zip', 'normalizer.json', 'rl-training.json'])
    base._json(directory / 'training-audit.json', training)
    base._json(directory / 'progress.json', {'phase': 'backtesting'})
    result = base.evaluate(evaluation_frame, actions, config)
    evaluation_audits = audits[int(train_mask.sum()):]
    decisions = [{**audit, 'decision_at': audit['cutoff'], 'feature_available_at': audit['cutoff'],
        'historical_fit_cutoff': cutoff.isoformat(), 'action': int(actions[index]),
        'prediction': float(predictions[index]) if predictions is not None else None}
        for index, audit in enumerate(evaluation_audits)]
    base._json(directory / 'evaluation-decisions.json', decisions)
    base._json(directory / 'evaluation.json', result)
    files = ['config.json', 'effective-config.json', 'snapshot.feather', 'data-provenance.json',
        'news-snapshot.json', 'news-feature-audit.json', 'feature-matrix.feather',
        'training-audit.json', 'evaluation-decisions.json', 'evaluation.json', *model_files]
    news_definition = {'schema': news_api().SCHEMA, 'features': list(news_api().NEWS_FEATURES),
        'policy': news_snapshot['policy'], 'mode': news_snapshot['mode'],
        'snapshot_sha256': base._hash(directory / 'news-snapshot.json'),
        'snapshot_id': news_snapshot.get('content_sha256'), 'source': 'GDELT sampled six-hour archive batches',
        'coverage': {'status': 'covered', 'checked_rows': len(audits), 'missing_rows': 0},
        'transform': 'log1p of deduplicated counts', 'risk_gate': 'not_enabled',
        'time_semantics': 'Archived batch plus declared delay for retrospective research; current reception time for live inference'}
    manifest = {'schema': MODEL_SCHEMA, 'feature_schema': news_api().SCHEMA, 'features': columns,
        'id': 'model-' + uuid.uuid4().hex, 'kind': config['kind'], 'symbol': config['symbol'],
        'timeframe': '1h', 'engine': ENGINE, 'config': submitted, 'effective_config': config,
        'config_sha256': base._config_hash(submitted), 'files': {name: base._hash(directory / name) for name in files},
        'code_sha256': _code_hashes(), 'versions': base._versions(), 'period': period, 'training': training,
        'created_at': pd.Timestamp.now(tz='UTC').isoformat(), 'training_seconds': time.monotonic() - started,
        'data_provenance': provenance, 'news': news_definition}
    base._json(directory / 'manifest.json', manifest)
    candidate = {'id': manifest['id'], 'kind': config['kind'], 'symbol': config['symbol'], 'artifact_dir': str(directory),
        'model_manifest': manifest, 'metrics': result['metrics'], 'period': period,
        'equity': result['equity'], 'trades': result['trades'], 'news': news_definition,
        'checks': [{'name': 'Price and sampled news coverage', 'status': 'passed'},
            {'name': 'News timing and frozen feature columns', 'status': 'passed'},
            {'name': 'Chronological train/evaluation split', 'status': 'passed'},
            {'name': 'Known-label cutoff', 'status': 'passed' if config['kind'] == 'prediction' else 'not_applicable'},
            {'name': 'Paper cash/trade reconciliation', 'status': 'passed'},
            {'name': 'Untouched final evaluation', 'status': 'unverified', 'detail': '2025Q4 reserved and not used'}],
        'assumptions': {'engine': ENGINE, 'execution': 'Local paper evaluator, not Freqtrade or exchange fills',
            'fill': 'Closed candle decision; next open fills; final evaluation close liquidation',
            'risk': 'Closed candle close stop, filled next open; news risk gate not enabled',
            'fee_per_side': config['fee'], 'stop_loss': config['parameters']['stop_loss'],
            'slippage': 'Not modeled', 'equity': 'Hourly marked-to-market equity including fees',
            'evaluation': 'Development data; fixed 70% training/30% evaluation; not an untouched test',
            'news': 'Six-hour sampled GDELT archives with declared availability assumption; not complete news coverage',
            'current_paper': 'Current reception-time news inputs; observed public quote virtual fills',
            'fit_time_semantics': 'Historical fitting cutoff idealized; actual training time recorded in manifest'}}
    base._json(directory / 'result.json', candidate)
    base._json(directory / 'progress.json', {'phase': 'succeeded'})
    return candidate


def verify_model(model_dir):
    directory = Path(model_dir).resolve()
    manifest = base._read(directory / 'manifest.json')
    api = news_api()
    if (manifest.get('schema') != MODEL_SCHEMA or manifest.get('feature_schema') != api.SCHEMA
            or manifest.get('features') != feature_names()):
        raise ValueError('Unsupported frozen news model or feature schema')
    if manifest.get('config_sha256') != base._config_hash(manifest['config']):
        raise ValueError('Frozen news configuration hash mismatch')
    config = normalize_config(manifest['config'])
    if config != manifest.get('effective_config') or manifest.get('kind') != config['kind'] or manifest.get('symbol') != config['symbol']:
        raise ValueError('Frozen news model configuration is inconsistent')
    if manifest.get('code_sha256') != _code_hashes() or manifest.get('versions') != base._versions():
        raise ValueError('Frozen news model code or dependency versions changed')
    if config['kind'] == 'rl' and manifest.get('training', {}).get('versions') != base._rl_versions():
        raise ValueError('Frozen news PPO dependency versions changed')
    required = ['config.json', 'effective-config.json', 'snapshot.feather', 'data-provenance.json',
        'news-snapshot.json', 'news-feature-audit.json', 'feature-matrix.feather', 'training-audit.json',
        'evaluation-decisions.json', 'evaluation.json']
    required += ['model.txt'] if config['kind'] == 'prediction' else ['model.zip', 'normalizer.json', 'rl-training.json']
    if not set(required).issubset(manifest.get('files', {})):
        raise ValueError('Frozen news model artifact list is incomplete')
    for relative, expected in manifest['files'].items():
        location = (directory / relative).resolve()
        if Path(relative).is_absolute() or not location.is_relative_to(directory) or not location.is_file() or base._hash(location) != expected:
            raise ValueError('Frozen news model artifact path or hash mismatch')
    if base._read(directory / 'config.json') != manifest['config'] or base._read(directory / 'effective-config.json') != config:
        raise ValueError('Frozen news configuration files differ from manifest')
    snapshot = base._read(directory / 'news-snapshot.json')
    _validate_snapshot(snapshot)
    definition = manifest.get('news', {})
    if (definition.get('schema') != api.SCHEMA or definition.get('features') != list(api.NEWS_FEATURES)
            or definition.get('policy') != snapshot['policy']
            or definition.get('snapshot_sha256') != manifest['files']['news-snapshot.json']):
        raise ValueError('Frozen news feature policy/evidence does not match model')
    provenance = manifest.get('data_provenance', {})
    if (provenance.get('source_document_sha256') != manifest['files']['data-provenance.json']
            or provenance.get('source_evidence') != base._read(directory / 'data-provenance.json')
            or provenance.get('snapshot_sha256') != manifest['files']['snapshot.feather']):
        raise ValueError('Frozen market provenance does not match news model')
    if config['kind'] == 'prediction':
        from lightgbm import Booster
        model = Booster(model_file=str(directory / 'model.txt'))
        if model.feature_name() != feature_names():
            raise ValueError('LightGBM news feature order does not match manifest')
    else:
        fitted = base._read(directory / 'normalizer.json')
        if (fitted.get('features') != feature_names() or len(fitted.get('mean', [])) != len(feature_names())
                or len(fitted.get('scale', [])) != len(feature_names())
                or not np.isfinite(fitted['mean']).all() or not np.isfinite(fitted['scale']).all()
                or min(fitted['scale']) <= 0):
            raise ValueError('Frozen news PPO normalizer columns do not match model')
    return manifest


def predict(model_dir, candles, news_snapshot):
    directory = Path(model_dir).resolve()
    manifest = verify_model(directory)
    frame = candles_frame(candles)
    now = pd.Timestamp.now(tz='UTC')
    frame = frame.loc[frame['date'] + HOUR <= now].reset_index(drop=True)
    if len(frame) < max(49, manifest['effective_config']['parameters']['slow'] + 1):
        raise ValueError('Insufficient closed market warmup for the news model')
    candle = frame.iloc[-1]
    cutoff = candle['date'] + HOUR
    result = {'model_id': manifest['id'], 'decision_at': cutoff.isoformat(), 'feature_available_at': cutoff.isoformat(),
        'model_created_at': manifest['created_at'], 'feature_schema': manifest['feature_schema'],
        'valid': False, 'action': 'hold', 'prediction': None}
    if now - cutoff > pd.Timedelta(hours=2):
        return {**result, 'reason': 'closed_candles_stale'}
    _validate_snapshot(news_snapshot)
    if news_snapshot['policy'] != manifest['news']['policy']:
        raise ValueError('Current news feature policy differs from the selected model')
    if news_snapshot['mode'] != 'observed':
        raise ValueError('Current inference requires observed reception times, not a historical availability assumption')
    factor = checked_factor(news_snapshot, manifest['symbol'], cutoff)
    result['news'] = factor
    if factor.get('coverage', {}).get('status') != 'covered':
        reason = 'news_source_error' if factor.get('coverage', {}).get('status') == 'source_error' else 'news_coverage_missing'
        return {**result, 'reason': reason}
    row = features(frame).iloc[-1].to_dict()
    row.update(factor['values'])
    array = np.asarray([[row[name] for name in manifest['features']]], dtype=float)
    if not np.isfinite(array).all() or float(candle['volume']) <= 0:
        return {**result, 'reason': 'unavailable_price_or_news_features'}
    if manifest['kind'] == 'prediction':
        from lightgbm import Booster
        value = float(Booster(model_file=str(directory / 'model.txt')).predict(array)[0])
        if not math.isfinite(value):
            return {**result, 'reason': 'nonfinite_news_model_prediction'}
        result['prediction'] = value
        action = int(base._prediction_actions(np.asarray([value]), manifest['effective_config'])[0])
        reason = 'lightgbm_price_news_prediction'
    else:
        with tempfile.TemporaryDirectory(prefix='quantpilot-news-infer-') as temporary:
            folder = Path(temporary)
            base._json(folder / 'input.json', {'features': array.tolist(), 'feature_names': manifest['features']})
            _rl_call(['infer', directory, folder / 'input.json', folder / 'output.json'], folder / 'infer.log', 90)
            action = int(base._read(folder / 'output.json')['actions'][0])
        reason = 'ppo_price_news_current_candle'
    if action not in (0, 1, 2):
        return {**result, 'reason': 'invalid_news_model_action'}
    return {**result, 'valid': True, 'action': ('hold', 'buy', 'sell')[action], 'raw_action': action,
            'reason': reason, 'features': dict(zip(manifest['features'], map(float, array[0]), strict=True))}
