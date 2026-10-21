"""Synthetic fixtures test transformations, never used as market evidence."""
import json
import zipfile
from pathlib import Path

import pytest

from quantpilot.engine import parse_result, run_backtest, timerange_argument


def archive(tmp_path, trades, final=1000):
    path = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("synthetic.json", json.dumps({"strategy": {"SmaTrend": {"trades": trades, "dry_run_wallet": 1000, "starting_balance": 1000, "final_balance": final, "max_drawdown_account": 0, "pairlist": ["BTC/USDT"], "timeframe": "1h", "stake_currency": "USDT", "total_trades": len(trades), "backtest_start_ts": 1735689600000, "backtest_end_ts": 1738364400000, "backtest_start": "2025-01-01 00:00:00", "backtest_end": "2025-01-31 23:00:00", "profit_total_abs": sum(t["profit_abs"] for t in trades)}}}))
    return path


CONFIG = {"start": "2025-01-01", "end": "2025-02-01", "fee": 0.001, "wallet": 1000, "pairs": ["BTC/USDT"]}


def test_zero_trades_is_defined_without_fake_win_rate(tmp_path):
    result = parse_result(archive(tmp_path, []), CONFIG, {})
    assert result["metrics"]["win_rate"] is None
    assert result["metrics"]["net_return"] == 0
    assert result["equity"][-1]["value"] == 1000
    assert result["checks"][-1]["status"] == "unverified"


def test_reconcile_fails_instead_of_publishing_false_balance(tmp_path):
    with pytest.raises(ValueError, match="reconcile"):
        parse_result(archive(tmp_path, [], final=1100), CONFIG, {})


def test_trade_profit_and_two_sided_turnover(tmp_path):
    trade = {"pair": "BTC/USDT", "open_date": "2025-01-02T00:00:00Z", "close_date": "2025-01-03T00:00:00Z", "open_rate": 100, "close_rate": 110, "amount": 2, "profit_abs": 19.58, "profit_ratio": .0979, "stake_amount": 200}
    trade.update(fee_open=0.001, fee_close=0.001)
    result = parse_result(archive(tmp_path, [trade], final=1019.58), CONFIG, {})
    assert result["metrics"]["profit_abs"] == 19.58
    assert result["metrics"]["turnover"] == .42
    assert result["metrics"]["win_rate"] == 1


def test_missing_dataset_fails_before_engine_launch(tmp_path, monkeypatch):
    monkeypatch.setattr("quantpilot.engine.get_dataset", lambda: None)
    with pytest.raises(RuntimeError, match="缺少"):
        run_backtest({"config": {}}, tmp_path)


def test_strategy_features_are_prefix_invariant():
    import importlib.util
    import numpy as np
    import pandas as pd
    path = Path(__file__).resolve().parents[1] / "strategies" / "SmaTrend.py"
    spec = importlib.util.spec_from_file_location("baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy = module.SmaTrend({})
    frame = pd.DataFrame({"close": 100 + np.sin(np.arange(150) / 5), "volume": 10})
    full = strategy.populate_indicators(frame.copy(), {})
    prefix = strategy.populate_indicators(frame.iloc[:100].copy(), {})
    pd.testing.assert_frame_equal(full.iloc[:100], prefix)


def test_exclusive_api_bound_maps_to_engine_inclusive_bound():
    import pandas as pd
    from freqtrade.configuration import TimeRange
    from freqtrade.data.converter import trim_dataframe
    interval = TimeRange.parse_timerange(timerange_argument("2025-01-01", "2025-02-01"))
    frame = pd.DataFrame({"date": pd.to_datetime(["2025-01-31T23:00:00Z", "2025-02-01T00:00:00Z"])})
    trimmed = trim_dataframe(frame, interval)
    assert len(trimmed) == 1
    assert trimmed.iloc[0]["date"].isoformat() == "2025-01-31T23:00:00+00:00"


@pytest.mark.parametrize("field,value", [("max_drawdown_account", None), ("timeframe", "5m"), ("backtest_end_ts", 1738368000000), ("pairlist", ["ETH/USDT"]), ("starting_balance", 2000)])
def test_inconsistent_engine_artifacts_are_rejected(tmp_path, field, value):
    path = archive(tmp_path, [])
    with zipfile.ZipFile(path) as zf:
        content = json.loads(zf.read("synthetic.json"))
    stats = content["strategy"]["SmaTrend"]
    if value is None:
        del stats[field]
    else:
        stats[field] = value
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("synthetic.json", json.dumps(content))
    with pytest.raises(ValueError):
        parse_result(path, CONFIG, {})

