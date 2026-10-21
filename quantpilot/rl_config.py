"""Frozen, intentionally small development-only PPO protocol. No user code or paths."""
import os
from . import settings

PROTOCOL = {
    'id': 'ppo-spot-v1', 'pair': 'BTC/USDT', 'timeframe': '1h',
    'seeds': [7, 17, 27, 37, 47], 'steps_per_seed': 8192, 'cpu_threads': 2,
    'train': ['2025-01-01', '2025-04-01'],
    'validation': ['2025-04-01', '2025-07-01'],
    'development_evaluation': ['2025-07-01', '2025-10-01'],
    'final_reserved_not_run': ['2025-10-01', '2026-01-01'],
    'wallet': 10000.0, 'stake': 1000.0, 'fee': 0.001,
    'drawdown_penalty': 0.1, 'max_seconds': 1800,
    'features': ['ret1', 'ret6', 'ret24', 'vol48', 'sma12', 'sma48'],
    'ppo': {'n_steps': 256, 'batch_size': 64, 'n_epochs': 5, 'learning_rate': 0.0003,
            'gamma': 0.99, 'net_arch': [32, 32]},
    'evaluation': 'Reused development data; not a blind test. No model selection or release.',
}


def python_path():
    return settings.ROOT / 'runtime' / 'rl-venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def artifact_root():
    return settings.ARTIFACTS / 'rl'


def child_environment():
    # Credential/proxy/PYTHONPATH/engine overrides are deliberately not inherited.
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'LANG',
               'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update({'QUANTPILOT_HOME': str(settings.HOME), 'PYTHONUTF8': '1',
                'PYTHONIOENCODING': 'utf-8', 'OMP_NUM_THREADS': '2',
                'MKL_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2'})
    return env
