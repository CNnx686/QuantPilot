"""Main-runtime coordinator: isolated PPO, then actual Freqtrade CLI replay and parsing."""
import json
import os
import shutil
import subprocess
import sys
import time
import statistics
import threading
from pathlib import Path
from . import settings, rl_store
from .data import get_dataset, sha256
from .engine import engine_config, parse_result, timerange_argument, write_json
from .rl_config import PROTOCOL, child_environment, python_path
from .public_worker import terminate_owned


def bounded(command, log_path, directory, seconds, env):
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=settings.ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + seconds
        try:
            while process.poll() is None:
                if (directory / 'CANCEL').exists():
                    raise RuntimeError('Cancelled')
                if time.monotonic() > deadline:
                    raise RuntimeError('Subprocess deadline exceeded')
                time.sleep(0.2)
            if process.returncode:
                raise RuntimeError(f'Subprocess exit {process.returncode}; see {log_path.relative_to(directory)}')
        finally:
            if process.poll() is None:
                terminate_owned(process)


def replay(directory, window, strategy_name, actions=None, seed=None):
    label = f'seed-{seed}' if seed is not None else strategy_name
    output = directory / 'replays' / window / label
    output.mkdir(parents=True)
    (output / 'strategies').mkdir()
    (output / 'userdata').mkdir()
    source = directory / 'source' / (strategy_name + '.py')
    target = output / 'strategies' / source.name
    shutil.copy2(source, target)
    if actions:
        shutil.copy2(actions, output / 'strategies' / 'actions.csv')
    config = {'strategy': strategy_name, 'start': PROTOCOL[window][0], 'end': PROTOCOL[window][1],
              'pairs': [PROTOCOL['pair']], 'stake': PROTOCOL['stake'], 'wallet': PROTOCOL['wallet'], 'fee': PROTOCOL['fee']}
    effective = engine_config(config, output)
    effective['datadir'] = str(directory / 'market')
    effective['rl_research_start'] = config['start'] + 'T00:00:00+00:00'
    write_json(output / 'effective-config.json', effective)
    command = [sys.executable, '-m', 'freqtrade', 'backtesting', '--config', str(output / 'effective-config.json'),
               '--strategy', strategy_name, '--strategy-path', str(output / 'strategies'),
               '--timerange', timerange_argument(config['start'], config['end']), '--cache', 'none',
               '--export', 'trades', '--backtest-directory', str(output), '--no-color',
               '--datadir', str(directory / 'market'), '--data-format-ohlcv', 'feather']
    provenance = {'command': command, 'strategy_sha256': sha256(target),
        'config_sha256': sha256(output / 'effective-config.json'),
        'dataset_manifest_sha256': sha256(directory / 'manifest.json'),
        'action_sha256': sha256(actions) if actions else None,
        'protocol_sha256': sha256(directory / 'protocol.json')}
    write_json(output / 'provenance.json', provenance)
    env = child_environment()
    # Public exchange metadata may require the user's proxy, never exchange/LLM keys.
    env.update({key: value for key, value in os.environ.items()
                if key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}})
    bounded(command, output / 'engine.log', directory, 180, env)
    archives = list(output.glob('backtest-result-*.zip'))
    if len(archives) != 1:
        raise RuntimeError('Expected one Freqtrade result archive')
    result = parse_result(archives[0], config, provenance)
    result['assumptions'].update({'stoploss': -0.05,
        'evaluation': 'Reused development evaluation; no untouched test, release, or investment claim',
        'policy': 'Frozen requested actions from price-only observations; Freqtrade decides position validity',
        'comparability': 'Same Freqtrade dates, BTC, wallet, fixed stake, fee and 5% stoploss across all rows'})
    result['checks'] = [check for check in result['checks'] if check['name'] != '最终样本外评估'] + [
        {'name': 'Untouched final test', 'status': 'unverified', 'detail': '2025Q4 not used; both displayed windows are development data'}]
    write_json(output / 'result.json', result)
    return {'window': window, 'strategy': strategy_name, 'seed': seed, 'metrics': result['metrics'],
            'result_artifact': (output / 'result.json').relative_to(directory).as_posix(),
            'raw_artifact': archives[0].relative_to(directory).as_posix()}


def execute(job):
    directory = rl_store.directory(job['id'])
    directory.mkdir(parents=True, exist_ok=True)
    if job['config'] != PROTOCOL:
        raise RuntimeError('Protocol version differs; submit a fresh job')
    manifest = get_dataset()
    if not manifest or not manifest['available'] or manifest['id'].split('-')[0] != 'binance':
        raise RuntimeError('Verified Binance development dataset unavailable')
    for entry in manifest['files']:
        if sha256(settings.ROOT / entry['path']) != entry['sha256']:
            raise RuntimeError('Dataset hash mismatch')
    (directory / 'market').mkdir()
    (directory / 'source').mkdir()
    entry = next(f for f in manifest['files'] if f['pair'] == PROTOCOL['pair'])
    shutil.copy2(settings.ROOT / entry['path'], directory / 'market' / 'BTC_USDT-1h.feather')
    if sha256(directory / 'market' / 'BTC_USDT-1h.feather') != entry['sha256']:
        raise RuntimeError('Copied dataset hash mismatch')
    sources = ['quantpilot/rl_config.py', 'quantpilot/rl_env.py', 'quantpilot/rl_train.py',
               'quantpilot/rl_runner.py', 'quantpilot/engine.py', 'strategies/RLActionReplay.py',
               'strategies/RLResearchSma.py', 'strategies/RLResearchBuyHold.py', 'requirements-rl.lock']
    hashes = {}
    for source in sources:
        path = settings.ROOT / source
        shutil.copy2(path, directory / 'source' / path.name)
        hashes[source] = sha256(path)
    write_json(directory / 'manifest.json', manifest)
    write_json(directory / 'protocol.json', PROTOCOL)
    write_json(directory / 'provenance.json', {'source_hashes': hashes, 'dataset_id': manifest['id'],
        'data_sha256': entry['sha256'], 'implementation': 'Standalone SB3 PPO + real Freqtrade 2026.9 action CSV replay; not native FreqAI RL',
        'native_hooks_inspected': ['ReinforcementLearner.fit', 'MyRLEnv.calculate_reward', 'Base3ActionRLEnv.step'],
        'final_test_used': False, 'cpu_threads': 2})
    bounded([str(python_path()), '-m', 'quantpilot.rl_train', str(directory)], directory / 'training.log',
            directory, 960, child_environment())
    rows = []
    for window in ('validation', 'development_evaluation'):
        for seed in PROTOCOL['seeds']:
            write_json(directory / 'progress.json', {'phase': 'freqtrade_replay', 'seed': seed, 'window': window})
            rows.append(replay(directory, window, 'RLActionReplay', directory / f'seed-{seed}' / f'{window}-actions.csv', seed))
            write_json(directory / 'partial-results.json', rows)
        for strategy in ('RLResearchSma', 'RLResearchBuyHold'):
            write_json(directory / 'progress.json', {'phase': 'baseline_replay', 'strategy': strategy, 'window': window})
            rows.append(replay(directory, window, strategy))
            write_json(directory / 'partial-results.json', rows)
    seed_artifacts = []
    for seed in PROTOCOL['seeds']:
        path = directory / f'seed-{seed}'
        seed_artifacts.append({'seed': seed, 'model_sha256': sha256(path / 'model.zip'),
            'training': json.loads((path / 'training.json').read_text()),
            'gym': {window: json.loads((path / f'{window}-gym.json').read_text()) for window in ('validation', 'development_evaluation')}})
    summary = {}
    for window in ('validation', 'development_evaluation'):
        returns = [row['metrics']['net_return'] for row in rows if row['window'] == window and row['seed'] is not None]
        mean = sum(returns) / len(returns)
        summary[window] = {'seed_count': len(returns), 'net_return_mean': mean,
            'net_return_std_population': (sum((v-mean)**2 for v in returns) / len(returns)) ** 0.5,
            'net_return_min': min(returns), 'net_return_max': max(returns)}
    result = {'protocol': PROTOCOL, 'rows': rows, 'all_seeds': seed_artifacts, 'summary': summary,
        'release_eligible': False, 'implementation': 'Standalone PPO + Freqtrade CSV replay',
        'limitations': ['Both evaluation windows have been used for development; not blind tests.',
            'Gym rewards/equity are separate diagnostics, never combined with Freqtrade curves.',
            'Gym uses exact quantities and terminal close liquidation, without intrabar stops; Freqtrade rounds lots, force-exits per engine rules and applies a 5% stoploss.',
            'No slippage, latency or market impact simulation; fee is 0.1% per side.',
            'Buy-and-hold is a risk-managed one-entry reference with 5% stoploss and fixed 1000 USDT, not the entire 10000 wallet.',
            'Code timing tests and exported action audit do not replace Freqtrade lookahead/recursive analysis.',
            'Final checkpoints from all five seeds are reported without a selected winner.']}
    for source, digest in hashes.items():
        if sha256(settings.ROOT / source) != digest:
            raise RuntimeError('Source changed during run; results not published: ' + source)
    for window in summary:
        summary[window]['net_return_median'] = statistics.median(row['metrics']['net_return'] for row in rows
            if row['window'] == window and row['seed'] is not None)
    write_json(directory / 'result.json', result)
    inventory = {p.relative_to(directory).as_posix(): {'sha256': sha256(p), 'bytes': p.stat().st_size}
                 for p in directory.rglob('*') if p.is_file() and p.name not in {'CANCEL', 'progress.json', 'inventory.json'}}
    write_json(directory / 'inventory.json', inventory)
    write_json(directory / 'progress.json', {'phase': 'complete'})


if __name__ == '__main__':
    job = rl_store.get(sys.argv[1])
    if not job or job['status'] != 'running':
        sys.exit(2)
    # Independent deadline remains active even if the scheduler/API process disappears.
    directory = rl_store.directory(job['id'])
    directory.mkdir(parents=True, exist_ok=True)
    watchdog = threading.Timer(PROTOCOL['max_seconds'], lambda: (directory / 'CANCEL').touch())
    watchdog.daemon = True
    watchdog.start()
    try:
        execute(job)
    finally:
        watchdog.cancel()
