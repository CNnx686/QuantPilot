"""Export the frozen second-round research summary without private paths or invented results."""
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
METRICS = ('net_return', 'max_drawdown', 'trades', 'turnover', 'win_rate')
RISK = ('sharpe', 'sortino', 'observations', 'annualization')


def select(obj, names):
    return {key: obj[key] for key in names if isinstance(obj, dict) and key in obj}


def safe_error(value):
    text = str(value or '')[:500]
    text = re.sub(r'[A-Za-z]:[\\/][^\s\'\"<>]+', '[本机路径]', text)
    return re.sub(r'\bsk-[A-Za-z0-9]{12,}\b', '[已隐藏]', text)


def read_source(path, schema):
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict) or data.get('schema') != schema:
            raise ValueError('不支持的报告格式')
        return data, hashlib.sha256(raw).hexdigest(), None
    except (OSError, ValueError, UnicodeError) as exc:
        return None, None, '报告缺失或无法读取：' + type(exc).__name__


def news_rows(window):
    rows = []
    for row in window.get('rows', []):
        if not isinstance(row, dict):
            continue
        item = select(row, ('seed', 'feature', 'policy', 'status', 'market_sha256', 'predictions_sha256', 'source_verified', 'candidate_id'))
        if row.get('error'):
            item['error'] = safe_error(row['error'])
        item['metrics'] = select(row.get('metrics'), METRICS)
        item['risk'] = select(row.get('risk'), RISK)
        rows.append(item)
    return rows


def ppo_rows(window):
    rows = []
    for row in window.get('runs', []):
        if not isinstance(row, dict):
            continue
        item = select(row, ('seed', 'variant', 'status', 'position_conflict_rate', 'evaluated_decisions', 'reused_previous'))
        if row.get('error'):
            item['error'] = safe_error(row['error'])
        item['metrics'] = select(row.get('metrics'), METRICS)
        rows.append(item)
    return rows


def prepare_source(path, schema, kind):
    data, digest, error = read_source(path, schema)
    if error:
        return {'status': 'failed', 'error': error, 'source_sha256': None, 'windows': []}
    windows = []
    for window in data.get('windows', []):
        if not isinstance(window, dict):
            continue
        item = select(window, ('start', 'end', 'status'))
        if window.get('error'):
            item['error'] = safe_error(window['error'])
        item['rows'] = news_rows(window) if kind == 'news' else ppo_rows(window)
        windows.append(item)
    failures = []
    for failure in data.get('failures', []):
        if isinstance(failure, dict):
            item = select(failure, ('window', 'seed', 'feature', 'variant'))
            item['error'] = safe_error(failure.get('error'))
            failures.append(item)
    status = data.get('status') if data.get('status') in ('running', 'complete', 'partial', 'failed') else 'partial'
    # A purported complete report must contain every predeclared run and only successful rows.
    expected_seeds = (7, 42, 99) if kind == 'news' else (7, 42, 99, 17, 52)
    expected_windows = ({'2025-01-01', '2025-01-15', '2025-04-01', '2025-06-01'} if kind == 'news'
                        else {'2025-01-01', '2025-01-15', '2025-04-01', '2025-07-01'})
    expected_keys = ({(seed, feature, policy) for seed in expected_seeds
                      for feature in ('price_only', 'price_news') for policy in ('baseline', 'cost_buffer')}
                     if kind == 'news' else {(seed, variant) for seed in expected_seeds
                                              for variant in ('price_only', 'price_position')})
    structurally_complete = len(windows) == 4 and {w.get('start') for w in windows} == expected_windows
    for window in windows:
        records = window['rows']
        actual_keys = ([(r.get('seed'), r.get('feature'), r.get('policy')) for r in records] if kind == 'news'
                       else [(r.get('seed'), r.get('variant')) for r in records])
        if (len(actual_keys) != len(expected_keys) or set(actual_keys) != expected_keys or
                any(r.get('status') != 'succeeded' or not r.get('metrics') for r in records)):
            structurally_complete = False
    if status == 'complete' and (not structurally_complete or failures):
        status = 'partial'
        failures.append({'error': '报告标记完成但窗口、种子或实验行不完整'})
    return {'status': status, 'source_sha256': digest, 'windows': windows, 'failures': failures}


def prepare_supplement(path):
    """Retain the unplanned replacement separately; it never completes the preregistered report."""
    data, digest, error = read_source(path, 'news-threshold-study-v1')
    if error:
        return {'status': 'failed', 'error': error, 'source_sha256': None, 'windows': []}
    windows = []
    for window in data.get('windows', []):
        if not isinstance(window, dict):
            continue
        item = select(window, ('start', 'end', 'status'))
        if window.get('error'):
            item['error'] = safe_error(window['error'])
        item['rows'] = news_rows(window)
        windows.append(item)
    failures = []
    for failure in data.get('failures', []):
        if isinstance(failure, dict):
            failures.append({**select(failure, ('window', 'seed', 'feature')), 'error': safe_error(failure.get('error'))})
    status = data.get('status') if data.get('status') in ('running', 'complete', 'partial', 'failed') else 'partial'
    expected = {(seed, feature, policy) for seed in (7, 42, 99)
                for feature in ('price_only', 'price_news') for policy in ('baseline', 'cost_buffer')}
    actual = ([(row.get('seed'), row.get('feature'), row.get('policy')) for row in windows[0]['rows']]
              if len(windows) == 1 else [])
    if status == 'complete' and (len(windows) != 1 or windows[0].get('start') != '2025-05-31'
            or windows[0].get('end') != '2025-06-14' or len(actual) != len(expected)
            or set(actual) != expected or failures or any(row.get('status') != 'succeeded'
            or not row.get('metrics') for row in windows[0]['rows'])):
        status = 'partial'
        failures.append({'error': '补充报告标记完成但窗口、种子或实验行不完整'})
    return {'status': status, 'source_sha256': digest, 'windows': windows, 'failures': failures,
            'label': '缺失后补充、非预注册'}


def read_diagnosis(path):
    """Copy only an independently recorded availability check, never alter original study."""
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode('utf-8'))
        if (not isinstance(data, dict) or data.get('batch') != '20250614180000'
                or data.get('method') != 'HEAD' or data.get('http_status') != 404
                or data.get('url') != 'https://data.gdeltproject.org/gdeltv2/20250614180000.gkg.csv.zip'
                or not isinstance(data.get('checked_at'), str)):
            raise ValueError('不支持的独立诊断证据')
        return {**select(data, ('checked_at', 'method', 'http_status', 'batch')),
                'source_sha256': hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, UnicodeError):
        return None


def prepare_innovation(path):
    data, digest, error = read_source(path, 'news-innovation-study-v1')
    if error:
        return {'status': 'failed', 'error': error, 'source_sha256': None, 'windows': []}
    windows = []
    for window in data.get('windows', []):
        if not isinstance(window, dict):
            continue
        item = select(window, ('start', 'end', 'status', 'supplemental_after_missing_archive'))
        if window.get('error'):
            item['error'] = safe_error(window['error'])
        item['rows'] = []
        for row in window.get('rows', []):
            if not isinstance(row, dict):
                continue
            entry = select(row, ('seed', 'arm', 'status', 'model_sha256', 'outcome_sha256'))
            entry['metrics'] = select(row.get('metrics'), METRICS)
            if row.get('error'):
                entry['error'] = safe_error(row['error'])
            item['rows'].append(entry)
        windows.append(item)
    failures = [{**select(f, ('window', 'seed', 'arm')), 'error': safe_error(f.get('error'))}
                for f in data.get('failures', []) if isinstance(f, dict)]
    expected = {(seed, arm) for seed in (7, 42, 99) for arm in ('price', 'raw_news', 'news_innovation')}
    valid = len(windows) == 4 and {w.get('start') for w in windows} == {'2025-01-01', '2025-01-15', '2025-04-01', '2025-05-31'}
    for window in windows:
        rows = window['rows']
        keys = [(r.get('seed'), r.get('arm')) for r in rows]
        valid = valid and len(keys) == len(expected) and set(keys) == expected and all(r.get('status') == 'succeeded' and r.get('metrics') for r in rows)
    status = data.get('status') if data.get('status') in ('running', 'complete', 'partial', 'failed') else 'partial'
    if status == 'complete' and (not valid or failures):
        status = 'partial'
        failures.append({'error': '因子变化报告标记完成但窗口或实验行不完整'})
    return {'status': status, 'source_sha256': digest, 'windows': windows, 'failures': failures}


def build(news_path, ppo_path, supplement_path=None, diagnosis_path=None, innovation_path=None):
    news = prepare_source(news_path, 'news-threshold-study-v1', 'news')
    ppo = prepare_source(ppo_path, 'ppo-position-state-v2', 'ppo')
    supplement = prepare_supplement(supplement_path) if supplement_path is not None else None
    # Supplementary runs cannot repair or replace a failed preregistered window.
    result = {'schema': 'strategy-v2-v1', 'generated_at': datetime.now(timezone.utc).isoformat(),
              'status': 'complete' if news['status'] == ppo['status'] == 'complete' else 'partial',
              'news': news, 'ppo': ppo}
    if supplement is not None:
        result['news_supplement'] = supplement
    if diagnosis_path is not None:
        result['news_diagnosis'] = read_diagnosis(diagnosis_path)
    if innovation_path is not None:
        result['news_innovation'] = prepare_innovation(innovation_path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--news-file', type=Path, default=ROOT/'artifacts/news-threshold-20261117/study.json')
    parser.add_argument('--ppo-file', type=Path, default=ROOT/'artifacts/ppo-position-v2-20261117/study.json')
    parser.add_argument('--supplement-file', type=Path, default=ROOT/'artifacts/news-threshold-supplement-20261117/study.json')
    parser.add_argument('--diagnosis-file', type=Path, default=ROOT/'artifacts/news-threshold-20261117/missing-batch-diagnosis.json')
    parser.add_argument('--innovation-file', type=Path, default=ROOT/'artifacts/news-innovation-20261117/study.json')
    parser.add_argument('--output', type=Path, default=ROOT/'apps/terminal/research/strategy-v2.json')
    args = parser.parse_args()
    data = build(args.news_file, args.ppo_file, args.supplement_file, args.diagnosis_file, args.innovation_file)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=args.output.parent, delete=False) as file:
        temporary = Path(file.name)
        json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write('\n')
    try:
        os.replace(temporary, args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({'status': data['status'], 'news_windows': len(data['news']['windows']), 'ppo_windows': len(data['ppo']['windows'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
