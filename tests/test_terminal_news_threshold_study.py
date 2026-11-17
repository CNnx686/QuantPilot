from copy import deepcopy
import numpy as np
import pytest

from quantpilot.terminal_api import StrategyInput
from quantpilot.terminal_news_threshold_study import actions_for


def test_fixed_threshold_changes_neither_costs_nor_original_config():
    config = StrategyInput(kind='prediction').model_dump(mode='json')
    original = deepcopy(config)
    # Entry above 0.3%, exit below zero; buffered band +0.5% / -0.2%.
    values = np.array([.006, .004, -.001, -.003, .001])
    assert actions_for(values, config, 'baseline').tolist() == [1, 1, 2, 2, 0]
    assert actions_for(values, config, 'cost_buffer').tolist() == [1, 0, 0, 2, 0]
    assert config == original
    assert actions_for(values, config, 'cost_buffer')[:3].tolist() == actions_for(values[:3], config, 'cost_buffer').tolist()


@pytest.mark.parametrize('predictions,variant', [([float('nan')], 'baseline'), ([0], 'other'), ([[0]], 'cost_buffer')])
def test_rejects_missing_or_unsupported_signals(predictions, variant):
    with pytest.raises(ValueError):
        actions_for(predictions, StrategyInput(kind='prediction').model_dump(mode='json'), variant)
