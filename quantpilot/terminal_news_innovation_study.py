"""Causal short-term innovation of sampled news counts; offline research only.

Every arm uses the same training rows, costs, snapshot, random seed and split.
The first row has no earlier news history and is excluded from training in all
arms. Research outputs are not registered as paper-trading candidates.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor

from .terminal_engine import HOUR, _prediction_actions, evaluate
from .terminal_features import FEATURES
from .terminal_metrics import prediction_metrics, risk_metrics
from .terminal_news_features import NEWS_FEATURES

SEEDS = (7, 42, 99)
SOURCES = (
    ('2025-01-01', 'artifacts/news-threshold-20261117/window-1/seed-7-price_news', False),
    ('2025-01-15', 'artifacts/news-threshold-20261117/window-2/seed-7-price_news', False),
    ('2025-04-01', 'artifacts/news-threshold-20261117/window-3/seed-7-price_news', False),
    ('2025-05-31', 'artifacts/news-threshold-supplement-20261117/window-1/seed-7-price_news', True),
)
INNOVATION = ('news_count_innovation_24h', 'news_sources_innovation_24h')
ARMS = {'price': FEATURES, 'raw_news': FEATURES + NEWS_FEATURES,
        'news_innovation': FEATURES + NEWS_FEATURES + list(INNOVATION)}


def add_innovation(values):
    """Current factor minus mean of strictly prior 24 available hourly values."""
    if not set(NEWS_FEATURES).issubset(values.columns):
        raise ValueError('Missing declared news columns')
    result = values.copy()
    for source, dest in zip(NEWS_FEATURES[:2], INNOVATION, strict=True):
        series = pd.to_numeric(values[source], errors='raise')
        if not np.isfinite(series.to_numpy()).all():
            raise ValueError('Nonfinite news feature')
        result[dest] = series - series.shift(1).rolling(24, min_periods=1).mean()
    return result


def _load(folder):
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('kind') != 'prediction' or not manifest.get('data_provenance', {}).get('source_verified'):
        raise ValueError('Requires verified historical prediction source')
    if manifest.get('news', {}).get('mode') != 'historical_archive_assumption':
        raise ValueError('Requires historical sampled news source')
    for name in ('snapshot.feather', 'feature-matrix.feather', 'news-feature-audit.json', 'news-snapshot.json'):
        if hashlib.sha256((folder/name).read_bytes()).hexdigest() != manifest['files'].get(name):
            raise ValueError('Input digest mismatch: ' + name)
    frame = pd.read_feather(folder/'snapshot.feather')
    matrix = pd.read_feather(folder/'feature-matrix.feather')
    frame['date'] = pd.to_datetime(frame['date'], utc=True)
    matrix['date'] = pd.to_datetime(matrix['date'], utc=True)
    if matrix['date'].duplicated().any() or not matrix['date'].is_monotonic_increasing:
        raise ValueError('Feature timestamps not unique and ordered')
    if not matrix['date'].diff().dropna().eq(pd.Timedelta(hours=1)).all():
        raise ValueError('Feature timestamps must be contiguous hourly')
    joined = matrix.merge(frame[['date', 'open', 'high', 'low', 'close', 'volume']], on='date', validate='one_to_one', how='left')
    if joined['close'].isna().any():
        raise ValueError('Feature and market snapshots differ')
    return manifest, add_innovation(joined)


def run(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 'news-innovation-study-v1', 'status': 'running', 'windows': [], 'failures': [],
              'policy': 'Add news count and source innovations from previous 24 hourly factors; same train rows across arms',
              'warning': 'Offline development research; supplemental 2025-05-31 window chosen after a missing archive; not a final blind test.',
              'seeds': list(SEEDS)}
    def save():
        (output/'study.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    save()
    for start, source, supplemental in SOURCES:
        window = {'start': start, 'supplemental_after_missing_archive': supplemental, 'status': 'running', 'rows': []}
        report['windows'].append(window); save()
        try:
            folder = Path(source)
            manifest, frame = _load(folder)
            config = manifest['effective_config']; period = manifest['period']
            cutoff = pd.Timestamp(period['train']['end']); end = pd.Timestamp(period['evaluation']['end'])
            date = frame['date']; horizon = int(config['parameters']['horizon'])
            labels = frame['close'].shift(-horizon) / frame['close'] - 1
            known = date + (horizon+1)*HOUR
            complete = frame[list(INNOVATION)].notna().all(axis=1)
            train_mask = (date < cutoff) & (known <= cutoff) & labels.notna() & complete
            eval_mask = (date >= cutoff) & (date < end) & complete
            if train_mask.sum() < 40 or eval_mask.sum() < 24:
                raise ValueError('Insufficient causal samples')
            evaluation = frame.loc[eval_mask, ['date','open','high','low','close','volume']].reset_index(drop=True)
            window.update(end=period['requested']['end'], period=period, train_rows=int(train_mask.sum()),
                          eval_rows=int(eval_mask.sum()), latest_train_label_at=known[train_mask].max().isoformat(),
                          market_sha256=manifest['files']['snapshot.feather'],
                          feature_sha256=manifest['files']['feature-matrix.feather'],
                          news_snapshot_sha256=manifest['files']['news-snapshot.json'])
            for seed in SEEDS:
                for arm, cols in ARMS.items():
                    row = {'seed': seed, 'arm': arm, 'status': 'running'}
                    window['rows'].append(row); save()
                    try:
                        model = LGBMRegressor(n_estimators=100, num_leaves=15, max_depth=5, min_child_samples=40,
                            learning_rate=.05, random_state=seed, n_jobs=2, deterministic=True,
                            force_col_wise=True, verbosity=-1)
                        model.fit(frame.loc[train_mask, cols], labels[train_mask])
                        predictions = model.predict(frame.loc[eval_mask, cols])
                        actions = _prediction_actions(predictions, config)
                        outcome = evaluate(evaluation, actions, config)
                        decisions = [{'decision_at': (at+HOUR).isoformat(), 'prediction': float(pred), 'action': int(action)}
                                     for at, pred, action in zip(evaluation['date'], predictions, actions, strict=True)]
                        prediction = prediction_metrics(frame[['date','close']], decisions, period, horizon)
                        run_dir = output / start / f'seed-{seed}-{arm}'
                        run_dir.mkdir(parents=True)
                        model.booster_.save_model(str(run_dir/'model.txt'))
                        (run_dir/'outcome.json').write_text(json.dumps({'decisions': decisions, 'evaluation': outcome}, allow_nan=False), encoding='utf-8')
                        row.update(status='succeeded', metrics=outcome['metrics'], risk=risk_metrics(outcome['equity']),
                                   prediction=prediction, features=cols,
                                   model_sha256=hashlib.sha256((run_dir/'model.txt').read_bytes()).hexdigest(),
                                   outcome_sha256=hashlib.sha256((run_dir/'outcome.json').read_bytes()).hexdigest())
                    except Exception as exc:
                        row.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:300]}')
                        report['failures'].append({'window': start, 'seed': seed, 'arm': arm, 'error': row['error']})
                    save()
            window['status'] = 'complete' if all(x['status']=='succeeded' for x in window['rows']) else 'partial'
        except Exception as exc:
            window.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:300]}')
            report['failures'].append({'window': start, 'error': window['error']})
        save()
    report['status'] = 'complete' if not report['failures'] else 'partial'; save()
    return report


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2: raise SystemExit('Expected a new output directory')
    run(sys.argv[1])
