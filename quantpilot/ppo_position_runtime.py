"""Research-only PPO with an observed position bit; no live deployment support."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import gymnasium as gym
import numpy as np
import pandas as pd
import torch
from stable_baselines3 import PPO

from .terminal_features import execute_bar, new_account, normalize
from .terminal_rl import TerminalSpotEnv


def observation(price_features, account):
    """Position is taken from the strategy's own account at this decision instant."""
    values = np.asarray(price_features, dtype=np.float32)
    if values.shape != (6,) or not np.isfinite(values).all():
        raise ValueError('Need six finite normalized price features')
    return np.concatenate((values, np.array([float(account['units'] > 0)], dtype=np.float32)))


class PositionEnv(TerminalSpotEnv):
    def __init__(self, candles, values, fitted, config):
        super().__init__(candles, values, fitted, config)
        self.observation_space = gym.spaces.Box(-10, 10, shape=(7,), dtype=np.float32)

    def reset(self, *, seed=None, options=None):
        price, info = super().reset(seed=seed, options=options)
        return observation(price, self.account), info

    def step(self, action):
        price, reward, done, truncated, info = super().step(action)
        return observation(price, self.account), reward, done, truncated, info


def sequential_actions(frame, price_values, fitted, config, choose):
    """Predict at each closed candle, then execute at the following open."""
    frame = frame.reset_index(drop=True)
    values = normalize(price_values, fitted)
    if len(frame) != len(values) or len(frame) < 2:
        raise ValueError('Need aligned evaluation bars and features')
    account = new_account(config['wallet'])
    rows = []
    for index in range(len(frame)):
        obs = observation(values[index], account)
        action = int(choose(obs))
        if action not in (0, 1, 2):
            raise ValueError('Invalid model action')
        held = bool(account['units'])
        invalid = bool((action == 1 and held) or (action == 2 and not held))
        row = {'decision_at': (frame.iloc[index]['date'] + pd.Timedelta(hours=1)).isoformat(),
               'position_before': int(held), 'action': action, 'position_conflict': invalid,
               'executed_next_bar': index + 1 < len(frame)}
        if index + 1 < len(frame):
            effective, equity = execute_bar(account, action, frame.iloc[index + 1], config,
                                            previous_close=frame.iloc[index]['close'])
            row.update(effective_action=int(effective), equity=float(equity),
                       position_after=int(account['units'] > 0))
        rows.append(row)
    return rows


def main(directory):
    directory = Path(directory)
    config = json.loads((directory / 'effective-config.json').read_text(encoding='utf-8'))
    fitted = json.loads((directory / 'normalizer.json').read_text(encoding='utf-8'))
    train_frame = pd.read_feather(directory / 'rl-train.feather')
    train_values = np.load(directory / 'rl-train-values.npy', allow_pickle=False)
    eval_frame = pd.read_feather(directory / 'evaluation-frame.feather')
    eval_values = np.load(directory / 'evaluation-values.npy', allow_pickle=False)
    torch.set_num_threads(2)
    torch.set_num_interop_threads(2)
    model = PPO('MlpPolicy', PositionEnv(train_frame, train_values, fitted, config),
                seed=config['parameters']['seed'], device='cpu', n_steps=256, batch_size=64,
                n_epochs=5, learning_rate=.0003, gamma=.99,
                policy_kwargs={'net_arch': [32, 32]}, verbose=0)
    model.learn(total_timesteps=config['parameters']['rl_steps'])
    model.save(directory / 'model.zip')
    restored = PPO.load(directory / 'model.zip', device='cpu')
    if restored.observation_space.shape != (7,):
        raise ValueError('Saved position PPO dimensions mismatch')
    rows = sequential_actions(eval_frame, eval_values, fitted, config,
                              lambda obs: restored.predict(obs, deterministic=True)[0])
    (directory / 'position-decisions.json').write_text(json.dumps(rows, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Expected prepared research directory')
    main(sys.argv[1])
