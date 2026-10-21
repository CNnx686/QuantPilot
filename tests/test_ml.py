"""Causal invariants and rejected signal tests using synthetic data only."""
import importlib.util
from pathlib import Path
import numpy as np
import pandas as pd
import pytest


def load(relative):
    spec = importlib.util.spec_from_file_location(Path(relative).stem, Path(__file__).resolve().parents[1] / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_freqai_config_supports_real_engine_warmup_calculation():
    from freqtrade.data.dataprovider import DataProvider
    from quantpilot.ml import freqai_config
    config = {'timeframe': '1h', 'startup_candle_count': 60,
              'freqai': freqai_config('warmup-regression')}
    assert DataProvider(config, None).get_required_startup('1h') == 90 * 24 + 60


def frame():
    close = 100 + np.sin(np.arange(160) / 5)
    return pd.DataFrame({"date": pd.date_range("2025-01-01", periods=160, freq="h", tz="UTC"),
                         "close": close, "high": close+1, "low": close-1, "volume": 10 + np.arange(160) % 3})


def test_causal_features_are_prefix_invariant():
    strategy = load("strategies/FreqaiTrend.py").FreqaiTrend({})
    full = strategy.feature_engineering_standard(frame(), {})
    prefix = strategy.feature_engineering_standard(frame().iloc[:100].copy(), {})
    pd.testing.assert_frame_equal(full.iloc[:100], prefix)


def test_purge_uses_label_close_availability_and_preserves_known_labels():
    strategy = load("strategies/FreqaiTrend.py").FreqaiTrend({})
    purge = load("models/PurgedLightGBMRegressor.py").purge_training_frame
    df = frame()
    cutoff = df.date.iloc[100]
    # Even if a caller constructs labels from a future-containing frame, purge removes them.
    full = purge(strategy.set_freqai_targets(df.copy(), {}), cutoff)
    prefix = purge(strategy.set_freqai_targets(df.iloc[:100].copy(), {}), cutoff)
    pd.testing.assert_frame_equal(full, prefix)
    assert len(full) == 94
    assert full.date.max() + pd.Timedelta(hours=7) == cutoff
    assert full['&-return6'].notna().all()


@pytest.mark.parametrize('validity,prediction', [(0,.1),(-1,.1),(2,.1),(1,np.nan),(1,np.inf)])
def test_invalid_and_expired_predictions_never_enter(validity,prediction):
    strategy = load("strategies/FreqaiTrend.py").FreqaiTrend({})
    df = pd.DataFrame({'do_predict':[validity], '&-return6':[prediction], 'volume':[10]})
    assert strategy.populate_entry_trend(df, {})['enter_long'].fillna(0).iloc[0] == 0


def test_missing_feature_cannot_become_valid_prediction(monkeypatch, tmp_path):
    from types import SimpleNamespace
    module = load("models/PurgedLightGBMRegressor.py")
    model = object.__new__(module.PurgedLightGBMRegressor)
    model.config = {'user_data_dir': str(tmp_path/'userdata')}
    monkeypatch.setattr(module.BaseRegressionModel, 'predict', lambda *a, **k: (pd.DataFrame({'&-return6':[.1,.1]}),np.ones(2,dtype=int)))
    dk = SimpleNamespace(training_features_list=['%-feature'], pair='BTC/USDT', data={'quantpilot_audit':{
        'model_id':'test','fit_cutoff':'2025-01-01T00:00:00+00:00','model_label_max_at':'2025-01-01T00:00:00+00:00'}})
    df = pd.DataFrame({'date':pd.date_range('2025-01-01',periods=2,freq='h',tz='UTC'),'%-feature':[np.nan,1.]})
    _,validity = model.predict(df,dk)
    assert validity.tolist() == [0,1]


def test_swallowed_training_failure_rejected(tmp_path):
    from quantpilot.ml import collect_ml_evidence
    with pytest.raises(ValueError, match='Missing successful'):
        collect_ml_evidence(tmp_path, {})


def test_frozen_config_disables_internal_random_validation():
    from quantpilot.ml import freqai_config
    config = freqai_config('test')
    assert config['data_split_parameters'] == {'test_size':0,'shuffle':False}
    assert config['feature_parameters']['label_period_candles'] == 6
    assert config['continual_learning'] is False
    assert config['model_training_parameters']['random_state'] == 42
