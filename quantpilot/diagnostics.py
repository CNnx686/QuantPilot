"""Run upstream lookahead checks in a disposable *new* workspace, preserving runs."""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from . import settings, store
from .data import sha256
from .engine import write_json, timerange_argument


def classify(rows: list[dict]) -> str:
    if not rows:
        return "unverified"
    for row in rows:
        try:
            if int(row["total_signals"]) < 5:
                return "unverified"
            if row["has_bias"].lower() not in {"true", "false", "yes", "no", "1", "0"}:
                return "unverified"
            int(row["biased_entry_signals"])
            int(row["biased_exit_signals"])
        except (ValueError, KeyError, TypeError):
            return "unverified"
    flagged = any(r["has_bias"].lower() in {"true", "yes", "1"} or int(r["biased_entry_signals"]) or int(r["biased_exit_signals"]) or r.get("biased_indicators", "").strip() for r in rows)
    return "flagged" if flagged else "passed_sampled"


def run(job_id: str, signals: int = 20) -> dict:
    job = store.get(job_id)
    if not job or job["status"] != "succeeded":
        raise ValueError("Select a completed real experiment")
    source = settings.ARTIFACTS / job_id
    target = (settings.ARTIFACTS / ("diagnostic-" + str(uuid.uuid4()))).resolve()
    if not target.is_relative_to(settings.ARTIFACTS.resolve()):
        raise ValueError("Diagnostic workspace outside artifacts")
    target.mkdir()
    for folder in ("market", "strategies", "models"):
        if (source / folder).exists():
            shutil.copytree(source / folder, target / folder)
    (target / "userdata").mkdir()
    config = json.loads((source / "effective-config.json").read_text(encoding="utf-8"))
    config.update(user_data_dir=str(target / "userdata"), datadir=str(target / "market"))
    if config.get("freqai", {}).get("enabled"):
        # Upstream deletes/retrains its identifier cache for diagnostic slices.
        # This path resolves only inside the brand-new diagnostic workspace.
        config["freqai"]["identifier"] = target.name
    write_json(target / "effective-config.json", config)
    cfg = job["config"]
    command = [sys.executable, "-m", "freqtrade", "lookahead-analysis", "--config", str(target / "effective-config.json"),
               "--strategy", cfg["strategy"], "--strategy-path", str(target / "strategies"),
               "--timerange", timerange_argument(cfg["start"], cfg["end"]), "--datadir", str(target / "market"),
               "--data-format-ohlcv", "feather", "--minimum-trade-amount", "5", "--targeted-trade-amount", str(signals),
               "--lookahead-analysis-exportfilename", str(target / "lookahead.csv"), "--no-color"]
    if cfg["strategy"] == "FreqaiTrend":
        adapter = target / 'lookahead_runner.py'
        shutil.copy2(Path(__file__).with_name('lookahead_runner.py'), adapter)
        command[1:3] = [str(adapter)]
        command += ["--freqaimodel", "PurgedLightGBMRegressor", "--freqaimodel-path", str(target / "models")]
    report = {"job_id": job_id, "strategy": cfg["strategy"], "source_result_sha256": sha256(source / "result.json"),
              "path": str(target), "command": command, "status": "unverified",
              "scope": "Sampled upstream lookahead check only; intentional FreqAI target columns can be flagged. Raw output preserved. Does not prove universal absence of leakage."}
    if cfg['strategy'] == 'FreqaiTrend':
        report.update(comparison_policy='freqai-half-open-v1', adapter_sha256=sha256(adapter),
                      scope='Freqtrade 2026.9 抽样交易检查，并比较预测结束时刻之前的全部指标列；额外执行 K 线仍参与交易检查。边界原始差异保存在 comparison-boundaries.jsonl，没有忽略任何指标列。通过仅限本次抽样，不证明普遍无前视偏差。')
    write_json(target / "diagnostic.json", report)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FREQTRADE__")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with (target / "lookahead.log").open("w", encoding="utf-8") as output:
        try:
            process = subprocess.run(command, cwd=settings.ROOT, stdout=output, stderr=subprocess.STDOUT, env=env, timeout=1200, shell=False)
            report["exit_code"] = process.returncode
            if process.returncode == 0 and (target / "lookahead.csv").exists():
                with (target / "lookahead.csv").open(encoding="utf-8") as table:
                    rows = list(csv.DictReader(table))
                report["rows"] = rows
                report["status"] = classify(rows)
                if cfg['strategy'] == 'FreqaiTrend':
                    boundary_file = target / 'comparison-boundaries.jsonl'
                    evidence = [json.loads(line) for line in boundary_file.read_text(encoding='utf-8').splitlines()] if boundary_file.exists() else []
                    expected = 2 * sum(int(row['total_signals']) for row in rows)
                    report['boundary_comparisons'] = len(evidence)
                    report['raw_difference_comparisons'] = sum(bool(e['raw_difference_columns']) for e in evidence)
                    report['in_window_difference_rows'] = sum(e['in_window_difference_rows'] for e in evidence)
                    if not evidence or len(evidence) != expected:
                        report['status'] = 'unverified'
                        report['error'] = 'Incomplete comparison-boundary evidence'
                    elif report['in_window_difference_rows']:
                        report['status'] = 'flagged'
                    if boundary_file.exists():
                        report['boundary_evidence_sha256'] = sha256(boundary_file)
        except subprocess.TimeoutExpired:
            report["error"] = "Diagnostic exceeded 1200 seconds"
    report["log_sha256"] = sha256(target / "lookahead.log")
    write_json(target / "diagnostic.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--signals", type=int, default=20, choices=range(5, 101))
    args = parser.parse_args()
    print(json.dumps(run(args.job_id, args.signals), indent=2))
