"""Causal close observations; next-open fills. Imported only by the isolated RL runtime."""
import gymnasium as gym
import numpy as np
import pandas as pd
from .rl_config import PROTOCOL


def features(candles):
    close = candles['close'].astype(float)
    result = pd.DataFrame(index=candles.index)
    for lag in (1, 6, 24):
        result[f'ret{lag}'] = close.pct_change(lag)
    result['vol48'] = close.pct_change().rolling(48).std(ddof=0)
    for size in (12, 48):
        result[f'sma{size}'] = close / close.rolling(size).mean() - 1
    return result


def fit_normalizer(values):
    array = np.asarray(values, dtype=np.float64)
    if not np.isfinite(array).all():
        raise ValueError('Non-finite training features')
    scale = array.std(axis=0)
    return {'mean': array.mean(axis=0).tolist(), 'scale': np.where(scale > 1e-12, scale, 1).tolist()}


class SpotEnv(gym.Env):
    metadata = {'render_modes': []}

    def __init__(self, candles, values, normalizer):
        super().__init__()
        self.candles = candles.reset_index(drop=True)
        self.values = np.asarray(values, dtype=np.float64)
        if len(candles) < 2 or not np.isfinite(self.values).all():
            raise ValueError('Need at least two complete finite candles')
        self.normalizer = normalizer
        self.action_space = gym.spaces.Discrete(3)
        self.observation_space = gym.spaces.Box(-10, 10, shape=(6,), dtype=np.float32)
        self.reset()

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.index, self.cash, self.units = 0, PROTOCOL['wallet'], 0.0
        self.peak = self.equity = PROTOCOL['wallet']
        self.drawdown = self.total_reward = self.turnover = 0.0
        self.invalid_actions = self.trades = 0
        return self._observation(), {}

    def _observation(self):
        scaled = (self.values[self.index] - self.normalizer['mean']) / np.asarray(self.normalizer['scale'])
        return np.clip(scaled, -10, 10).astype(np.float32)

    def step(self, action):
        action = int(action)
        if action not in (0, 1, 2):
            raise ValueError('Action outside hold/buy/sell')
        if self.index >= len(self.candles) - 1:
            raise RuntimeError('Episode already ended')
        current = self.candles.iloc[self.index]
        following = self.candles.iloc[self.index + 1]
        price, fee, stake = float(following['open']), PROTOCOL['fee'], PROTOCOL['stake']
        valid = not ((action == 1 and (self.units > 0 or self.cash < stake * (1 + fee))) or (action == 2 and self.units == 0))
        effective = action if valid else 0
        if not valid:
            self.invalid_actions += 1
        if effective == 1:
            self.units = stake / price
            self.cash -= stake * (1 + fee)
            self.turnover += stake
        elif effective == 2:
            gross = self.units * price
            self.cash += gross * (1 - fee)
            self.turnover += gross
            self.units = 0.0
            self.trades += 1
        self.index += 1
        terminated = self.index == len(self.candles) - 1
        forced_exit = False
        # Terminal liquidation is explicitly at the last close, unlike engine force-exit.
        if terminated and self.units:
            gross = self.units * float(following['close'])
            self.cash += gross * (1 - fee)
            self.turnover += gross
            self.units = 0.0
            self.trades += 1
            forced_exit = True
        equity = self.cash + self.units * float(following['close'])
        self.peak = max(self.peak, equity)
        drawdown = 1 - equity / self.peak
        reward = (equity - self.equity) / PROTOCOL['wallet'] - PROTOCOL['drawdown_penalty'] * max(0, drawdown - self.drawdown)
        self.equity, self.drawdown = equity, drawdown
        self.total_reward += reward
        info = {'candle_at': current['date'].isoformat(),
                'decision_at': (current['date'] + pd.Timedelta(hours=1)).isoformat(),
                'feature_available_at': (current['date'] + pd.Timedelta(hours=1)).isoformat(),
                'fill_at': following['date'].isoformat(), 'fill_price': price,
                'requested_action': action, 'action': effective, 'valid': valid,
                'forced_exit': forced_exit, 'equity': equity, 'reward': reward}
        return self._observation(), reward, terminated, False, info
