"""Transparent comparisons of completed experiments; no candidate selection."""
from collections import defaultdict
from datetime import datetime


def period_role(start: str, end: str) -> str:
    if "2025-04-01" <= start < end <= "2025-07-01":
        return "development"
    if "2025-07-01" <= start < end <= "2025-10-01":
        return "validation"
    return "mixed_or_legacy_development"


def breakdown(result: dict) -> dict:
    monthly = defaultdict(lambda: {"trades": 0, "profit_abs": 0.0})
    for trade in result["trades"]:
        month = trade["close_date"][:7]
        monthly[month]["trades"] += 1
        monthly[month]["profit_abs"] += trade["profit_abs"]
    first = datetime.fromisoformat(result["period"]["start"])
    last = datetime.fromisoformat(result["period"]["end"])
    # Notional time exposure ignores floating PnL and sums concurrent stakes.
    elapsed = (last - first).total_seconds()
    capital_seconds = sum(
        float(t["stake_amount"]) * max(0, (datetime.fromisoformat(t["close_date"]) - datetime.fromisoformat(t["open_date"])).total_seconds())
        for t in result["trades"]
    )
    initial = result["metrics"]["initial_balance"]
    turnover = result["metrics"]["turnover"]
    return {
        "monthly": [{"month": m, **values} for m, values in sorted(monthly.items())],
        "monthly_definition": "Closed-trade PnL grouped by exit month, not mark-to-market monthly returns",
        "stake_time_exposure": capital_seconds / (elapsed * initial) if elapsed > 0 else None,
        "exposure_definition": "Sum(stake * holding seconds) / (initial wallet * period seconds); no mark-to-market valuation",
        "cost_sensitivity": [{"extra_fee_per_side": fee, "estimated_net_return": result["metrics"]["net_return"] - fee * turnover} for fee in (0.0, 0.0005, 0.001)],
        "cost_definition": "Postprocessed extra fee on fixed entry/exit notionals; NOT engine reruns; ignores changed sizing, fills and trading paths",
    }


def compare(jobs: list[dict]) -> dict:
    reasons = []
    if len(jobs) != 2 or len({j["id"] for j in jobs}) != 2:
        reasons.append("请选择两个不同实验")
    for job in jobs:
        if job["status"] != "succeeded" or not job.get("result"):
            reasons.append(f"实验 {job['id'][:8]} 尚未成功完成")
    ready = len(jobs) == 2 and all(j.get("result") and j["status"] == "succeeded" for j in jobs)
    if ready:
        left, right = jobs
        for field in ("start", "end", "wallet", "stake", "fee"):
            if left["config"][field] != right["config"][field]:
                reasons.append(f"配置不一致：{field}")
        if sorted(left["config"]["pairs"]) != sorted(right["config"]["pairs"]):
            reasons.append("配置不一致：pairs")
        for field in ("data_sha256", "dataset_id"):
            values = [j["result"].get("provenance", {}).get(field) for j in jobs]
            if not all(values) or values[0] != values[1]:
                reasons.append(f"数据证据不一致或缺失：{field}")
        for field in ("engine_version", "mode"):
            values = [j["result"].get(field) for j in jobs]
            if not all(values) or values[0] != values[1]:
                reasons.append(f"引擎条件不一致或缺失：{field}")
        for field in ("fee_per_side", "fill", "stoploss", "max_open_trades", "timeframe", "slippage"):
            values = [j["result"].get("assumptions", {}).get(field) for j in jobs]
            if any(v is None for v in values) or values[0] != values[1]:
                reasons.append(f"运行假设不一致或缺失：{field}")
    rows = []
    for job in jobs:
        result = job.get("result")
        if job["status"] == "succeeded" and result:
            rows.append({"id": job["id"], "name": job["name"], "strategy": job["config"]["strategy"],
                         "config": job["config"], "metrics": result["metrics"], "period": result["period"],
                         "role": period_role(job["config"]["start"], job["config"]["end"]),
                         "provenance": result["provenance"], **breakdown(result)})
    return {"comparable": not reasons, "reasons": reasons, "rows": rows,
            "conclusion": "仅比较本次历史记录，不代表统计显著改善或未来收益；最终测试尚未运行。"}
