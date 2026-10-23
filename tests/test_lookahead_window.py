from types import SimpleNamespace

import pandas as pd
import pytest

from quantpilot.lookahead_runner import comparison_frames


def frames():
    full = pd.DataFrame({'date': pd.date_range('2025-04-01', periods=8, freq='h', tz='UTC'),
                         '&-return6': [0.01] * 8, 'do_predict': [1] * 8})
    cut = full.iloc[:5].copy()
    cut.loc[4, ['&-return6', 'do_predict']] = 0
    return full, cut, cut.date.iloc[-1]


def test_only_exclusive_endpoint_is_outside_prediction_domain():
    full, cut, end = frames()
    left, right = comparison_frames(full, cut, end)
    assert len(right) == 4
    pd.testing.assert_frame_equal(left, right)
    assert cut.loc[4, 'do_predict'] == 0  # originals retained


@pytest.mark.parametrize('column', ['&-return6', 'do_predict', 'enter_long'])
def test_real_in_window_leak_is_not_whitelisted(column):
    full, cut, end = frames()
    full[column] = 1
    cut[column] = 1
    cut.loc[3, column] = 0
    left, right = comparison_frames(full, cut, end)
    assert column in left.compare(right).columns.get_level_values(0)


def test_unexpected_future_rows_rejected():
    full, cut, end = frames()
    with pytest.raises(ValueError, match='beyond'):
        comparison_frames(full, cut, end - pd.Timedelta(hours=1))


def test_upstream_detector_still_flags_future_feature():
    from freqtrade.optimize.analysis.lookahead import Analysis, LookaheadAnalysis
    full, cut, end = frames()
    full['future_close'] = [11, 12, 13, 14, 15, 16, 17, 18]
    cut['future_close'] = [11, 12, 13, float('nan'), float('nan')]
    left, right = comparison_frames(full, cut, end)
    analyzer = object.__new__(LookaheadAnalysis)
    analyzer.current_analysis = Analysis()
    analyzer.analyze_indicators(SimpleNamespace(indicators={'BTC/USDT': left}),
                               SimpleNamespace(indicators={'BTC/USDT': right}), 'BTC/USDT')
    assert 'future_close' in analyzer.current_analysis.false_indicators
