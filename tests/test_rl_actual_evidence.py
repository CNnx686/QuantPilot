"""Opt-in audit of immutable completed evidence: python tests/test_rl_actual_evidence.py JOB_ID.

The normal unit suite does not require a completed job. This command checks real raw
artifacts and writes a separate audit receipt, never alters the original run.
"""
import json
import sys
from pathlib import Path
import pandas as pd


def audit(directory):
    import hashlib
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    inventory = json.loads((directory / 'inventory.json').read_text(encoding='utf-8'))
    for relative, item in inventory.items():
        assert digest(directory / relative) == item['sha256'], relative
    result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    market = pd.read_feather(directory / 'market' / 'BTC_USDT-1h.feather').set_index('date')
    assert market.index.max() < pd.Timestamp('2025-10-01', tz='UTC')
    records = []
    for row in result['rows']:
        if row['seed'] is None:
            continue
        actions = pd.read_csv(directory / f"seed-{row['seed']}" / f"{row['window']}-actions.csv")
        assert len(actions) > 0 and not actions['candle_at'].duplicated().any()
        times = {key: pd.to_datetime(actions[key], utc=True) for key in
                 ('candle_at', 'decision_at', 'feature_available_at', 'model_fit_at', 'model_label_max_at')}
        assert (times['decision_at'] == times['candle_at'] + pd.Timedelta(hours=1)).all()
        assert (times['feature_available_at'] <= times['decision_at']).all()
        assert (times['model_label_max_at'] <= times['model_fit_at']).all()
        assert (times['model_fit_at'] <= times['decision_at']).all()
        buy_times = set(times['candle_at'][actions['requested_action'] == 1])
        sell_times = set(times['candle_at'][actions['requested_action'] == 2])
        replay = json.loads((directory / row['result_artifact']).read_text(encoding='utf-8'))
        for trade in replay['trades']:
            opened = pd.Timestamp(trade['open_date'])
            assert opened - pd.Timedelta(hours=1) in buy_times
            assert abs(trade['open_rate'] - market.loc[opened, 'open']) < 1e-8
            if trade['exit_reason'] == 'exit_signal':
                closed = pd.Timestamp(trade['close_date'])
                assert closed - pd.Timedelta(hours=1) in sell_times
                assert abs(trade['close_rate'] - market.loc[closed, 'open']) < 1e-8
        assert abs(10000 + sum(t['profit_abs'] for t in replay['trades']) - replay['metrics']['final_balance']) < 1e-5
        records.append({'window': row['window'], 'seed': row['seed'], 'action_rows': len(actions),
                        'trades': len(replay['trades']), 'causal_timing_and_next_open_fills': 'passed'})
    assert len(records) == 10
    assert len(result['all_seeds']) == 5
    assert all(seed['training']['steps'] == 8192 for seed in result['all_seeds'])
    return {'job_id': directory.name, 'inventory_files_verified': len(inventory),
            'all_seeds_reported': True, 'final_q4_absent': True, 'replays': records,
            'limitation': 'Checks recorded metadata, deterministic feature tests and executed fills; not full lookahead-analysis'}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    identifier = sys.argv[1]
    import re
    if not re.fullmatch(r'rl-[a-f0-9]{32}', identifier):
        raise ValueError('Invalid job ID')
    receipt = audit(root / 'artifacts' / 'rl' / identifier)
    target = root / 'artifacts' / 'rl-audits'
    target.mkdir(exist_ok=True)
    (target / f'{identifier}.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt, indent=2))
