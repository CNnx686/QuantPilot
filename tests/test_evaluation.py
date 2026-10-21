"""Synthetic accounting fixtures, not investment results."""
from copy import deepcopy
import pytest
from quantpilot.evaluation import compare, breakdown, period_role


def job(identifier):
    return {"id": identifier, "name": identifier, "status": "succeeded", "config": {
        "strategy": "SmaTrend", "start": "2025-04-01", "end": "2025-07-01", "pairs": ["BTC/USDT"], "wallet": 1000, "stake": 100, "fee": .001},
        "result": {"engine_version": "2026.9", "mode": "historical_backtest", "trades": [],
            "period": {"start": "2025-04-01", "end": "2025-07-01"},
            "metrics": {"initial_balance": 1000, "net_return": 0, "turnover": 0},
            "provenance": {"dataset_id": "synthetic", "data_sha256": "abc"},
            "assumptions": {"fee_per_side": .001, "fill": "next candle", "stoploss": -.05, "max_open_trades": 1, "timeframe": "1h", "slippage": "none"}}}


def test_comparison_rejects_different_costs_and_missing_evidence():
    left, right = job("a"), job("b")
    right["config"]["strategy"] = "FreqaiTrend"
    assert compare([left, right])["comparable"]
    right["config"]["fee"] = .002
    assert not compare([left, right])["comparable"]
    right = job("b")
    del right["result"]["provenance"]["data_sha256"]
    assert not compare([left, right])["comparable"]
    right["status"] = "failed"
    assert not compare([left, right])["comparable"]


def test_monthly_and_extra_fee_are_explicit_fixed_path_accounting():
    result = deepcopy(job("a")["result"])
    result["metrics"].update(net_return=.01, turnover=2)
    result["trades"] = [{"open_date": "2025-04-30T00:00:00+00:00", "close_date": "2025-05-01T00:00:00+00:00", "profit_abs": 10, "stake_amount": 100}]
    report = breakdown(result)
    assert report["monthly"] == [{"month": "2025-05", "trades": 1, "profit_abs": 10}]
    assert report["cost_sensitivity"][-1]["estimated_net_return"] == pytest.approx(.008)
    assert report["stake_time_exposure"] == pytest.approx(.1 / 91)
    assert period_role("2025-07-01", "2025-10-01") == "validation"


@pytest.mark.parametrize("field", ["engine_version", "mode"])
def test_missing_engine_evidence_is_not_comparable(field):
    left, right = job("a"), job("b")
    del left["result"][field]
    del right["result"][field]
    assert not compare([left, right])["comparable"]
