"""One cancellable terminal training process. Never publishes its own candidate."""
import json
import sys
from copy import deepcopy

from . import terminal_store as store


def main(identifier):
    from .terminal_engine import train
    from .terminal_market import prepare_dataset
    job = store.get('jobs', identifier)
    if job['status'] != 'running':
        raise RuntimeError('Task was cancelled before execution')
    directory = store.job_directory(identifier)
    directory.mkdir(parents=True, exist_ok=True)
    comparison = None
    active_arm = None
    def write(name, value):
        temporary = directory / (name + '.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(directory / name)
    def checkpoint(phase):
        if (directory / 'CANCEL').exists():
            raise RuntimeError('Task cancelled')
        if comparison is not None:
            write('comparison.json', comparison)
        store.progress(identifier, phase, comparison=comparison)
    try:
        candles = prepare_dataset(job['config'], directory)
        is_comparison = job.get('mode') == 'comparison'
        news_snapshot = None
        if is_comparison:
            from . import terminal_comparison
            comparison = terminal_comparison.initial(job['config'])
        if is_comparison or job['config'].get('news'):
            from .terminal_news import prepare_history
            checkpoint('preparing_news')
            news_snapshot = prepare_history(job['config'], directory)
            if comparison is not None:
                comparison['news_snapshot_id'] = news_snapshot.get('id') or news_snapshot.get('snapshot_id')
        if is_comparison:
            candidates = []
            for variant in terminal_comparison.VARIANTS:
                active_arm = variant
                comparison['arms'][variant] = {'status': 'running'}
                checkpoint('training_' + variant)
                config = deepcopy(job['config'])
                config['news'] = variant == 'price_news'
                if config['news']:
                    from .terminal_news_engine import train as train_news
                    candidate = train_news(config, directory / 'model-news', candles, news_snapshot)
                else:
                    candidate = train(config, directory / 'model-price', candles)
                candidate['variant'] = variant
                candidates.append(candidate)
                comparison['arms'][variant] = terminal_comparison.arm_result(candidate)
                checkpoint('completed_' + variant)
            comparison = terminal_comparison.complete(comparison, candidates)
            if not comparison['comparable']:
                raise ValueError('; '.join(comparison['reasons']))
            checkpoint('comparison_complete')
            write('completion.json', {'candidates': candidates, 'comparison': comparison})
        else:
            checkpoint('training_and_backtesting')
            if job['config'].get('news'):
                from .terminal_news_engine import train as train_news
                candidate = train_news(job['config'], directory / 'model', candles, news_snapshot)
            else:
                candidate = train(job['config'], directory / 'model', candles)
            checkpoint('training_complete')
            write('candidate.json', candidate)
    except Exception as exc:
        error = str(exc)[:2000] or type(exc).__name__
        if comparison is not None:
            comparison.update(status='failed', comparable=False, delta=None)
            if active_arm and comparison['arms'][active_arm]['status'] != 'succeeded':
                comparison['arms'][active_arm] = {'status': 'failed', 'error': error}
            comparison['reasons'] = [error]
            write('comparison.json', comparison)
        write('error.json', {'error': error})
        raise


if __name__ == '__main__':
    main(sys.argv[1])
