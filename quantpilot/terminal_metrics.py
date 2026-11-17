"""Recompute development metrics from immutable terminal model artifacts.

This module does not change frozen model files or their historical accounting.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ANNUAL_HOURS = 365 * 24
MIN_RETURNS = 24


def risk_metrics(equity):
    """Annualized hourly Sharpe and Sortino, zero risk-free/minimum return.

    Sharpe uses sample standard deviation (ddof=1); Sortino uses the root
    mean square of min(hourly return, 0) over all hours. Insufficient data or
    a zero denominator is undefined, represented as None, never infinity.
    """
    dates = pd.to_datetime([p['time'] for p in equity], utc=True)
    values = np.asarray([p['value'] for p in equity], dtype=float)
    if len(values) and (not np.isfinite(values).all() or np.any(values <= 0)):
        raise ValueError('Equity must contain positive finite values')
    # The existing evaluator emits initial equity at evaluation open, then
    # first marks after the next bar closes. Its first bar has no position or
    # action, so reconstruct that one missing close at the initial cash value.
    reconstructed = False
    if len(dates) > 1:
        gaps = np.diff(dates.as_unit('ns').asi8)
        hour = pd.Timedelta(hours=1).value
        if gaps[0] == 2 * hour and np.all(gaps[1:] == hour):
            values = np.insert(values, 1, values[0])
            reconstructed = True
        elif not np.all(gaps == hour):
            raise ValueError('Equity timestamps must be unique, ordered and hourly')
    returns = values[1:] / values[:-1] - 1
    if not np.isfinite(returns).all():
        raise ValueError('Equity returns must be finite')
    n = len(returns)
    sharpe = sortino = None
    if n >= MIN_RETURNS:
        mean = float(np.mean(returns))
        deviation = float(np.std(returns, ddof=1))
        downside = float(np.sqrt(np.mean(np.minimum(returns, 0.0) ** 2)))
        if deviation > 1e-12:
            sharpe = mean / deviation * math.sqrt(ANNUAL_HOURS)
        if downside > 1e-12:
            sortino = mean / downside * math.sqrt(ANNUAL_HOURS)
    return {'sharpe': sharpe, 'sortino': sortino, 'observations': n,
            'annualization': ANNUAL_HOURS, 'risk_free_rate': 0.0,
            'minimum_observations': MIN_RETURNS, 'initial_flat_hour_reconstructed': reconstructed}


def prediction_metrics(candles, decisions, period, horizon):
    """Evaluate predictions whose future close is known inside evaluation.

    A bar stamped t is known at t+1h. A prediction made at that close targets
    close[t+h]/close[t]-1, so its target is available at decision_at+h hours.
    The evaluation end is a half-open candle boundary and a target whose close
    equals that boundary is available. Later labels are excluded.
    """
    start = pd.Timestamp(period['evaluation']['start'])
    end = pd.Timestamp(period['evaluation']['end'])
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError('Evaluation interval must be timezone aware')
    if not isinstance(horizon, int) or horizon < 1:
        raise ValueError('Invalid prediction horizon')
    frame = candles[['date', 'close']].copy()
    frame['available_at'] = pd.to_datetime(frame['date'], utc=True) + pd.Timedelta(hours=1)
    if frame['available_at'].duplicated().any():
        raise ValueError('Duplicate price timestamps')
    closes = dict(zip(frame['available_at'], frame['close']))
    observed, predicted, excluded = [], [], 0
    seen = set()
    for decision in decisions:
        at = pd.Timestamp(decision['decision_at'])
        if at.tzinfo is None or at <= start or at > end or at in seen:
            raise ValueError('Decision outside evaluation interval or duplicated')
        seen.add(at)
        prediction = decision.get('prediction')
        if prediction is None or not math.isfinite(float(prediction)):
            raise ValueError('Prediction is missing or invalid')
        target_at = at + pd.Timedelta(hours=horizon)
        if target_at > end:
            excluded += 1
            continue
        base, target = closes.get(at), closes.get(target_at)
        if base is None or target is None or not np.isfinite([base, target]).all() or base <= 0 or target <= 0:
            raise ValueError('Evaluation label is missing or invalid')
        observed.append(float(target / base - 1))
        predicted.append(float(prediction))
    count = len(observed)
    if count:
        delta = np.asarray(predicted) - np.asarray(observed)
        mae = float(np.mean(np.abs(delta)))
        rmse = float(np.sqrt(np.mean(delta ** 2)))
        accuracy = float(np.mean(np.sign(predicted) == np.sign(observed)))
    else:
        mae = rmse = accuracy = None
    return {'mae': mae, 'rmse': rmse, 'direction_accuracy': accuracy,
            'observations': count, 'excluded_unavailable': excluded,
            'horizon_hours': horizon}


def evaluate_candidate(candidate):
    """Verify and recompute from the candidate's declared immutable inputs."""
    directory = Path(candidate['artifact_dir']).resolve()
    manifest = candidate['model_manifest']
    if json.loads((directory / 'manifest.json').read_text(encoding='utf-8')) != manifest:
        raise ValueError('Candidate manifest mismatch')
    files = manifest['files']
    for name in ('snapshot.feather', 'evaluation-decisions.json', 'evaluation.json'):
        path = directory / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if files.get(name) != actual:
            raise ValueError(f'Candidate artifact hash mismatch: {name}')
    result = json.loads((directory / 'evaluation.json').read_text(encoding='utf-8'))
    metrics = {'risk': risk_metrics(result['equity']), 'prediction': None,
        'methodology': {'risk': 'Hourly marked-to-market equity returns; annualized by sqrt(8760); zero risk-free and downside threshold; sample standard deviation for Sharpe; downside RMS over all hours for Sortino. Results are descriptive, not a guarantee.',
            'prediction': 'Evaluation-only labels known by evaluation end; MAE/RMSE in fractional returns; directional accuracy treats zero as its own direction.',
            'scope': 'development evaluation, not untouched final test'}}
    if manifest['kind'] == 'prediction':
        decisions = json.loads((directory / 'evaluation-decisions.json').read_text(encoding='utf-8'))
        frame = pd.read_feather(directory / 'snapshot.feather')
        metrics['prediction'] = prediction_metrics(frame, decisions, manifest['period'],
            int(manifest['effective_config']['parameters']['horizon']))
    return metrics
