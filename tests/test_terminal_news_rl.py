"""Run in the isolated RL runtime; exercises the new observation contract."""
import pytest
pytest.importorskip('gymnasium')
pytest.importorskip('stable_baselines3')

import numpy as np
import pandas as pd

from quantpilot.terminal_news_rl import NewsSpotEnv


def inputs():
    candles = pd.DataFrame({'date': pd.date_range('2025-01-01', periods=4, freq='h', tz='UTC'),
        'open': 100., 'high': 101., 'low': 99., 'close': 100., 'volume': 1.})
    values = np.zeros((4, 10))
    values[:, 6:] = [1., 2., 3., 4.]
    fitted = {'mean': [0.] * 10, 'scale': [1.] * 10, 'features': [f'feature-{i}' for i in range(10)]}
    config = {'wallet': 10000., 'stake': 1000., 'fee': .001, 'parameters': {'stop_loss': .05}}
    return candles, values, fitted, config


def test_price_news_observation_is_ten_dimensional_and_keeps_accounting():
    env = NewsSpotEnv(*inputs())
    observation, _ = env.reset()
    assert env.observation_space.shape == observation.shape == (10,)
    assert env.observation_space.contains(observation)
    np.testing.assert_array_equal(observation[6:], [1., 2., 3., 4.])
    following, _, _, _, info = env.step(1)
    assert info['effective_action'] == 1 and env.account['cash'] == 8999
    assert following.shape == (10,)
    env.step(2)
    assert env.account['cash'] == 9998


def test_news_columns_cannot_be_silently_removed_from_observation():
    candles, values, fitted, config = inputs()
    fitted['features'] = fitted['features'][:-1]
    with pytest.raises(ValueError, match='observation columns'):
        NewsSpotEnv(candles, values, fitted, config)
