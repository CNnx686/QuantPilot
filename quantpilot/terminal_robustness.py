"""Bounded multi-window, multi-seed research; results never become deployable candidates."""
import json
import statistics
import uuid
from copy import deepcopy
from pathlib import Path

from . import terminal_store as store


def _write(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    # The UI may briefly read the old file on Windows. Never truncate it in place.
    import time
    try:
        for attempt in range(10):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(.05)
    finally:
        tmp.unlink(missing_ok=True)


def _summarize(report):
    rows = report['rows']
    by_variant = {}
    for variant in report['arms']:
        results = [r['metrics']['net_return'] for r in rows if r['variant'] == variant and r['status'] == 'succeeded']
        by_variant[variant] = {'count':len(results), 'net_return_mean':statistics.mean(results) if results else None,
                               'net_return_min':min(results) if results else None,
                               'net_return_max':max(results) if results else None}
    report['summary'] = {'total':len(rows), 'completed':sum(r['status']=='succeeded' for r in rows),
                         'failed':sum(r['status']=='failed' for r in rows), 'by_variant':by_variant}


def run(job):
    from .terminal_engine import train
    from .terminal_market import prepare_dataset
    from .terminal_metrics import evaluate_candidate
    identifier = job['id']
    directory = store.job_directory(identifier)
    directory.mkdir(parents=True, exist_ok=True)
    spec = job['robustness']
    arms = ['price_only', 'price_news'] if job['config'].get('news') else ['price_only']
    rows = [{'window':window, 'seed':seed, 'variant':variant, 'status':'pending', 'metrics':None,
             'evaluation_metrics':None, 'error':None} for window in spec['windows'] for seed in spec['seeds'] for variant in arms]
    report = {'status':'running', 'comparable':False, 'windows':spec['windows'], 'seeds':spec['seeds'],
              'arms':arms, 'rows':rows, 'summary':{},
              'interpretation':'Development evaluation; overlapping assumptions and small samples are not evidence of profit or causal effect. No candidates are published.'}

    def checkpoint(phase):
        _summarize(report)
        _write(directory / 'comparison.json', report)
        store.progress(identifier, phase, comparison=report)
        if (directory / 'CANCEL').exists():
            raise RuntimeError('Task cancelled')

    checkpoint('robustness_preparing')
    for wi, window in enumerate(spec['windows']):
        window_dir = directory / ('window-' + str(wi+1))
        window_dir.mkdir(parents=True, exist_ok=True)
        config = deepcopy(job['config'])
        config.update(window)
        window_rows = [r for r in rows if r['window']==window]
        try:
            checkpoint('robustness_market_' + str(wi+1))
            candles = prepare_dataset(config, window_dir)
        except Exception as exc:
            if (directory / 'CANCEL').exists(): raise
            for row in window_rows: row.update(status='failed', error='Market preparation: '+str(exc)[:500])
            checkpoint('robustness_market_failed')
            continue
        news_snapshot = None
        news_error = None
        if 'price_news' in arms:
            try:
                from .terminal_news import prepare_history
                checkpoint('robustness_news_' + str(wi+1))
                news_snapshot = prepare_history(config, window_dir)
            except Exception as exc:
                if (directory / 'CANCEL').exists(): raise
                news_error = str(exc)[:500]
        for si, seed in enumerate(spec['seeds']):
            for variant in arms:
                row = next(r for r in window_rows if r['seed']==seed and r['variant']==variant)
                if variant == 'price_news' and news_error:
                    row.update(status='failed', error='News preparation: '+news_error)
                    checkpoint('robustness_arm_failed')
                    continue
                row['status'] = 'running'
                checkpoint('robustness_training')
                run_config = deepcopy(config)
                run_config['news'] = variant == 'price_news'
                run_config['parameters']['seed'] = seed
                try:
                    model_dir = window_dir / ('seed-' + str(seed) + '-' + variant)
                    if variant == 'price_news':
                        from .terminal_news_engine import train as train_news
                        candidate = train_news(run_config, model_dir, candles, news_snapshot)
                    else:
                        candidate = train(run_config, model_dir, candles)
                    evaluation_metrics = evaluate_candidate(candidate)
                    row.update(status='succeeded', metrics=candidate['metrics'], evaluation_metrics=evaluation_metrics,
                               candidate_id=candidate['id'], period=candidate['period'],
                               price_snapshot_sha256=candidate['model_manifest']['files']['snapshot.feather'],
                               source_verified=candidate['model_manifest']['data_provenance']['source_verified'])
                except Exception as exc:
                    if (directory / 'CANCEL').exists(): raise
                    row.update(status='failed', error=str(exc)[:500] or type(exc).__name__)
                checkpoint('robustness_arm_complete')
    # Compare each pair on the identical price snapshot, window and random seed.
    pair_checks = []
    if len(arms) == 2:
        for window in spec['windows']:
            for seed in spec['seeds']:
                pair = [r for r in rows if r['window']==window and r['seed']==seed]
                ok = all(r['status']=='succeeded' for r in pair) and len({r.get('price_snapshot_sha256') for r in pair}) == 1
                pair_checks.append({'window':window, 'seed':seed, 'comparable':ok})
    report['pair_checks'] = pair_checks
    report['comparable'] = bool(pair_checks) and all(p['comparable'] for p in pair_checks)
    report['status'] = 'complete' if all(r['status']=='succeeded' for r in rows) else 'partial'
    checkpoint('robustness_complete')
    _write(directory / 'robustness-completion.json', {'comparison':report})
