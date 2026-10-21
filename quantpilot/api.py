"""Local-only API for persisted historical research experiments."""
import hashlib
import importlib.metadata
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import settings, store
from .data import get_dataset

app = FastAPI(title="QuantPilot", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Do not echo raw NaN/Infinity inputs into a JSON response (or submitted values).
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]})


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(default="趋势基线研究", min_length=1, max_length=100)
    pairs: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"], min_length=1, max_length=2)
    start: date = date(2025, 1, 1)
    end: date = date(2025, 4, 1)
    wallet: float = Field(default=10000, gt=0, le=1e9)
    stake: float = Field(default=1000, gt=0, le=1e9)
    fee: float = Field(default=0.001, ge=0, le=0.1)
    strategy: str = "SmaTrend"
    dataset_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_research(self):
        if self.strategy not in {"SmaTrend", "FreqaiTrend"}:
            raise ValueError("Select SmaTrend or FreqaiTrend")
        if self.strategy == "FreqaiTrend" and (self.start < date(2025, 4, 1) or self.end > date(2025, 10, 1)):
            raise ValueError("FreqAI uses development/validation [2025-04-01, 2025-10-01); final test is reserved")
        if len(set(self.pairs)) != len(self.pairs) or any(p not in {"BTC/USDT", "ETH/USDT"} for p in self.pairs):
            raise ValueError("Select unique BTC/USDT and/or ETH/USDT pairs")
        if not 0 < (self.end - self.start).days <= 366:
            raise ValueError("end must follow start by at most 366 days (end is exclusive)")
        if self.stake > self.wallet:
            raise ValueError("stake must not exceed wallet")
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name must not be blank")
        return self


@app.middleware("http")
async def local_writes(request: Request, call_next):
    # Restrict Host as well as Origin to protect a localhost server from DNS rebinding.
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
        return JSONResponse({"detail": "Local host required"}, status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                return JSONResponse({"detail": "Only localhost origins may submit experiments"}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def health():
    try:
        version = importlib.metadata.version("freqtrade")
    except importlib.metadata.PackageNotFoundError:
        version = None
    dataset = get_dataset()
    return {"status": "ok", "engine_version": version, "worker": store.worker_status(),
            "dataset": {"available": bool(dataset and dataset.get("available", True)), "id": dataset.get("id") if dataset else None}}


@app.get("/api/dataset")
def dataset():
    return get_dataset()


@app.get("/api/experiments")
def experiments():
    return store.list_jobs()


@app.get("/api/comparison")
def comparison(ids: str):
    from .evaluation import compare
    selected = ids.split(",")
    if len(selected) != 2 or len(set(selected)) != 2:
        raise HTTPException(422, "Select two distinct experiment IDs")
    return compare([require_job(job_id) for job_id in selected])


@app.post("/api/experiments")
def create_experiment(body: ExperimentInput, response: Response,
                      idempotency_key: str = Header(min_length=1, max_length=100)):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", idempotency_key):
        raise HTTPException(422, "Idempotency-Key must contain only letters, digits, dot, underscore, colon, or hyphen")
    config = body.model_dump(mode="json", exclude={"name"}, exclude_none=True)
    try:
        existing = store.find_submission(body.name, config, idempotency_key, infer_dataset=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if existing:
        return existing
    manifest = get_dataset()
    if not manifest or not manifest.get("available", True):
        raise HTTPException(409, "Historical dataset is unavailable; run the data preparation command first")
    if body.dataset_id and body.dataset_id != manifest["id"]:
        raise HTTPException(409, "Dataset changed; refresh and confirm the current snapshot")
    config["dataset_id"] = manifest["id"]
    lower = manifest.get("available_start", manifest["start"])
    upper = manifest.get("available_end", manifest["end"])
    if body.start < date.fromisoformat(lower) or body.end > date.fromisoformat(upper):
        raise HTTPException(422, f"Requested dates must be within [{lower}, {upper}) including warmup requirements")
    try:
        job, created = store.enqueue(body.name, config, idempotency_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    response.status_code = 201 if created else 200
    return job


def require_job(job_id):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Experiment not found")
    return job


@app.get("/api/experiments/{job_id}")
def experiment(job_id: str):
    return require_job(job_id)


def artifact_files(job_id):
    require_job(job_id)
    directory = settings.ARTIFACTS / job_id
    if not directory.exists():
        return {}
    allowed = {"effective-config.json", "manifest.json", "provenance.json", "result.json", "engine.log", "training-audit.json", "predictions.csv", "research-protocol.json", "lookahead.log", "lookahead.csv", "evaluation.json"}
    return {p.name: p for p in directory.iterdir()
            if p.is_file() and not p.is_symlink() and (p.name in allowed or p.name.endswith(".zip"))}


@app.get("/api/experiments/{job_id}/logs")
def logs(job_id: str):
    path = artifact_files(job_id).get("engine.log")
    if not path:
        return {"text": ""}
    # Bound response size while retaining recent engine diagnostics.
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 256_000))
        return {"text": handle.read().decode("utf-8", errors="replace")}


def diagnostic_reports(job_id: str):
    import json
    require_job(job_id)
    source = settings.ARTIFACTS / job_id / "result.json"
    if not source.is_file():
        return []
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    reports = []
    for path in settings.ARTIFACTS.glob("diagnostic-*/diagnostic.json"):
        if path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if report.get("job_id") == job_id and report.get("source_result_sha256") == digest:
            report["id"] = path.parent.name
            report["files"] = [{"name": name, "url": f"/api/experiments/{job_id}/diagnostics/{path.parent.name}/{name}"}
                               for name in ("diagnostic.json", "lookahead.csv", "lookahead.log", "boundary-audit.json") if (path.parent / name).is_file()]
            reports.append(report)
    return reports


@app.get("/api/experiments/{job_id}/diagnostics")
def diagnostics(job_id: str):
    return diagnostic_reports(job_id)


@app.get("/api/experiments/{job_id}/diagnostics/{diagnostic_id}/{filename}")
def diagnostic_artifact(job_id: str, diagnostic_id: str, filename: str):
    if filename not in {"diagnostic.json", "lookahead.csv", "lookahead.log", "boundary-audit.json"}:
        raise HTTPException(404, "Diagnostic file not found")
    if not any(r["id"] == diagnostic_id for r in diagnostic_reports(job_id)):
        raise HTTPException(404, "Diagnostic not linked to this experiment")
    path = settings.ARTIFACTS / diagnostic_id / filename
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "Diagnostic file not found")
    return FileResponse(path, filename=filename)


@app.get("/api/experiments/{job_id}/artifacts")
def artifacts(job_id: str):
    entries = []
    for name, path in sorted(artifact_files(job_id).items()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append({"name": name, "size": path.stat().st_size, "sha256": digest.hexdigest(),
                        "url": f"/api/experiments/{job_id}/artifacts/{name}"})
    return entries


@app.get("/api/experiments/{job_id}/artifacts/{filename}")
def artifact(job_id: str, filename: str):
    path = artifact_files(job_id).get(filename)
    if path is None:
        raise HTTPException(404, "Artifact not found")
    return FileResponse(path, filename=filename)


from .workflow_api import create_router

app.include_router(create_router(lambda: get_dataset()))


@app.get("/{path:path}")
def spa(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "API route not found")
    dist = (settings.ROOT / "apps" / "web" / "dist").resolve()
    candidate = (dist / path).resolve()
    if candidate.is_relative_to(dist) and candidate.is_file():
        return FileResponse(candidate)
    if (dist / "index.html").is_file():
        return FileResponse(dist / "index.html")
    raise HTTPException(404, "Build the frontend first: cd apps/web && npm run build")
