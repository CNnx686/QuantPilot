import pytest
pytest.importorskip('gymnasium')
import numpy as np
import pandas as pd
from quantpilot.rl_env import features, fit_normalizer, SpotEnv


def candles(count=180):
    close = 100 + np.arange(count) * .1
    return pd.DataFrame({'date': pd.date_range('2025-01-01', periods=count, freq='h', tz='UTC'),
        'open': close, 'close': close, 'high': close+1, 'low': close-1, 'volume': np.ones(count)})


def test_features_prefix_is_causal_and_train_normalizer_unaffected_by_future():
    frame = candles()
    original = features(frame)
    altered = frame.copy()
    altered.loc[120:, 'close'] *= 50
    np.testing.assert_allclose(original.iloc[:120], features(altered).iloc[:120], equal_nan=True)
    assert fit_normalizer(original.iloc[48:120]) == fit_normalizer(features(altered).iloc[48:120])
    np.testing.assert_allclose(features(frame.iloc[:120]), original.iloc[:120], equal_nan=True)


def test_next_open_fee_invalid_actions_and_terminal_costs():
    frame = candles(5)
    frame['open'] = frame['close'] = 100.0
    env = SpotEnv(frame, np.zeros((5, 6)), {'mean': [0]*6, 'scale': [1]*6})
    observation, _ = env.reset()
    assert observation.shape == (6,)
    _, reward, _, _, buy = env.step(1)
    assert env.cash == 8999 and env.units == 10
    assert buy['fill_at'] == '2025-01-01T01:00:00+00:00'
    assert buy['decision_at'] == buy['fill_at'] == buy['feature_available_at']
    assert reward < 0
    _, _, _, _, invalid = env.step(1)
    assert not invalid['valid'] and invalid['action'] == 0
    _, _, _, _, sell = env.step(2)
    assert env.cash == 9998 and env.units == 0
    _, _, done, _, invalid_sell = env.step(2)
    assert done and not invalid_sell['valid']
    assert env.invalid_actions == 2 and env.trades == 1
    assert env.turnover == 2000


def test_execution_uses_next_open_and_observation_has_no_wallet_state():
    frame = candles(4)
    frame.loc[1, 'open'] = 200
    env = SpotEnv(frame, np.zeros((4, 6)), {'mean': [0]*6, 'scale': [1]*6})
    before = env._observation()
    env.step(1)
    assert env.units == 5
    np.testing.assert_equal(before, env._observation())
    env.step(0)
    _, _, done, _, info = env.step(0)
    assert done and info['forced_exit'] and env.units == 0


def test_bad_input_and_actions_rejected():
    with pytest.raises(ValueError):
        fit_normalizer([[float('nan')]*6])
    env = SpotEnv(candles(3), np.zeros((3, 6)), {'mean': [0]*6, 'scale': [1]*6})
    with pytest.raises(ValueError):
        env.step(3)
