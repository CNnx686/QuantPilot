"""Isolated PPO runtime for the separately versioned price-plus-news schema."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import sys

import gymnasium as gym
import numpy as np
import pandas as pd
import torch
from stable_baselines3 import PPO

from .terminal_rl import TerminalSpotEnv, save
from .terminal_features import normalize


class NewsSpotEnv(TerminalSpotEnv):
    """Keep v1 trading/reward semantics; declare this new model's observation size."""

    def __init__(self, candles, values, fitted, config):
        super().__init__(candles, values, fitted, config)
        if self.values.ndim != 2 or self.values.shape[1] != len(fitted['features']):
            raise ValueError('News PPO observation columns do not match the frozen normalizer')
        self.observation_space = gym.spaces.Box(-10, 10, shape=(self.values.shape[1],), dtype=np.float32)


def train(directory):
    config = json.loads((directory / 'effective-config.json').read_text(encoding='utf-8'))
    fitted = json.loads((directory / 'normalizer.json').read_text(encoding='utf-8'))
    candles = pd.read_feather(directory / 'rl-train.feather')
    values = np.load(directory / 'rl-train-values.npy', allow_pickle=False)
    evaluation = np.load(directory / 'evaluation-values.npy', allow_pickle=False)
    parameters = config['parameters']
    model = PPO('MlpPolicy', NewsSpotEnv(candles, values, fitted, config),
        seed=parameters['seed'], device='cpu', n_steps=256, batch_size=64, n_epochs=5,
        learning_rate=.0003, gamma=.99, policy_kwargs={'net_arch': [32, 32]}, verbose=0)
    model.learn(total_timesteps=parameters['rl_steps'])
    model.save(directory / 'model.zip')
    restored = PPO.load(directory / 'model.zip', device='cpu')
    if restored.observation_space.shape != (len(fitted['features']),):
        raise ValueError('Saved news PPO model has the wrong observation shape')
    actions, _ = restored.predict(normalize(evaluation, fitted), deterministic=True)
    np.save(directory / 'evaluation-actions.npy', actions.astype(np.int64), allow_pickle=False)
    save(directory / 'rl-training.json', {'steps': model.num_timesteps, 'seed': parameters['seed'],
        'versions': {name: importlib.metadata.version(name) for name in ('torch', 'stable-baselines3', 'gymnasium', 'numpy')},
        'observation_features': fitted['features'], 'observation_shape': list(restored.observation_space.shape),
        'observation': 'Frozen price and news columns; no portfolio state; invalid actions become hold in broker',
        'selection': 'Final checkpoint, one seed; no optimization on evaluation results'})


def infer(directory, input_path, output_path):
    fitted = json.loads((directory / 'normalizer.json').read_text(encoding='utf-8'))
    payload = json.loads(input_path.read_text(encoding='utf-8'))
    if payload['feature_names'] != fitted['features']:
        raise ValueError('Current news features differ from the frozen normalizer')
    values = np.asarray(payload['features'], dtype=float)
    model = PPO.load(directory / 'model.zip', device='cpu')
    if model.observation_space.shape != (len(fitted['features']),):
        raise ValueError('Frozen news PPO model has the wrong observation shape')
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
