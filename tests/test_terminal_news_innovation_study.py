import numpy as np
import pandas as pd
import pytest

from quantpilot.terminal_news_innovation_study import INNOVATION, add_innovation
from quantpilot.terminal_news_features import NEWS_FEATURES


def test_innovation_uses_strictly_prior_history_across_split_and_is_prefix_stable():
    values = pd.DataFrame({name: np.arange(40, dtype=float) for name in NEWS_FEATURES})
    whole = add_innovation(values)
    prefix = add_innovation(values.iloc[:30])
    assert np.isnan(whole.loc[0, INNOVATION[0]])
    # At index 30 use rows 6..29, including rows on the other side of a split.
    assert whole.loc[30, INNOVATION[0]] == pytest.approx(30 - np.mean(np.arange(6,30)))
    pd.testing.assert_frame_equal(whole.iloc[:30], prefix)
    changed = values.copy(); changed.loc[30:, NEWS_FEATURES[:2]] = 999
    pd.testing.assert_frame_equal(whole.iloc[:30], add_innovation(changed).iloc[:30])
    assert add_innovation(changed).loc[30, INNOVATION[0]] == pytest.approx(999 - np.mean(np.arange(6,30)))


def test_missing_and_nonfinite_are_rejected_without_zero_fill():
    with pytest.raises(ValueError): add_innovation(pd.DataFrame())
    values = pd.DataFrame({name: [0., 1.] for name in NEWS_FEATURES})
    values.loc[1, NEWS_FEATURES[0]] = np.nan
    with pytest.raises(ValueError): add_innovation(values)
