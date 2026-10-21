"""Bounded Freqtrade subprocess and evidence-backed artifact transformation."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from quantpilot.data import ROOT, get_dataset, sha256


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def timerange_argument(start: str, end: str) -> str:
    # Freqtrade's stop bound is inclusive; our public API is [start, end).
    left = int(datetime.combine(date.fromisoformat(start), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    right = int(datetime.combine(date.fromisoformat(end), datetime.min.time(), tzinfo=timezone.utc).timestamp()) - 1
    return f"{left}-{right}"


def engine_config(config: dict, job_dir: Path) -> dict:
    return {
        "dry_run": True, "trading_mode": "spot", "margin_mode": "",
        "max_open_trades": len(config["pairs"]), "stake_currency": "USDT",
        "stake_amount": config["stake"], "dry_run_wallet": config["wallet"],
        "tradable_balance_ratio": 0.99, "timeframe": "1h", "fee": config["fee"],
        "dataformat_ohlcv": "feather", "user_data_dir": str(job_dir / "userdata"),
        "datadir": str(job_dir / "market"),
        "exchange": {"name": "binance", "key": "", "secret": "",
            "pair_whitelist": config["pairs"], "pair_blacklist": [],
            "ccxt_config": {"enableRateLimit": True, "timeout": 15000, "aiohttp_trust_env": True,
                "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}}}},
        "pairlists": [{"method": "StaticPairList"}],
        "entry_pricing": {"price_side": "other", "use_order_book": False},
        "exit_pricing": {"price_side": "other", "use_order_book": False},
        "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes"},
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
    }


def parse_result(archive: Path, config: dict, provenance: dict) -> dict:
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if n.endswith(".json") and not n.endswith("_config.json")]
        raw = next((json.loads(zf.read(n)) for n in names if "strategy" in json.loads(zf.read(n))), None)
    strategy_name = config.get("strategy", "SmaTrend")
    if raw is None or strategy_name not in raw["strategy"]:
        raise ValueError(f"Freqtrade archive does not contain {strategy_name} results")
    stat = raw["strategy"][strategy_name]
    required = {"trades", "starting_balance", "final_balance", "max_drawdown_account", "pairlist", "timeframe", "stake_currency", "total_trades", "backtest_start_ts", "backtest_end_ts", "backtest_start", "backtest_end", "profit_total_abs"}
    if required - stat.keys():
        raise ValueError(f"Incomplete engine artifact: {sorted(required - stat.keys())}")
    start_ms = int(datetime.fromisoformat(config["start"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(config["end"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
    if set(stat["pairlist"]) != set(config["pairs"]) or stat["timeframe"] != "1h" or stat["stake_currency"] != "USDT":
        raise ValueError("Engine market configuration does not match request")
    if stat["backtest_start_ts"] != start_ms or stat["backtest_end_ts"] != end_ms - 3_600_000:
        raise ValueError("Engine period does not match complete requested hourly interval")
    if not math.isclose(float(stat["starting_balance"]), config["wallet"], abs_tol=1e-8):
        raise ValueError("Engine initial balance does not match request")
    if len(stat["trades"]) != stat["total_trades"]:
        raise ValueError("Engine trade count mismatch")
    trades = [{key: trade.get(key) for key in ["pair", "open_date", "close_date", "open_rate", "close_rate", "profit_abs", "profit_ratio", "exit_reason", "stake_amount", "amount", "fee_open", "fee_close", "enter_tag"]} for trade in stat["trades"]]
    initial = float(stat["starting_balance"] if "starting_balance" in stat else stat["dry_run_wallet"])
    final = float(stat["final_balance"])
    profit = sum(float(t["profit_abs"]) for t in trades)
    if not math.isclose(profit, stat["profit_total_abs"], abs_tol=1e-5):
        raise ValueError("Engine reported profit does not reconcile with trades")
    for trade in trades:
        if trade["pair"] not in config["pairs"]:
            raise ValueError("Unexpected pair in trade artifact")
        for key in ("fee_open", "fee_close"):
            if not math.isclose(float(trade[key]), config["fee"], abs_tol=1e-10):
                raise ValueError("Engine trade fee does not match request")
    if not math.isclose(initial + profit, final, abs_tol=1e-5):
        raise ValueError("Engine balance does not reconcile with trade profit")
    balance = initial
    equity = [{"time": config["start"] + "T00:00:00Z", "value": balance}]
    for trade in sorted(trades, key=lambda t: t["close_date"]):
        balance += float(trade["profit_abs"])
        equity.append({"time": trade["close_date"], "value": balance})
    equity.append({"time": config["end"] + "T00:00:00Z", "value": balance})
    turnover = sum(float(t["amount"]) * (float(t["open_rate"]) + float(t["close_rate"])) for t in trades) / initial
    count = len(trades)
    result = {
        "engine_version": importlib.metadata.version("freqtrade"), "strategy": strategy_name, "mode": "historical_backtest",
        "metrics": {"net_return": profit / initial, "profit_abs": profit,
            "max_drawdown": float(stat["max_drawdown_account"]), "trades": count,
            "win_rate": sum(t["profit_abs"] > 0 for t in trades) / count if count else None,
            "initial_balance": initial, "final_balance": final, "turnover": turnover},
        "equity": equity, "trades": trades, "period": {"start": config["start"], "end": config["end"], "engine_start": stat.get("backtest_start"), "engine_end": stat.get("backtest_end")},
        "assumptions": {"fee_per_side": config["fee"], "slippage": "Not modeled; engine OHLC fills only", "fill": "Signal on closed candle, execution on subsequent candle per Freqtrade", "max_open_trades": len(config["pairs"]), "stoploss": -0.05, "timeframe": "1h", "timezone": "UTC", "equity": "Realized closed-trade balance, not mark-to-market equity", "drawdown": "Freqtrade closed-trade account drawdown", "turnover": "Sum of entry plus exit notionals / initial balance", "evaluation": "Development baseline only; no ML training or untouched final evaluation"},
        "provenance": {**provenance, "raw_artifact": archive.name, "raw_sha256": sha256(archive), "metrics_transform": "quantpilot.engine.parse_result"},
        "checks": [{"name": "行情完整性与哈希", "status": "passed", "detail": "使用前校验每个行情文件并保存运行快照"}, {"name": "资金核对", "status": "passed", "detail": "初始资金 + 所有成交净损益 = 最终资金"}, {"name": "未来数据专项检查", "status": "unverified", "detail": "尚未运行 lookahead-analysis；代码检查不能替代专项验证"}, {"name": "最终样本外评估", "status": "unverified", "detail": "第一周为开发基线，不构成最终策略评估"}],
    }
    json.dumps(result, allow_nan=False)
    return result


def run_backtest(job: dict, job_dir: Path) -> dict:
    job_dir.mkdir(parents=True, exist_ok=True)
    cfg = job["config"]
    manifest = get_dataset()
    if not manifest or not manifest.get("available"):
        raise RuntimeError("缺少本地真实行情；请先运行 python -m quantpilot.data")
    if not manifest["available_start"] <= cfg["start"] < cfg["end"] <= manifest["available_end"]:
        raise ValueError("Requested dates outside verified dataset")
    date.fromisoformat(cfg["start"])
    date.fromisoformat(cfg["end"])
    if cfg["strategy"] not in ("SmaTrend", "FreqaiTrend") or not set(cfg["pairs"]).issubset(manifest["pairs"]):
        raise ValueError("Unsupported strategy or pair")
    if cfg.get("dataset_id") and cfg["dataset_id"] != manifest["id"]:
        raise ValueError("Requested dataset snapshot is no longer current")
    is_ml = cfg["strategy"] == "FreqaiTrend"
    if is_ml and not ("2025-04-01" <= cfg["start"] < cfg["end"] <= "2025-10-01"):
        raise ValueError("ML period must remain inside declared development/validation windows")
    version = importlib.metadata.version("freqtrade")
    if version != "2026.9":
        raise RuntimeError(f"Expected tested Freqtrade 2026.9, got {version}")
    (job_dir / "market").mkdir(exist_ok=True)
    (job_dir / "userdata").mkdir(exist_ok=True)
    (job_dir / "strategies").mkdir(exist_ok=True)
    for file in manifest["files"]:
        if file["pair"] not in cfg["pairs"]:
            continue
        source = ROOT / file["path"]
        destination = job_dir / "market" / source.name
        shutil.copy2(source, destination)
        if sha256(destination) != file["sha256"]:
            raise ValueError(f"行情文件被修改，哈希校验失败: {source.name}")
    strategy = job_dir / "strategies" / (cfg["strategy"] + ".py")
    shutil.copy2(ROOT / "strategies" / strategy.name, strategy)
    write_json(job_dir / "manifest.json", manifest)
    effective = engine_config(cfg, job_dir)
    if is_ml:
        from quantpilot.ml import freqai_config
        (job_dir / "models").mkdir(exist_ok=True)
        shutil.copy2(ROOT / "models" / "PurgedLightGBMRegressor.py", job_dir / "models" / "PurgedLightGBMRegressor.py")
        effective["freqai"] = freqai_config("qp-week2-" + job_dir.name)
        effective["freqaimodel"] = "PurgedLightGBMRegressor"
        effective["freqaimodel_path"] = str(job_dir / "models")
    write_json(job_dir / "effective-config.json", effective)
    protocol = {"id": "week2-v1", "development": ["2025-04-01", "2025-07-01"],
                "validation": ["2025-07-01", "2025-10-01"],
                "final_reserved_not_run": ["2025-10-01", "2026-01-01"],
                "candidate_count": 1, "search": "No hyperparameter search; failed engineering runs retained",
                "strategy": cfg["strategy"], "freqai": effective.get("freqai"),
                "definition_commit": "b3f71f1", "final_test_downloaded": False}
    write_json(job_dir / "research-protocol.json", protocol)
    command = [sys.executable, "-m", "freqtrade", "backtesting", "--config", str(job_dir / "effective-config.json"), "--strategy", cfg["strategy"], "--strategy-path", str(job_dir / "strategies"), "--timerange", timerange_argument(cfg["start"], cfg["end"]), "--cache", "none", "--export", "trades", "--backtest-directory", str(job_dir), "--no-color"]
    provenance = {"dataset_id": manifest["id"], "data_sha256": manifest["sha256"], "strategy_sha256": sha256(strategy), "adapter_sha256": sha256(Path(__file__)), "config_sha256": sha256(job_dir / "effective-config.json"), "source": manifest["source"], "engine_version": version, "command": command}
    provenance["protocol_sha256"] = sha256(job_dir / "research-protocol.json")
    command.extend(["--datadir", str(job_dir / "market"), "--data-format-ohlcv", "feather"])
    if is_ml:
        provenance["model_sha256"] = sha256(job_dir / "models" / "PurgedLightGBMRegressor.py")
        provenance["ml_adapter_sha256"] = sha256(ROOT / "quantpilot" / "ml.py")
        provenance["model_versions"] = {p: importlib.metadata.version(p) for p in ("lightgbm", "datasieve", "scikit-learn")}
    write_json(job_dir / "provenance.json", provenance)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FREQTRADE__")}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    with (job_dir / "engine.log").open("w", encoding="utf-8") as log:
        try:
            completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=1200 if is_ml else 240, env=env, shell=False)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Freqtrade 超过运行时间上限；请检查网络与日志") from error
    if completed.returncode:
        raise RuntimeError(f"Freqtrade 运行失败（exit {completed.returncode}），请查看引擎日志")
    archives = list(job_dir.glob("backtest-result-*.zip"))
    if len(archives) != 1:
        raise RuntimeError("Expected exactly one fresh Freqtrade result archive")
    result = parse_result(archives[0], cfg, provenance)
    if is_ml:
        from quantpilot.ml import collect_ml_evidence
        result["ml"] = collect_ml_evidence(job_dir, cfg)
        result["assumptions"]["evaluation"] = "Predeclared rolling 90-day training / 30-day backtest; final period reserved and not run"
        result["checks"].insert(2, {"name": "训练标签与预测时间审计", "status": "passed", "detail": "逐模型清除未可观察标签，核验预测时间及拟合记录；不替代完整未来数据专项检查"})
    return result
