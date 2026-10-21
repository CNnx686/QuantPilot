"""Verified Binance public archives -> immutable hourly Freqtrade data snapshot."""
from __future__ import annotations

import hashlib
import io
import json
import urllib.request
import zipfile
import argparse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "manifests" / "binance-2025q1.json"
WEEK2_MANIFEST = ROOT / "data" / "manifests" / "binance-week2.json"
PAIRS = ["BTC/USDT", "ETH/USDT"]
MONTHS = ["2024-12", "2025-01", "2025-02", "2025-03"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get_dataset() -> dict | None:
    path = WEEK2_MANIFEST if WEEK2_MANIFEST.exists() else MANIFEST
    if not path.exists():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    result["available"] = all((ROOT / f["path"]).is_file() for f in result["files"])
    return result


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "QuantPilot-Education/0.1"})
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.read()


def prepare(week2: bool = False) -> dict:
    import pandas as pd
    import numpy as np
    manifest_path = WEEK2_MANIFEST if week2 else MANIFEST
    start, end = ("2023-12-01", "2025-10-01") if week2 else ("2024-12-01", "2025-04-01")
    months = pd.date_range(start, end, freq="MS", inclusive="left").strftime("%Y-%m").tolist()
    existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    if existing and all((ROOT / f["path"]).is_file() and sha256(ROOT / f["path"]) == f["sha256"] for f in existing["files"]):
        print("Verified existing dataset:", existing["id"])
        return existing
    raw_dir = ROOT / "data" / "downloads"
    market_dir = ROOT / "data" / "market" / ("binance-week2" if week2 else "binance")
    raw_dir.mkdir(parents=True, exist_ok=True)
    market_dir.mkdir(parents=True, exist_ok=True)
    sources, files = [], []
    for pair in PAIRS:
        frames = []
        for month in months:
            name = f"{pair.replace('/', '')}-1h-{month}.zip"
            url = f"https://data.binance.vision/data/spot/monthly/klines/{pair.replace('/', '')}/1h/{name}"
            print("Fetching", name, flush=True)
            cached = raw_dir / name
            checksum = raw_dir / (name + ".CHECKSUM")
            payload = cached.read_bytes() if cached.exists() else fetch(url)
            checksum_payload = checksum.read_bytes() if checksum.exists() else fetch(url + ".CHECKSUM")
            expected = checksum_payload.decode().split()[0]
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise ValueError(f"Archive checksum mismatch: {name}")
            (raw_dir / name).write_bytes(payload)
            checksum.write_bytes(checksum_payload)
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                frame = pd.read_csv(zf.open(name.replace(".zip", ".csv")), header=None)
            timestamp = pd.to_numeric(frame[0])
            # Binance changed spot archive timestamps to microseconds on 2025-01-01.
            milliseconds = timestamp.where(timestamp < 10**14, timestamp // 1000)
            normalized = pd.DataFrame({"date": pd.to_datetime(milliseconds, unit="ms", utc=True)})
            for index, column in enumerate(["open", "high", "low", "close", "volume"], 1):
                normalized[column] = pd.to_numeric(frame[index]).astype(float)
            frames.append(normalized)
            sources.append({"pair": pair, "url": url, "checksum_url": url + ".CHECKSUM", "sha256": actual})
        data = pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)
        expected_dates = pd.date_range(start, end, freq="h", inclusive="left", tz="UTC")
        duplicates = int(data["date"].duplicated().sum())
        missing = len(expected_dates.difference(pd.DatetimeIndex(data["date"])))
        numeric = data[["open", "high", "low", "close", "volume"]]
        invalid = int((~np.isfinite(numeric).all(axis=1) | (data["low"] > data[["open", "close"]].min(axis=1)) | (data["high"] < data[["open", "close"]].max(axis=1)) | (data["low"] <= 0) | (data["volume"] < 0)).sum())
        if duplicates or missing or invalid or len(data) != len(expected_dates):
            raise ValueError(f"Data quality failed for {pair}: duplicates={duplicates},missing={missing},invalid={invalid}")
        path = market_dir / f"{pair.replace('/', '_')}-1h.feather"
        data.to_feather(path)
        files.append({"pair": pair, "path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path), "rows": len(data), "missing": missing, "duplicates": duplicates, "invalid": invalid})
    snapshot_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest = {"id": "binance-2025q1-" + snapshot_hash[:12], "source": "Binance public historical archives", "exchange": "binance", "timeframe": "1h", "pairs": PAIRS, "start": "2024-12-01", "end": "2025-04-01", "available_start": "2025-01-01", "available_end": "2025-04-01", "timezone": "UTC", "timestamp_meaning": "candle open; OHLCV available after one hour", "retrieved_at": datetime.now(timezone.utc).isoformat(), "sha256": snapshot_hash, "files": files, "sources": sources, "quality": {"status": "passed", "warmup_hours": 744, "missing": 0, "duplicates": 0, "invalid": 0}, "usage": "development baseline only; not an untouched final test", "available": True}
    if week2:
        manifest.update(id="binance-week2-" + snapshot_hash[:12], start=start, end=end,
                        available_start="2025-01-01", available_end=end,
                        usage="development and validation only; final 2025Q4 reserved and not downloaded",
                        split_protocol="week2-v1")
        manifest["quality"]["warmup_hours"] = 24 * 90
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Prepared", manifest["id"], flush=True)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--week2", action="store_true")
    prepare(parser.parse_args().week2)
