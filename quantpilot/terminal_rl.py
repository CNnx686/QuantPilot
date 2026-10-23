"""Isolated CPU PPO training/current inference; never imports historical action CSVs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import gymnasium as gym
import numpy as np
import pandas as pd
import torch
from stable_baselines3 import PPO

from .terminal_features import close_position, execute_bar, new_account, normalize


class TerminalSpotEnv(gym.Env):
    metadata = {'render_modes': []}

    def __init__(self, candles, values, fitted, config):
        self.candles = candles.reset_index(drop=True)
        self.values = normalize(values, fitted)
        self.config = config
        self.action_space = gym.spaces.Discrete(3)
        self.observation_space = gym.spaces.Box(-10, 10, shape=(6,), dtype=np.float32)
        self.reset()

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.index = 0
        self.account = new_account(self.config['wallet'])
        self.equity = self.peak = self.config['wallet']
        self.drawdown = 0.0
        return self.values[0], {}

    def step(self, action):
        if self.index >= len(self.candles) - 1:
            raise RuntimeError('Episode ended')
        if int(action) not in (0, 1, 2):
            raise ValueError('Invalid action')
        self.index += 1
        bar = self.candles.iloc[self.index]
        effective, equity = execute_bar(self.account, int(action), bar, self.config,
                                       previous_close=self.candles.iloc[self.index - 1]['close'])
        done = self.index == len(self.candles) - 1
        if done:
            close_position(self.account, bar['close'], self.config['fee'], bar['date'] + pd.Timedelta(hours=1), 'training_end')
            equity = self.account['cash']
        self.peak = max(self.peak, equity)
        drawdown = 1 - equity / self.peak
        reward = (equity - self.equity) / self.config['wallet'] - .1 * max(0, drawdown - self.drawdown)
        self.equity, self.drawdown = equity, drawdown
        return self.values[self.index], reward, done, False, {'effective_action': effective}


def save(path, value):
    path.write_text(json.dumps(value, allow_nan=False, indent=2), encoding='utf-8')


def train(directory):
    config = json.loads((directory / 'effective-config.json').read_text(encoding='utf-8'))
    fitted = json.loads((directory / 'normalizer.json').read_text(encoding='utf-8'))
    train_candles = pd.read_feather(directory / 'rl-train.feather')
    values = np.load(directory / 'rl-train-values.npy', allow_pickle=False)
    evaluation = np.load(directory / 'evaluation-values.npy', allow_pickle=False)
    parameters = config['parameters']
    model = PPO('MlpPolicy', TerminalSpotEnv(train_candles, values, fitted, config),
                seed=parameters['seed'], device='cpu', n_steps=256, batch_size=64,
                n_epochs=5, learning_rate=.0003, gamma=.99,
                policy_kwargs={'net_arch': [32, 32]}, verbose=0)
    model.learn(total_timesteps=parameters['rl_steps'])
    model.save(directory / 'model.zip')
    # Reload the actual serialized artifact, including for the historical evaluation.
    restored = PPO.load(directory / 'model.zip', device='cpu')
    actions, _ = restored.predict(normalize(evaluation, fitted), deterministic=True)
    np.save(directory / 'evaluation-actions.npy', actions.astype(np.int64), allow_pickle=False)
    import importlib.metadata as metadata
    save(directory / 'rl-training.json', {'steps': model.num_timesteps, 'seed': parameters['seed'],
        'versions': {package: metadata.version(package) for package in ('torch', 'stable-baselines3', 'gymnasium', 'numpy')},
        'observation': 'Six causal price features; no portfolio state; invalid actions become hold in broker',
        'selection': 'Final checkpoint, one seed; no optimization on evaluation results'})


def infer(directory, input_path, output_path):
    fitted = json.loads((directory / 'normalizer.json').read_text(encoding='utf-8'))
    values = np.asarray(json.loads(input_path.read_text(encoding='utf-8'))['features'], dtype=float)
    model = PPO.load(directory / 'model.zip', device='cpu')
    actions, _ = model.predict(normalize(values, fitted), deterministic=True)
    save(output_path, {'actions': np.atleast_1d(actions).astype(int).tolist()})


if __name__ == '__main__':
    torch.set_num_threads(2)
    torch.set_num_interop_threads(2)
    command, folder, *other = sys.argv[1:]
    if command == 'train' and not other:
        train(Path(folder).resolve())
    elif command == 'infer' and len(other) == 2:
        infer(Path(folder).resolve(), Path(other[0]).resolve(), Path(other[1]).resolve())
    else:
        raise SystemExit('Expected train DIR or infer DIR INPUT OUTPUT')
