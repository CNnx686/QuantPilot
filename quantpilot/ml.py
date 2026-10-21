"""Frozen ML configuration and fail-closed evidence validation."""
import csv
import importlib.metadata
import json
from datetime import datetime


def freqai_config(identifier):
    return {"enabled": True, "identifier": identifier, "train_period_days": 90,
            "backtest_period_days": 30, "save_backtest_models": True, "activate_tensorboard": False,
            "continual_learning": False, "feature_parameters": {"include_timeframes": ["1h"],
            "include_corr_pairlist": [], "label_period_candles": 6, "include_shifted_candles": 0,
            "indicator_periods_candles": [], "shuffle_after_split": False},
            "data_split_parameters": {"test_size": 0, "shuffle": False},
            "model_training_parameters": {"n_estimators": 100, "num_leaves": 15, "max_depth": 5,
            "min_child_samples": 40, "learning_rate": 0.05, "random_state": 42, "n_jobs": 2,
            "deterministic": True, "force_col_wise": True, "verbosity": -1}}


def collect_ml_evidence(job_dir, cfg):
    import math
    from datetime import timedelta, timezone
    if not (job_dir / "training-audit.json").exists() or not (job_dir / "predictions.csv").exists():
        raise ValueError("Missing successful ML training/prediction audit; inspect swallowed FreqAI failures")
    audits = json.loads((job_dir / "training-audit.json").read_text(encoding="utf-8"))
    start = datetime.fromisoformat(cfg["start"]).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(cfg["end"]).replace(tzinfo=timezone.utc)
    expected_fits = {(pair, (start + timedelta(days=30 * fold)).isoformat())
                     for pair in cfg["pairs"] for fold in range(math.ceil((end-start).days / 30))}
    if {(a["pair"], a["fit_cutoff"]) for a in audits} != expected_fits or len(audits) != len(expected_fits):
        raise ValueError("Missing or duplicate successful model fits; FreqAI may have skipped training")
    by_model = {(a["pair"], a["model_id"]): a for a in audits}
    rows = list(csv.DictReader((job_dir / "predictions.csv").open(encoding="utf-8")))
    expected_rows = int((end-start).total_seconds() / 3600) * len(cfg["pairs"])
    if len(rows) != expected_rows or len({(r["pair"], r["decision_at"]) for r in rows}) != expected_rows:
        raise ValueError("Incomplete or duplicate ML predictions")
    valid_count = 0
    for row in rows:
        decision, feature, fit, label = [datetime.fromisoformat(row[k]) for k in
                                       ("decision_at", "feature_available_at", "model_fit_at", "model_label_max_at")]
        audit = by_model[(row["pair"], row["model_id"])]
        if not (feature == decision and label <= fit < decision <= fit + timedelta(days=30) and start < decision <= end):
            raise ValueError("Prediction timestamp audit failed")
        if row["model_fit_at"] != audit["fit_cutoff"] or row["model_label_max_at"] != audit["model_label_max_at"]:
            raise ValueError("Prediction model provenance mismatch")
        if row["do_predict"] == "1":
            if not math.isfinite(float(row["prediction"])):
                raise ValueError("Nonfinite usable prediction")
            valid_count += 1
    if not valid_count:
        raise ValueError("No usable ML predictions")
    return {"model_class": "PurgedLightGBMRegressor", "lightgbm_version": importlib.metadata.version("lightgbm"),
            "protocol": {"horizon_candles": 6, "train_period_days": 90, "backtest_period_days": 30,
                         "entry_threshold": .003, "exit_threshold": 0, "seed": 42},
            "fit_count": len(audits), "prediction_count": len(rows), "valid_prediction_count": valid_count,
            "invalid_prediction_count": len(rows)-valid_count, "total_fit_seconds": sum(a["duration_seconds"] for a in audits),
            "audits": audits, "artifacts": {"training_audit": "training-audit.json", "predictions": "predictions.csv"},
            "timestamp_audit": "passed", "final_test": "reserved_not_run"}
