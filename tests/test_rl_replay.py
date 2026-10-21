import importlib.util
import pandas as pd
import pytest
from pathlib import Path


@pytest.fixture
def replay(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'strategies' / 'RLActionReplay.py'
    path = tmp_path / source.name
    path.write_bytes(source.read_bytes())
    spec = importlib.util.spec_from_file_location('tested_rl_replay', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RLActionReplay({}), tmp_path


def records():
    return pd.DataFrame([{'candle_at': '2025-04-01T00:00:00Z', 'decision_at': '2025-04-01T01:00:00Z',
        'feature_available_at': '2025-04-01T01:00:00Z', 'model_fit_at': '2025-04-01T00:00:00Z',
        'model_label_max_at': '2025-04-01T00:00:00Z', 'requested_action': 1, 'action': 0}])


def frame():
    return pd.DataFrame({'date': pd.to_datetime(['2025-04-01T00:00:00Z']), 'volume': [1]})


def test_replay_uses_requested_action_not_gym_hold(replay):
    strategy, path = replay
    records().to_csv(path / 'actions.csv', index=False)
    result = strategy.populate_entry_trend(strategy.populate_indicators(frame(), {'pair': 'BTC/USDT'}), {})
    assert result.iloc[0]['enter_long'] == 1


@pytest.mark.parametrize('column,value', [('model_fit_at', '2025-04-02T00:00:00Z'),
    ('feature_available_at', '2025-04-02T00:00:00Z'), ('model_label_max_at', '2025-04-02T00:00:00Z')])
def test_replay_rejects_future_information(replay, column, value):
    strategy, path = replay
    actions = records()
    actions[column] = value
    actions.to_csv(path / 'actions.csv', index=False)
    with pytest.raises(ValueError, match='causality'):
        strategy.populate_indicators(frame(), {'pair': 'BTC/USDT'})


def test_replay_rejects_duplicates_and_other_pair(replay):
    strategy, path = replay
    pd.concat([records(), records()]).to_csv(path / 'actions.csv', index=False)
    with pytest.raises(ValueError, match='duplicate'):
        strategy.populate_indicators(frame(), {'pair': 'BTC/USDT'})
    with pytest.raises(ValueError, match='BTC'):
        strategy.populate_indicators(frame(), {'pair': 'ETH/USDT'})
