import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantpilot.terminal_metrics import evaluate_candidate, prediction_metrics, risk_metrics


def equity(returns, *, missing_first=False):
    values = [100.0]
    for ret in returns:
        values.append(values[-1] * (1 + ret))
    dates = pd.date_range('2025-01-01', periods=len(values), freq='h', tz='UTC')
    result = [{'time': str(date), 'value': value} for date, value in zip(dates, values)]
    if missing_first:
        result.pop(1)
    return result


def test_risk_annualization_and_empty_denominators():
    returns = [0.01, -0.005] * 12
    actual = risk_metrics(equity(returns))
    assert actual['sharpe'] == pytest.approx(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(8760))
    assert actual['sortino'] == pytest.approx(np.mean(returns) / np.sqrt(np.mean(np.minimum(returns, 0) ** 2)) * np.sqrt(8760))
    assert risk_metrics(equity(returns[:5]))['sharpe'] is None
    flat = risk_metrics(equity([0] * 30))
    assert flat['sharpe'] is None and flat['sortino'] is None
    rising = risk_metrics(equity([.001, .002] * 12))
    assert rising['sharpe'] is not None and rising['sortino'] is None


def test_reconstructed_initial_bar_and_bad_gap():
    returns = [0.0] + [0.01, -0.005] * 12
    actual = risk_metrics(equity(returns, missing_first=True))
    assert actual['initial_flat_hour_reconstructed'] is True
    assert actual['observations'] == 25
    bad = equity([0] * 30)
    bad.pop(5)
    with pytest.raises(ValueError, match='timestamps'):
        risk_metrics(bad)


def test_predictions_align_close_times_and_exclude_future():
    candles = pd.DataFrame({'date': pd.date_range('2025-01-01', periods=5, freq='h', tz='UTC'),
                            'close': [100, 110, 99, 99, 120]})
    decisions = [{'decision_at': str(date), 'prediction': prediction} for date, prediction in
                 zip(pd.date_range('2025-01-01 01:00', periods=5, freq='h', tz='UTC'), [.05, -.2, 0, .3, .9])]
    period = {'evaluation': {'start':'2025-01-01T00:00:00+00:00', 'end':'2025-01-01T05:00:00+00:00'}}
    result = prediction_metrics(candles, decisions, period, 1)
    assert result['observations'] == 4 and result['excluded_unavailable'] == 1
    assert result['direction_accuracy'] == 1.0
    actual = np.array([.1, -.1, 0, 120/99-1])
    delta = np.array([.05, -.2, 0, .3]) - actual
    assert result['mae'] == pytest.approx(np.mean(np.abs(delta)))
    assert result['rmse'] == pytest.approx(np.sqrt(np.mean(delta ** 2)))
    with pytest.raises(ValueError, match='duplicated'):
        prediction_metrics(candles, decisions + decisions[:1], period, 1)


def test_candidate_hash_mismatch_rejected(tmp_path):
    names = ('snapshot.feather', 'evaluation-decisions.json', 'evaluation.json')
    for name in names:
        (tmp_path / name).write_text('data', encoding='utf-8')
    manifest = {'kind':'rule', 'files': {n:hashlib.sha256((tmp_path/n).read_bytes()).hexdigest() for n in names}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (tmp_path/'evaluation.json').write_text('tampered', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch'):
        evaluate_candidate({'artifact_dir':str(tmp_path), 'model_manifest':manifest})
