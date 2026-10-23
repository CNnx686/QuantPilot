"""Versioned causal price inputs and the local-paper backtest accounting kernel."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

SCHEMA = 'terminal-price6-v1'
FEATURES = ['ret1', 'ret6', 'ret24', 'vol48', 'sma12', 'sma48']


def candles_frame(candles):
    frame = pd.DataFrame(candles).copy()
    required = ['date', 'open', 'high', 'low', 'close', 'volume']
    if not set(required).issubset(frame.columns) or len(frame) < 2:
        raise ValueError('Need dated hourly OHLCV candles')
    frame = frame[required].reset_index(drop=True)
    frame['date'] = pd.to_datetime(frame['date'], utc=True)
    frame[required[1:]] = frame[required[1:]].astype(float)
    values = frame[required[1:]].to_numpy()
    if not np.isfinite(values).all() or frame['date'].isna().any():
        raise ValueError('Candle data contains non-finite values')
    if (frame['low'] <= 0).any() or (frame['volume'] < 0).any():
        raise ValueError('Candle prices or volumes are invalid')
    if (frame['high'] < frame[['open', 'close', 'low']].max(axis=1)).any() or (frame['low'] > frame[['open', 'close']].min(axis=1)).any():
        raise ValueError('Invalid candle OHLC ordering')
    differences = frame['date'].diff().dropna()
    if not differences.eq(pd.Timedelta(hours=1)).all():
        raise ValueError('Candles must be unique, ordered, contiguous hourly bars')
    if not frame['date'].eq(frame['date'].dt.floor('h')).all():
        raise ValueError('Candle timestamps must be hour-aligned open times')
    return frame


def features(candles):
    close = candles['close'].astype(float)
    values = pd.DataFrame(index=candles.index)
    for lag in (1, 6, 24):
        values[f'ret{lag}'] = close.pct_change(lag, fill_method=None)
    values['vol48'] = close.pct_change(fill_method=None).rolling(48).std(ddof=0)
    for size in (12, 48):
        values[f'sma{size}'] = close / close.rolling(size).mean() - 1
    return values[FEATURES]


def normalizer(values):
    array = np.asarray(values, dtype=float)
    if len(array) < 2 or not np.isfinite(array).all():
        raise ValueError('Training features are missing or non-finite')
    scale = array.std(axis=0)
    return {'mean': array.mean(axis=0).tolist(), 'scale': np.where(scale > 1e-12, scale, 1).tolist()}


def normalize(values, fitted):
    array = (np.asarray(values, dtype=float) - np.asarray(fitted['mean'])) / np.asarray(fitted['scale'])
    if not np.isfinite(array).all():
        raise ValueError('Invalid inference features or fitted normalizer')
    return np.clip(array, -10, 10).astype(np.float32)


def new_account(wallet):
    return {'cash': float(wallet), 'units': 0.0, 'entry_price': None, 'entry_cost': 0.0,
            'entry_at': None, 'turnover': 0.0, 'trades': []}


def close_position(account, price, fee, when, reason):
    if not account['units']:
        return
    gross = account['units'] * float(price)
    net = gross * (1 - fee)
    account['cash'] += net
    account['turnover'] += gross
    account['trades'].append({'open_date': account['entry_at'], 'close_date': str(when),
        'open_rate': account['entry_price'], 'close_rate': float(price), 'amount': account['units'],
        'profit_abs': net - account['entry_cost'], 'exit_reason': reason,
        'fee_open': fee, 'fee_close': fee})
    account.update(units=0.0, entry_price=None, entry_cost=0.0, entry_at=None)


def execute_bar(account, action, bar, config, previous_close=None):
    """Previous closed-bar signal/risk decision; execution at the next open."""
    price, fee, stake = float(bar['open']), config['fee'], config['stake']
    requested, effective = int(action), 0
    stop = config['parameters']['stop_loss']
    stopped = bool(account['units'] and previous_close is not None
                   and float(previous_close) <= account['entry_price'] * (1 - stop))
    if stopped:
        close_position(account, price, fee, bar['date'], 'stop_loss_closed_candle')
        effective = 2
    elif requested == 1 and not account['units'] and account['cash'] >= stake * (1 + fee):
        account.update(units=stake / price, entry_price=price, entry_cost=stake * (1 + fee), entry_at=str(bar['date']))
        account['cash'] -= stake * (1 + fee)
        account['turnover'] += stake
        effective = 1
    elif requested == 2 and account['units']:
        close_position(account, price, fee, bar['date'], 'model_exit')
        effective = 2
    equity = account['cash'] + account['units'] * float(bar['close'])
    if not math.isfinite(equity) or account['cash'] < -1e-8:
        raise ValueError('Paper accounting invariant failed')
    return effective, equity
