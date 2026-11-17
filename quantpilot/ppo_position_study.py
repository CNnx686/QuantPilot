"""Predeclared development study: price PPO vs price plus own position bit."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from . import settings
from .rl_config import child_environment, python_path
from .terminal_api import StrategyInput
from .terminal_engine import _period, evaluate, train, verify_model
from .terminal_features import execute_bar, new_account
from .terminal_market import prepare_dataset

WINDOWS = [('2025-01-01', '2025-01-15'), ('2025-01-15', '2025-01-29'),
           ('2025-04-01', '2025-04-15'), ('2025-07-01', '2025-07-15')]
SEEDS = [7, 42, 99, 17, 52]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def conflicts(frame, actions, config):
    """Reconstruct the baseline's own position, using only executed past bars."""
    frame = frame.reset_index(drop=True)
    if len(frame) != len(actions):
        raise ValueError('Actions and candles must align')
    account = new_account(config['wallet'])
    rows = []
    for i, action in enumerate(actions):
        held = bool(account['units'])
        invalid = bool((int(action) == 1 and held) or (int(action) == 2 and not held))
        item = {'decision_at': (frame.iloc[i]['date'] + pd.Timedelta(hours=1)).isoformat(),
                'position_before': int(held), 'action': int(action), 'position_conflict': invalid,
                'executed_next_bar': i + 1 < len(frame)}
        if i + 1 < len(frame):
            effective, equity = execute_bar(account, int(action), frame.iloc[i+1], config,
                                            previous_close=frame.iloc[i]['close'])
            item.update(effective_action=int(effective), equity=float(equity), position_after=int(account['units'] > 0))
        rows.append(item)
    return rows


def summary(rows):
    executable = [r for r in rows if r['executed_next_bar']]
    return {'evaluated_decisions': len(executable),
            'position_conflict_rate': sum(r['position_conflict'] for r in executable) / len(executable) if executable else None}


def _save(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)


def run(folder):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    report = {'schema':'ppo-position-state-v2','status':'running','seeds':SEEDS,'windows':[],
              'warning':'Development only; Q4 reserved. Equal 2048 steps per model; 7-dimensional variant is research only, not deployable.',
              'protocol':'docs/product/personal-quant-v1/ppo-v2-protocol.md','failures':[]}
    def save(): _save(folder / 'study.json', report)
    save()
    for wi, (start, end) in enumerate(WINDOWS, 1):
        wd = folder / f'window-{wi}'; wd.mkdir()
        window = {'start':start,'end':end,'runs':[]}; report['windows'].append(window); save()
        config = StrategyInput(name='PPO voting research', kind='rl', symbol='BTC/USDT', start=start, end=end).model_dump(mode='json')
        try:
            candles = prepare_dataset(config, wd)
            period = _period(config)
            start_eval, end_eval = map(pd.Timestamp, (period['evaluation']['start'], period['evaluation']['end']))
            frame = candles.loc[(candles['date'] >= start_eval) & (candles['date'] < end_eval)].reset_index(drop=True)
        except Exception as exc:
            report['failures'].append({'window':wi,'error':str(exc)[:500]})
            for seed in SEEDS:
                for variant in ('price_only','price_position'):
                    window['runs'].append({'seed':seed,'variant':variant,'status':'failed','error':'Market preparation failed'})
            save(); continue
        for seed in SEEDS:
            config['parameters']['seed'] = seed
            previous = settings.ROOT / 'artifacts' / 'ppo-ensemble-20261117' / f'window-{wi}' / f'seed-{seed}'
            reuse = wi <= 2 and previous.exists()
            baseline = previous if reuse else wd / f'seed-{seed}-baseline'
            base_row = {'seed':seed,'variant':'price_only','status':'running','reused_previous':reuse}
            window['runs'].append(base_row); save()
            try:
                if not reuse:
                    candidate = train(config, baseline, candles)
                else:
                    candidate = json.loads((baseline / 'result.json').read_text(encoding='utf-8'))
                    manifest = verify_model(baseline)
                    if manifest['config'] != config or not pd.read_feather(baseline / 'snapshot.feather').equals(
                        candles[(candles['date'] >= pd.Timestamp(start, tz='UTC')-pd.Timedelta(hours=64)) & (candles['date'] < end_eval)].reset_index(drop=True)):
                        raise ValueError('Previous baseline configuration or snapshot differs')
                manifest = candidate['model_manifest']
                if not manifest['data_provenance']['source_verified']:
                    raise ValueError('Market data provenance not verified')
                decisions = json.loads((baseline/'evaluation-decisions.json').read_text(encoding='utf-8'))
                actions = [r['action'] for r in decisions]
                effective = manifest['effective_config']
                eval_result = evaluate(frame, actions, effective)
                if eval_result['metrics'] != candidate['metrics']:
                    raise ValueError('Baseline replay differs from frozen result')
                decision_rows = conflicts(frame, actions, effective)
                diagnostic = wd / f'seed-{seed}-baseline-decisions.json'
                _save(diagnostic, decision_rows)
                base_row.update(status='succeeded',metrics=candidate['metrics'],period=period,**summary(decision_rows),
                    evidence_sha256={'price_snapshot':manifest['files']['snapshot.feather'],
                        'model':manifest['files']['model.zip'],'decisions':manifest['files']['evaluation-decisions.json'],
                        'position_replay':sha(diagnostic)})
            except Exception as exc:
                base_row.update(status='failed',error=str(exc)[:500]); report['failures'].append({'window':wi,'seed':seed,'variant':'price_only','error':str(exc)[:500]})
                window['runs'].append({'seed':seed,'variant':'price_position','status':'failed','error':'Baseline unavailable; paired run skipped'})
                save(); continue
            save()
            row = {'seed':seed,'variant':'price_position','status':'running'}
            window['runs'].append(row); save()
            model_dir = wd / f'seed-{seed}-position'; model_dir.mkdir()
            try:
                for file in ('effective-config.json','normalizer.json','rl-train.feather','rl-train-values.npy','evaluation-values.npy'):
                    shutil.copyfile(baseline / file, model_dir / file)
                frame.to_feather(model_dir/'evaluation-frame.feather')
                command = [str(python_path()), '-m', 'quantpilot.ppo_position_runtime', str(model_dir)]
                with (model_dir/'runtime.log').open('w',encoding='utf-8') as log:
                    result = subprocess.run(command,cwd=settings.ROOT,env=child_environment(),stdin=subprocess.DEVNULL,
                        stdout=log,stderr=subprocess.STDOUT,timeout=600,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                if result.returncode:
                    raise RuntimeError(f'Position PPO training failed ({result.returncode}); inspect runtime.log')
                rows = json.loads((model_dir/'position-decisions.json').read_text(encoding='utf-8'))
                result = evaluate(frame,[r['action'] for r in rows],effective)
                _save(model_dir/'evaluation.json',result)
                row.update(status='succeeded',metrics=result['metrics'],period=period,**summary(rows),
                    evidence_sha256={'price_snapshot':manifest['files']['snapshot.feather'],
                        'model':sha(model_dir/'model.zip'),'decisions':sha(model_dir/'position-decisions.json'),
                        'evaluation':sha(model_dir/'evaluation.json')})
            except Exception as exc:
                row.update(status='failed',error=str(exc)[:500]); report['failures'].append({'window':wi,'seed':seed,'variant':'price_position','error':str(exc)[:500]})
            save()
    report['status'] = 'partial' if report['failures'] else 'complete'; save()
    return report


if __name__ == '__main__':
    if len(sys.argv)!=2: raise SystemExit('Expected a new output directory')
    run(sys.argv[1])
