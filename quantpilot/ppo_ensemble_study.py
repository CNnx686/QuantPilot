"""Bounded research-only PPO voting study; does not publish deployable candidates."""
from __future__ import annotations

import json
import statistics
import sys
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from .terminal_engine import evaluate, train
from .terminal_market import prepare_dataset
from .terminal_api import StrategyInput


WINDOWS = [('2025-01-01', '2025-01-15'), ('2025-01-15', '2025-01-29'),
           ('2025-02-01', '2025-02-15')]
GROUPS = [(7, 42, 99), (17, 52, 109)]


def vote(actions):
    """Majority vote, with hold for a three-way split; no future inputs."""
    matrix = np.asarray(actions)
    if matrix.ndim != 2 or matrix.shape[0] != 3 or not np.isin(matrix, [0, 1, 2]).all():
        raise ValueError('Exactly three valid aligned action arrays required')
    return np.array([next((int(action) for action in (0, 1, 2) if (column == action).sum() >= 2), 0)
                     for column in matrix.T], dtype=np.int64)


def run(folder):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    report = {'study': 'ppo-three-seed-majority-v1', 'status': 'running', 'windows': [],
              'policy': '3 independent PPO models; majority action; all-different => hold',
              'warning': 'Development evaluation only. Ensemble uses 3x training budget of one member. No automatic selection or deployment.',
              'groups': [list(g) for g in GROUPS], 'failures': []}

    def save():
        target = folder / 'study.json'
        temporary = folder / ('study.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        try:
            for attempt in range(10):
                try:
                    temporary.replace(target)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(.05)
        finally:
            temporary.unlink(missing_ok=True)

    save()
    for wi, (start, end) in enumerate(WINDOWS, 1):
        window = {'start': start, 'end': end, 'groups': [], 'singles': []}
        report['windows'].append(window)
        wd = folder / f'window-{wi}'
        wd.mkdir()
        config = StrategyInput(name='PPO voting research', kind='rl', symbol='BTC/USDT',
                               start=start, end=end, news=False).model_dump(mode='json')
        try:
            candles = prepare_dataset(config, wd)
        except Exception as exc:
            report['failures'].append({'window': wi, 'error': str(exc)[:500]})
            save()
            continue
        for gi, seeds in enumerate(GROUPS, 1):
            group = {'seeds': list(seeds), 'status': 'running', 'metrics': None}
            window['groups'].append(group)
            save()
            actions = []
            try:
                for seed in seeds:
                    current = json.loads(json.dumps(config))
                    current['parameters']['seed'] = seed
                    candidate = train(current, wd / f'seed-{seed}', candles)
                    decisions = json.loads((wd / f'seed-{seed}' / 'evaluation-decisions.json').read_text(encoding='utf-8'))
                    actions.append([r['action'] for r in decisions])
                    window['singles'].append({'seed': seed, 'id': candidate['id'], 'metrics': candidate['metrics'],
                        'period': candidate['period'], 'data_sha256': candidate['model_manifest']['files']['snapshot.feather']})
                    save()
                hashes = {r['data_sha256'] for r in window['singles'] if r['seed'] in seeds}
                if len(hashes) != 1:
                    raise ValueError('Models in a voting group have mismatched price snapshots')
                boundary = pd.Timestamp(candidate['period']['evaluation']['start'])
                end_time = pd.Timestamp(end, tz='UTC')
                frame = candles.loc[(candles['date'] >= boundary) & (candles['date'] < end_time)].reset_index(drop=True)
                ensemble_actions = vote(actions)
                outcome = evaluate(frame, ensemble_actions, candidate['model_manifest']['effective_config'])
                (wd / f'group-{gi}-actions.json').write_text(json.dumps(ensemble_actions.tolist()), encoding='utf-8')
                (wd / f'group-{gi}-evaluation.json').write_text(json.dumps(outcome, allow_nan=False), encoding='utf-8')
                group.update(status='succeeded', metrics=outcome['metrics'], price_snapshot_sha256=next(iter(hashes)),
                             disagreements=int(np.sum(~np.all(np.asarray(actions) == np.asarray(actions)[0], axis=0))),
                             three_way_ties=int(np.sum(np.apply_along_axis(lambda x: len(set(x)) == 3, 0, np.asarray(actions)))))
            except Exception as exc:
                group.update(status='failed', error=str(exc)[:500])
                report['failures'].append({'window': wi, 'group': gi, 'error': str(exc)[:500]})
            save()
        values = [x['metrics']['net_return'] for x in window['singles']]
        ensemble = [x['metrics']['net_return'] for x in window['groups'] if x['status'] == 'succeeded']
        window['summary'] = {'single_mean': statistics.mean(values) if values else None,
            'single_min': min(values) if values else None, 'single_max': max(values) if values else None,
            'ensemble_mean': statistics.mean(ensemble) if ensemble else None,
            'ensemble_min': min(ensemble) if ensemble else None, 'ensemble_max': max(ensemble) if ensemble else None}
        save()
    report['status'] = 'partial' if report['failures'] else 'complete'
    save()
    return report


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Expected new output directory')
    run(sys.argv[1])
