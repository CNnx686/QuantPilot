"""Isolated SB3 training entry point. Writes every seed, including zero/negative results."""
import json
import importlib.metadata
import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd
import torch
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback
from .rl_config import PROTOCOL
from .rl_env import SpotEnv, features, fit_normalizer


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


class AuditCallback(BaseCallback):
    def __init__(self, directory, deadline):
        super().__init__()
        self.directory, self.deadline = directory, deadline
        self.rewards, self.episodes = [], []
        self.reward_sum = 0.0
        self.rollout_rewards = []
        self.invalid = 0

    def _on_step(self):
        if (self.directory / 'CANCEL').exists() or time.monotonic() > self.deadline:
            raise RuntimeError('Training cancelled or deadline exceeded')
        self.reward_sum += float(self.locals['rewards'][0])
        self.rollout_rewards.append(float(self.locals['rewards'][0]))
        self.invalid += int(not self.locals['infos'][0].get('valid', True))
        if self.locals['dones'][0]:
            self.episodes.append({'step': self.num_timesteps, 'reward': self.reward_sum})
            self.reward_sum = 0.0
        if self.num_timesteps % 256 == 0:
            self.rewards.append({'step': self.num_timesteps, 'rollout_reward_mean': float(np.mean(self.rollout_rewards))})
            self.rollout_rewards.clear()
        return True


def run(directory):
    torch.set_num_threads(2)
    torch.set_num_interop_threads(2)
    deadline = time.monotonic() + 900
    save(directory / 'rl-versions.json', {name: importlib.metadata.version(name)
        for name in ('torch', 'stable-baselines3', 'gymnasium', 'numpy', 'pandas', 'pyarrow')})
    candles = pd.read_feather(directory / 'market' / 'BTC_USDT-1h.feather')
    candles['date'] = pd.to_datetime(candles['date'], utc=True)
    values = features(candles)
    mask = (candles['date'] >= PROTOCOL['train'][0]) & (candles['date'] < PROTOCOL['train'][1])
    train, train_values = candles.loc[mask], values.loc[mask]
    normalizer = fit_normalizer(train_values)
    save(directory / 'normalizer.json', {**normalizer, 'fit_period': PROTOCOL['train'], 'rows': len(train), 'features': PROTOCOL['features']})
    windows = ('validation', 'development_evaluation')
    for seed in PROTOCOL['seeds']:
        out = directory / f'seed-{seed}'
        out.mkdir(exist_ok=True)
        save(directory / 'progress.json', {'phase': 'training', 'seed': seed, 'seeds': PROTOCOL['seeds']})
        params = dict(PROTOCOL['ppo'])
        architecture = params.pop('net_arch')
        model = PPO('MlpPolicy', SpotEnv(train, train_values, normalizer), device='cpu', seed=seed,
                    policy_kwargs={'net_arch': architecture}, verbose=0, **params)
        callback = AuditCallback(directory, deadline)
        started = time.monotonic()
        model.learn(total_timesteps=PROTOCOL['steps_per_seed'], callback=callback)
        model.save(out / 'model.zip')
        save(out / 'training.json', {'seed': seed, 'steps': model.num_timesteps,
            'seconds': time.monotonic() - started, 'episodes': callback.episodes,
            'partial_episode_reward': callback.reward_sum, 'invalid_actions': callback.invalid,
            'rollouts': callback.rewards, 'selection': 'Final checkpoint; no best-validation selection'})
        model = PPO.load(out / 'model.zip', device='cpu')
        for window in windows:
            if (directory / 'CANCEL').exists():
                raise RuntimeError('Cancelled')
            bounds = PROTOCOL[window]
            select = (candles['date'] >= bounds[0]) & (candles['date'] < bounds[1])
            env = SpotEnv(candles.loc[select], values.loc[select], normalizer)
            obs, _ = env.reset()
            records, equities = [], [PROTOCOL['wallet']]
            while True:
                if (directory / 'CANCEL').exists() or time.monotonic() > deadline:
                    raise RuntimeError('Inference cancelled or deadline exceeded')
                action, _ = model.predict(obs, deterministic=True)
                obs, reward, done, _, info = env.step(action)
                info.update({'model_fit_at': PROTOCOL['train'][1] + 'T00:00:00+00:00',
                             'model_label_max_at': PROTOCOL['train'][1] + 'T00:00:00+00:00'})
                records.append(info)
                equities.append(info['equity'])
                if done:
                    break
            pd.DataFrame(records).to_csv(out / f'{window}-actions.csv', index=False)
            array = np.asarray(equities)
            save(out / f'{window}-gym.json', {'metrics': {'net_return': env.equity / PROTOCOL['wallet'] - 1,
                'max_drawdown': float(np.max(1-array/np.maximum.accumulate(array))), 'trades': env.trades,
                'turnover': env.turnover / PROTOCOL['wallet'], 'invalid_actions': env.invalid_actions,
                'training_objective_reward': env.total_reward},
                'equity_kind': 'Gym mark-to-market hourly; terminal liquidation at last close; not Freqtrade execution'})
    save(directory / 'training-complete.json', {'seeds': PROTOCOL['seeds'], 'status': 'complete'})


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve())
