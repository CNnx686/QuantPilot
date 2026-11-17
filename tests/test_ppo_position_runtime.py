import numpy as np
import pandas as pd
import pytest

pytest.importorskip('gymnasium')

from quantpilot.ppo_position_runtime import PositionEnv, observation, sequential_actions
from quantpilot.terminal_features import execute_bar, new_account


def sample():
    frame = pd.DataFrame({'date':pd.date_range('2025-01-01', periods=5, freq='h', tz='UTC'),
        'open':[100.,101.,102.,103.,104.], 'high':[101.,102.,103.,104.,105.],
        'low':[99.,100.,101.,102.,103.], 'close':[100.,101.,102.,103.,104.], 'volume':[1.]*5})
    values = np.zeros((5,6), dtype=float)
    fitted = {'mean':[0.]*6,'scale':[1.]*6}
    config = {'wallet':10000.,'stake':1000.,'fee':.001,'parameters':{'stop_loss':.05}}
    return frame, values, fitted, config


def test_position_observation_and_environment_follow_executed_account():
    frame, values, fitted, config = sample()
    assert observation(values[0], {'units':0})[-1] == 0
    assert observation(values[0], {'units':1})[-1] == 1
    env = PositionEnv(frame, values, fitted, config)
    obs, _ = env.reset(); assert obs.shape == (7,) and obs[-1] == 0
    obs, *_ = env.step(1); assert obs[-1] == 1
    obs, *_ = env.step(2); assert obs[-1] == 0


def test_sequential_uses_own_previous_account_and_no_future_bar_leak():
    frame, values, fitted, config = sample()
    choose = lambda obs: 2 if obs[-1] else 1
    rows = sequential_actions(frame, values, fitted, config, choose)
    assert [r['position_before'] for r in rows] == [0,1,0,1,0]
    prefix = sequential_actions(frame.iloc[:3], values[:3], fitted, config, choose)
    assert [(r['position_before'],r['action']) for r in prefix] == [(r['position_before'],r['action']) for r in rows[:3]]
    altered = frame.copy(); altered.loc[3:,['open','high','low','close']] *= 2
    other = sequential_actions(altered, values, fitted, config, choose)
    assert [(r['position_before'],r['action']) for r in rows[:3]] == [(r['position_before'],r['action']) for r in other[:3]]
    account = new_account(config['wallet'])
    for i, row in enumerate(rows[:-1]):
        _, equity = execute_bar(account,row['action'],frame.iloc[i+1],config,previous_close=frame.iloc[i]['close'])
        assert row['equity'] == pytest.approx(equity)
    assert len(account['trades']) == 2
