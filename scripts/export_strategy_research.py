"""Export bounded, path-free research summaries for the local terminal.

Source reports stay in the ignored artifacts directory; missing or incomplete
sources are represented explicitly and never replaced with synthetic values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEWS_NAMES = ('w1', 'w2', 'q2')
ARM_NAMES = ('price_only', 'full_news', 'fixed_25_news', 'validation_selected')
METRICS = ('net_return', 'max_drawdown', 'trades', 'turnover', 'win_rate')
RISK = ('sharpe', 'sortino', 'observations', 'annualization', 'risk_free_rate', 'minimum_observations')
PREDICTION = ('mae', 'rmse', 'direction_accuracy', 'observations', 'excluded_unavailable', 'horizon_hours')


def selected(data, names):
    return {key: data[key] for key in names if isinstance(data, dict) and key in data}


def safe_error(value):
    text = str(value or '')[:500]
    text = re.sub(r'[A-Za-z]:[\\/][^\s\'\"<>]+', '[本机路径]', text)
    return re.sub(r'\bsk-[A-Za-z0-9]{12,}\b', '[已隐藏]', text)


def read_report(path):
    if not path.is_file():
        return None, None, '报告文件缺失'
    try:
        raw = path.read_bytes()
        obj = json.loads(raw.decode('utf-8'))
        if not isinstance(obj, dict):
            raise ValueError('报告必须是 JSON 对象')
        return obj, hashlib.sha256(raw).hexdigest(), None
    except (OSError, ValueError, UnicodeError) as exc:
        return None, None, '报告无法读取：' + type(exc).__name__


def news_window(obj, label, digest):
    arms = obj.get('arms') if isinstance(obj.get('arms'), dict) else {}
    cleaned = {}
    for name in ARM_NAMES:
        arm = arms.get(name)
        if not isinstance(arm, dict):
            cleaned[name] = {'status': 'failed', 'error': '该对照臂缺失'}
            continue
        cleaned[name] = {
            **selected(arm, ('status', 'weight')),
            'metrics': selected(arm.get('metrics'), METRICS),
            'risk': selected(arm.get('risk'), RISK),
            'prediction': selected(arm.get('prediction'), PREDICTION),
        }
    cal = obj.get('calibration') or {}
    scores = cal.get('weight_scores') if isinstance(cal, dict) else None
    return {
        'name': label, 'source_sha256': digest,
        'period': selected(obj.get('period'), ('requested', 'train', 'evaluation')),
        'config': selected(obj.get('config'), ('symbol', 'timeframe', 'wallet', 'stake', 'fee')),
        'calibration': {**selected(cal, ('selected_weight', 'validation_rows', 'training_cutoff', 'validation_start')),
                        'weight_scores': [selected(score, ('weight', 'rmse')) for score in scores]
                        if isinstance(scores, list) else []},
        'arms': cleaned,
    }


def ppo_window(window):
    if not isinstance(window, dict):
        return None
    singles, groups = [], []
    for item in window.get('singles', []):
        if isinstance(item, dict):
            singles.append({**selected(item, ('seed', 'id', 'status', 'period', 'data_sha256')),
                            **({'error': safe_error(item['error'])} if item.get('error') else {}),
                            'metrics': selected(item.get('metrics'), METRICS)})
    for item in window.get('groups', []):
        if isinstance(item, dict):
            groups.append({**selected(item, ('seeds', 'status', 'price_snapshot_sha256', 'disagreements', 'three_way_ties')),
                           **({'error': safe_error(item['error'])} if item.get('error') else {}),
                           'metrics': selected(item.get('metrics'), METRICS)})
    return {**selected(window, ('start', 'end', 'summary')), 'singles': singles, 'groups': groups}


def build(news_dir, ppo_file):
    windows, news_errors, source_hashes = [], [], {}
    for name in NEWS_NAMES:
        obj, digest, error = read_report(news_dir / (name + '.json'))
        if error:
            news_errors.append({'window': name, 'error': error})
            continue
        windows.append(news_window(obj, name, digest))
        source_hashes[name] = digest
    news_complete = len(windows) == len(NEWS_NAMES) and all(
        all(a.get('status') not in {'failed', 'running', 'pending'} and bool(a.get('metrics'))
            for a in w['arms'].values()) for w in windows)
    ppo, ppo_hash, ppo_error = read_report(ppo_file)
    ppo = ppo or {}
    ppo_windows = [result for w in ppo.get('windows', []) if (result := ppo_window(w)) is not None]
    failures = [{**selected(item, ('window', 'group')), 'error': safe_error(item.get('error'))}
                for item in ppo.get('failures', []) if isinstance(item, dict)]
    if ppo_error:
        failures.append({'error': ppo_error})
    ppo_status = ppo.get('status', 'failed' if ppo_error else 'partial')
    if ppo_status == 'complete' and (len(ppo_windows) != 3 or any(
            len(w['singles']) != 6 or len(w['groups']) != 2 or
            any(g.get('status') != 'succeeded' or not g.get('metrics') for g in w['groups'])
            for w in ppo_windows)):
        ppo_status = 'partial'
        failures.append({'error': 'PPO 报告的窗口、单模型或投票组不完整'})
    complete = news_complete and ppo_status == 'complete' and not failures
    return {
        'schema': 'strategy-improvements-v1',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'status': 'complete' if complete else 'partial',
        'limitations': [
            '历史开发研究；2025 Q4 继续保留，结果不是最终盲测或盈利保证。',
            '回测计入手续费，未完整模拟滑点、盘口深度与市场冲击；研究结果不自动发布候选。',
        ],
        'news': {'status': 'complete' if news_complete else 'partial',
                 'warning': '新闻为每六小时一个 15 分钟批次的标题采样；融合权重仅在训练区间内选择。',
                 'source_hashes': source_hashes, 'failures': news_errors, 'windows': windows},
        'ppo': {'status': ppo_status, 'warning': '三模型投票需要单模型三倍的训练预算；没有自动选优或上线。',
                'source_sha256': ppo_hash, 'policy': ppo.get('policy'), 'failures': failures,
                'groups': ppo.get('groups', []), 'windows': ppo_windows},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--news-dir', type=Path, default=ROOT / 'artifacts/news-blend-20261117')
    parser.add_argument('--ppo-file', type=Path, default=ROOT / 'artifacts/ppo-ensemble-20261117/study.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'apps/terminal/research/strategy-improvements.json')
    args = parser.parse_args()
    data = build(args.news_dir, args.ppo_file)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=args.output.parent, delete=False) as file:
        try:
            file.write(payload)
            temporary = Path(file.name)
        finally:
            file.flush()
    try:
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({'output': str(args.output), 'status': data['status'],
                      'news_windows': len(data['news']['windows']), 'ppo_windows': len(data['ppo']['windows'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
