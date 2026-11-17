"""Predeclared cost-buffer threshold ablation, using frozen local prediction models.

This experiment changes signal thresholds, not features, model weights or execution
costs. It does not publish candidates or trade. Dates after 2025-10-01 stay reserved.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd

from .terminal_api import StrategyInput
from .terminal_engine import _prediction_actions, evaluate, train
from .terminal_market import prepare_dataset
from .terminal_news import prepare_history
from .terminal_news_engine import train as train_news
from .terminal_metrics import risk_metrics

WINDOWS = (('2025-01-01', '2025-01-15'), ('2025-01-15', '2025-01-29'),
           ('2025-04-01', '2025-04-15'), ('2025-06-01', '2025-06-15'))
SEEDS = (7, 42, 99)
THRESHOLDS = {'baseline': (0.003, 0.0), 'cost_buffer': (0.005, -0.002)}


def actions_for(predictions, config, variant):
    """Apply one predetermined entry/exit pair without looking ahead."""
    if variant not in THRESHOLDS:
        raise ValueError('Unknown preset threshold')
    predictions = np.asarray(predictions, dtype=float)
    if predictions.ndim != 1 or not np.isfinite(predictions).all():
        raise ValueError('Predictions must be a finite vector')
    effective = deepcopy(config)
    effective['parameters']['entry_threshold'], effective['parameters']['exit_threshold'] = THRESHOLDS[variant]
    return _prediction_actions(predictions, effective)


def _write(path, obj):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def run(folder, windows=WINDOWS):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    report = {'schema': 'news-threshold-study-v1', 'status': 'running',
              'interpretation': 'Same fitted prediction, price data, fees and execution; change is thresholds, not news features. Development only, no deployment.',
              'windows': [], 'seeds': list(SEEDS), 'thresholds': THRESHOLDS, 'failures': []}
    _write(folder/'study.json', report)
    for number, (start, end) in enumerate(windows, 1):
        wd = folder / f'window-{number}'
        wd.mkdir()
        config = StrategyInput(name='News decision threshold study', kind='prediction', symbol='ETH/USDT',
                               start=start, end=end, news=True).model_dump(mode='json')
        window = {'start': start, 'end': end, 'rows': [], 'status': 'running'}
        report['windows'].append(window)
        _write(folder/'study.json', report)
        try:
            candles = prepare_dataset(config, wd)
            news = prepare_history(config, wd)
            window['news_snapshot_id'] = news['id']
        except Exception as exc:
            window.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:400]}')
            report['failures'].append({'window': number, 'error': window['error']})
            _write(folder/'study.json', report)
            continue
        for seed in SEEDS:
            seed_hashes = []
            for feature in ('price_only', 'price_news'):
                current = deepcopy(config)
                current['news'] = feature == 'price_news'
                current['parameters']['seed'] = seed
                model_dir = wd / f'seed-{seed}-{feature}'
                try:
                    candidate = (train_news(current, model_dir, candles, news) if current['news']
                                 else train(current, model_dir, candles))
                    manifest = candidate['model_manifest']
                    if not manifest['data_provenance']['source_verified']:
                        raise ValueError('Verified historical source required')
                    seed_hashes.append(manifest['files']['snapshot.feather'])
                    raw = (model_dir/'evaluation-decisions.json').read_bytes()
                    if hashlib.sha256(raw).hexdigest() != manifest['files']['evaluation-decisions.json']:
                        raise ValueError('Frozen prediction digest mismatch')
                    decisions = json.loads(raw)
                    predictions = [decision['prediction'] for decision in decisions]
                    period = candidate['period']
                    frame = candles.loc[(candles['date'] >= pd.Timestamp(period['evaluation']['start'])) &
                                        (candles['date'] < pd.Timestamp(period['evaluation']['end']))].reset_index(drop=True)
                    for variant in THRESHOLDS:
                        acts = actions_for(predictions, manifest['effective_config'], variant)
                        evaluation = evaluate(frame, acts, manifest['effective_config'])
                        if variant == 'baseline' and evaluation['metrics'] != candidate['metrics']:
                            raise ValueError('Baseline does not reproduce frozen candidate')
                        armfile = model_dir / ('threshold-' + variant + '.json')
                        _write(armfile, {'actions': acts.tolist(), 'evaluation': evaluation})
                        window['rows'].append({'seed': seed, 'feature': feature, 'policy': variant,
                            'status': 'succeeded', 'metrics': evaluation['metrics'],
                            'risk': risk_metrics(evaluation['equity']), 'candidate_id': candidate['id'],
                            'market_sha256': manifest['files']['snapshot.feather'],
                            'predictions_sha256': manifest['files']['evaluation-decisions.json'],
                            'source_verified': True, 'period': period})
                except Exception as exc:
                    error = f'{type(exc).__name__}: {str(exc)[:400]}'
                    report['failures'].append({'window': number, 'seed': seed, 'feature': feature, 'error': error})
                    window['rows'].append({'seed': seed, 'feature': feature, 'status': 'failed', 'error': error})
                _write(folder/'study.json', report)
            if len(seed_hashes) == 2 and len(set(seed_hashes)) != 1:
                report['failures'].append({'window': number, 'seed': seed, 'error': 'Unmatched price snapshots'})
        window['status'] = 'complete' if len(window['rows']) == len(SEEDS)*4 and all(x['status']=='succeeded' for x in window['rows']) and not any(x['window']==number for x in report['failures']) else 'partial'
        _write(folder/'study.json', report)
    report['status'] = 'complete' if not report['failures'] and all(x['status']=='complete' for x in report['windows']) else 'partial'
    _write(folder/'study.json', report)
    return report


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        raise SystemExit('Expected a new output directory')
    run(sys.argv[1])
