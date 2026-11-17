import numpy as np
import pytest

from quantpilot.ppo_ensemble_study import vote


def test_vote_resolves_majority_and_split_without_looking_ahead():
    actions = [[1, 2, 0, 2], [1, 1, 0, 0], [2, 0, 2, 2]]
    assert vote(actions).tolist() == [1, 0, 0, 2]
    prefix = [row[:2] for row in actions]
    assert np.array_equal(vote(prefix), vote(actions)[:2])


@pytest.mark.parametrize('bad', [[[0], [1]], [[0], [1], [4]]])
def test_vote_rejects_invalid_members_or_actions(bad):
    with pytest.raises(ValueError):
        vote(bad)
