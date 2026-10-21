"""Immutable teaching-simulation audits. A pass is not a profit certification."""
import csv
import hashlib
import json
import math
import uuid
import zipfile
from datetime import date
from pathlib import Path

from . import settings, store
from .data import sha256
from .diagnostics import classify
from .engine import parse_result

POLICY = {"id": "teaching-simulation-v1", "mode": "teaching_simulation", "min_trades": 30,
          "min_days": 60, "max_drawdown": .10, "min_lookahead_signals": 20,
          "final_holdout_gating": False, "supported_strategy": "SmaTrend",
          "meaning": "Engineering gate for teaching simulation only; not profit certification or permission for real-money trading"}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def object_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def safe_file(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact path escapes its evidence directory")
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
        raise ValueError("Symlink evidence is not accepted")
    if not path.is_file():
        raise FileNotFoundError("Missing evidence: " + str(relative))
    return path


def load(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    # Recursive finite check also catches nonfinite nested balances and trades.
    canonical(value)
    return value


def audit(job):
    directory = settings.ARTIFACTS / job["id"]
    checks, files, diagnostics = [], {}, []
    versions = {"experiment_config_sha256": object_hash(job["config"]), "artifact_sha256": files}
    evidence = {"config": job["config"], "result": None, "provenance": None, "diagnostics": diagnostics}

    def add(name, status, detail, required=True):
        checks.append({"name": name, "status": status, "detail": detail, "required": required})

    def remember(relative):
        path = safe_file(directory, relative)
        files[str(relative).replace('\\', '/')] = sha256(path)
        return path

    def check(name, action):
        try:
            action()
            add(name, "passed", "Evidence verified")
        except (FileNotFoundError, KeyError) as exc:
            add(name, "unverified", str(exc))
        except (ValueError, TypeError, AttributeError, OSError, ArithmeticError, zipfile.BadZipFile) as exc:
            add(name, "failed", str(exc) or "Invalid evidence")

    def require(condition, detail):
        if not condition:
            raise ValueError(detail)

    add("source_job", "passed" if job["status"] == "succeeded" else "failed" if job["status"] == "failed" else "unverified", "Source experiment status: " + job["status"])
    strategy = job["config"].get("strategy")
    add("simulation_strategy", "passed" if strategy == "SmaTrend" else "failed",
        "SmaTrend supported" if strategy == "SmaTrend" else "FreqaiTrend blocked: original diagnostic was flagged and actual model is historical-only (train refuses self.live); no substituted strategy is permitted")
    result, provenance, manifest, effective = {}, {}, {}, {}

    def source_evidence():
        result.update(load(remember("result.json")))
        versions["result_sha256"] = files["result.json"]
        evidence["result"] = result
        require(result == job["result"], "Stored job result differs from result.json")
        provenance.update(load(remember("provenance.json")))
        evidence["provenance"] = provenance
        require(all(result["provenance"].get(k) == v for k, v in provenance.items()), "Result provenance differs from provenance snapshot")
        require(result["strategy"] == strategy, "Result strategy differs from source job")
        versions.update(engine_version=result["engine_version"], adapter_sha256=provenance.get("adapter_sha256"))
    check("source_evidence", source_evidence)

    def configuration():
        effective.update(load(remember("effective-config.json")))
        require(files["effective-config.json"] == provenance["config_sha256"], "Effective configuration hash mismatch")
        cfg = job["config"]
        require(effective["dry_run"] is True and effective["trading_mode"] == "spot" and effective["timeframe"] == "1h", "Engine configuration is not hourly spot simulation")
        require(effective["exchange"]["pair_whitelist"] == cfg["pairs"] and effective["stake_amount"] == cfg["stake"] and
                effective["dry_run_wallet"] == cfg["wallet"] and effective["fee"] == cfg["fee"], "Effective configuration does not match experiment")
        evidence["effective_config"] = effective
        versions["config_sha256"] = files["effective-config.json"]
    check("configuration_integrity", configuration)

    def strategy_integrity():
        require(strategy in {"SmaTrend", "FreqaiTrend"}, "Unsupported strategy")
        snapshot = remember("strategies/" + strategy + ".py")
        current = safe_file(settings.ROOT, "strategies/" + strategy + ".py")
        require(sha256(snapshot) == provenance["strategy_sha256"] == sha256(current), "Current strategy differs from the experiment snapshot")
        versions["strategy_sha256"] = sha256(snapshot)
        if provenance.get("protocol_sha256"):
            require(sha256(remember("research-protocol.json")) == provenance["protocol_sha256"], "Research protocol hash mismatch")
        if strategy == "FreqaiTrend":
            require(sha256(remember("models/PurgedLightGBMRegressor.py")) == provenance["model_sha256"], "Model code hash mismatch")
            versions["model_sha256"] = provenance["model_sha256"]
    check("strategy_integrity", strategy_integrity)

    def dataset_integrity():
        manifest.update(load(remember("manifest.json")))
        require(manifest["id"] == job["config"]["dataset_id"] == provenance["dataset_id"], "Dataset snapshot ID mismatch")
        digest = hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest()
        require(digest == manifest["sha256"] == provenance["data_sha256"], "Dataset manifest hash mismatch")
        selected = [f for f in manifest["files"] if f["pair"] in job["config"]["pairs"]]
        require(sorted(f["pair"] for f in selected) == sorted(job["config"]["pairs"]), "Missing or duplicate dataset pairs")
        for entry in selected:
            require(sha256(remember("market/" + Path(entry["path"]).name)) == entry["sha256"], "Market file hash mismatch")
        evidence["manifest"] = manifest
        versions.update(dataset_id=manifest["id"], dataset_sha256=digest, manifest_sha256=files["manifest.json"])
    check("dataset_integrity", dataset_integrity)

    def raw_integrity():
        archive = remember(result["provenance"]["raw_artifact"])
        require(sha256(archive) == result["provenance"]["raw_sha256"], "Raw engine archive hash mismatch")
        transformed = parse_result(archive, job["config"], provenance)
        for key in ("metrics", "trades", "equity", "period", "strategy"):
            require(transformed[key] == result[key], "Raw engine artifact does not reconcile with " + key)
        versions["raw_sha256"] = sha256(archive)
    check("raw_artifact_integrity", raw_integrity)

    def thresholds():
        metrics = result["metrics"]
        require(isinstance(metrics["trades"], int) and not isinstance(metrics["trades"], bool) and metrics["trades"] >= POLICY["min_trades"], "At least 30 trades required")
        period = result["period"]
        require(period["start"] == job["config"]["start"] and period["end"] == job["config"]["end"], "Result period differs from source configuration")
        require((date.fromisoformat(period["end"]) - date.fromisoformat(period["start"])).days >= POLICY["min_days"], "At least 60 days required")
        require(0 <= metrics["max_drawdown"] <= POLICY["max_drawdown"], "Drawdown must be within [0, 0.10]")
        profit = sum(t["profit_abs"] for t in result["trades"])
        require(len(result["trades"]) == metrics["trades"] and math.isclose(profit, metrics["profit_abs"], abs_tol=1e-5)
                and math.isclose(metrics["initial_balance"] + profit, metrics["final_balance"], abs_tol=1e-5), "Balances do not reconcile")
    check("performance_and_balances", thresholds)

    def lookahead():
        matching = []
        for path in sorted(settings.ARTIFACTS.glob("diagnostic-*/diagnostic.json")):
            try:
                report = load(safe_file(settings.ARTIFACTS, path.relative_to(settings.ARTIFACTS)))
            except (ValueError, OSError):
                continue
            if not isinstance(report, dict):
                continue
            if report.get("job_id") == job["id"] and report.get("source_result_sha256") == versions.get("result_sha256"):
                matching.append((path, report))
        if not matching:
            raise FileNotFoundError("No diagnostic bound to this experiment and result SHA")
        sufficient = False
        for path, report in matching:
            root = path.parent
            frozen = {"id": root.name, "report": report, "sha256": {"diagnostic.json": sha256(path)}}
            diagnostics.append(frozen)
            require(report.get("strategy") == strategy, "Diagnostic strategy mismatch")
            if report.get("status") == "flagged":
                raise ValueError("A matching lookahead diagnostic flagged bias")
            if report.get("status") != "passed_sampled" or report.get("exit_code") != 0:
                continue
            for filename in ("lookahead.csv", "lookahead.log", "strategies/" + strategy + ".py"):
                frozen["sha256"][filename] = sha256(safe_file(root, filename))
            require(frozen["sha256"]["lookahead.log"] == report["log_sha256"], "Diagnostic log hash mismatch")
            require(frozen["sha256"]["strategies/" + strategy + ".py"] == provenance["strategy_sha256"], "Diagnostic strategy snapshot mismatch")
            with safe_file(root, "lookahead.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            require(rows == report.get("rows"), "Diagnostic CSV differs from recorded rows")
            require(classify(rows) == "passed_sampled", "Raw diagnostic output did not pass")
            require(all(row.get("strategy") == strategy for row in rows), "Diagnostic row strategy mismatch")
            sufficient |= bool(rows) and all(int(row["total_signals"]) >= POLICY["min_lookahead_signals"] for row in rows)
        if not sufficient:
            raise FileNotFoundError("Successful lookahead with at least 20 sampled signals required")
    check("sampled_lookahead", lookahead)
    add("final_holdout", "unverified", "Final holdout is reserved and has not been verified; explicitly non-gating for this teaching simulation policy", False)
    required = [c["status"] for c in checks if c["required"]]
    status = "rejected" if "failed" in required else "insufficient_evidence" if "unverified" in required else "approved"
    return {"id": str(uuid.uuid4()), "experiment_id": job["id"], "created_at": store.now(), "status": status,
            "policy": dict(POLICY), "checks": checks, "versions": versions, "evidence": evidence}


def create_release(job):
    record = audit(job)
    with store.connect() as conn:
        conn.execute("INSERT INTO releases VALUES(?,?)", (record["id"], canonical(record)))
    return record


def get_release(release_id):
    with store.connect() as conn:
        row = conn.execute("SELECT record FROM releases WHERE id=?", (release_id,)).fetchone()
        return json.loads(row["record"]) if row else None


def verify_release(release_id):
    """Return (frozen release, source job), or ValueError; never mutates the audit.

    Deployment must call this before preparing/starting, then copy and verify the frozen
    strategy/config hashes again to close any external filesystem mutation window.
    """
    release = get_release(release_id)
    if not release or release["status"] != "approved":
        raise ValueError("An approved teaching-simulation release is required")
    job = store.get(release["experiment_id"])
    if not job:
        raise ValueError("Source experiment is missing")
    current = audit(job)
    if current["status"] != "approved" or current["versions"] != release["versions"]:
        raise ValueError("Release source or strategy changed; create a new audit")
    for diagnostic in release["evidence"]["diagnostics"]:
        for filename, digest in diagnostic["sha256"].items():
            try:
                path = safe_file(settings.ARTIFACTS / diagnostic["id"], filename)
                if sha256(path) != digest:
                    raise ValueError("Frozen diagnostic evidence changed")
            except OSError as exc:
                raise ValueError("Frozen diagnostic evidence missing") from exc
    return release, job
