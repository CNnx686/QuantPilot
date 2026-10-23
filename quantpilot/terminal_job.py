"""One cancellable terminal training process. Never publishes its own candidate."""
import json
import sys

from . import terminal_store as store


def main(identifier):
    from .terminal_engine import train
    from .terminal_market import prepare_dataset
    job = store.get('jobs', identifier)
    if job['status'] != 'running':
        raise RuntimeError('Task was cancelled before execution')
    directory = store.job_directory(identifier)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        candles = prepare_dataset(job['config'], directory)
        if (directory / 'CANCEL').exists():
            raise RuntimeError('Task cancelled')
        store.progress(identifier, 'training_and_backtesting')
        candidate = train(job['config'], directory / 'model', candles)
        if (directory / 'CANCEL').exists():
            raise RuntimeError('Task cancelled')
        temporary = directory / 'candidate.json.tmp'
        temporary.write_text(json.dumps(candidate, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(directory / 'candidate.json')
    except Exception as exc:
        (directory / 'error.json').write_text(json.dumps({'error': str(exc)[:2000] or type(exc).__name__}, ensure_ascii=False), encoding='utf-8')
        raise


if __name__ == '__main__':
    main(sys.argv[1])
