"""Research workflow routes, created with explicit dependencies to avoid circular imports."""
import json
import uuid
import zipfile
from datetime import date

from fastapi import APIRouter, HTTPException, Response, Header
from pydantic import BaseModel, ConfigDict, Field

from . import store, llm
from .research import ConfirmInput, DraftInput, EditInput, SuggestInput, suggest
from .releases import create_release


class ReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    experiment_id: str = Field(min_length=1, max_length=100)


class DeploymentInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    release_id: str = Field(min_length=1, max_length=100)


def dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def read_record(conn, table, record_id):
    # Callers supply only literal table names, never request values.
    row = conn.execute(f"SELECT record FROM {table} WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Record not found")
    return json.loads(row["record"])


def create_router(dataset_provider):
    router = APIRouter(prefix="/api")

    @router.get("/research/templates")
    def templates():
        return {"schema_version": "1", "strategies": ["SmaTrend", "FreqaiTrend"],
                "pairs": ["BTC/USDT", "ETH/USDT"], "timeframe": "1h",
                "development": ["2025-04-01", "2025-07-01"], "validation": ["2025-07-01", "2025-10-01"],
                "source": "local_template", "online_llm": llm.metadata(),
                "example_prompts": ["BTC SMA trend baseline", "BTC ETH FreqAI validation"]}

    @router.post("/research/suggest")
    def suggestion(body: SuggestInput):
        manifest = dataset_provider()
        if not manifest or not manifest.get("available", True):
            raise HTTPException(409, "Historical dataset unavailable")
        try:
            if body.provider == "llm":
                spec, generator = llm.suggest(body.prompt, manifest["id"])
            else:
                spec = suggest(body.prompt, manifest["id"])
                generator = {"provider": "local_template", "model": None}
        except llm.ProviderUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc
        except llm.ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        suggestion_id = str(uuid.uuid4())
        record = {"id": suggestion_id, "spec": spec.model_dump(mode="json"), "generator": generator,
                  "prompt": body.prompt, "created_at": store.now()}
        with store.connect() as conn:
            conn.execute("INSERT INTO suggestions VALUES(?,?)", (suggestion_id, dumps(record)))
        return {"source": body.provider, "spec": record["spec"], "suggestion_id": suggestion_id,
                "limitations": ["Deterministic local template selection; no online LLM" if body.provider == "local_template" else "Online model suggestion; validate assumptions before confirmation",
                                "Review dates, costs and hypothesis before confirmation", "No arbitrary factors, code or performance guarantees"]}

    def generator_for(body, conn):
        if not body.suggestion_id:
            if body.source != "manual":
                raise HTTPException(422, "Generated provenance requires a server-issued suggestion_id")
            return None
        generated = read_record(conn, "suggestions", body.suggestion_id)
        if body.source != "manual" and (body.source != generated["generator"]["provider"] or body.spec.model_dump(mode="json") != generated["spec"]):
            raise HTTPException(422, "Edited suggestions must use source=manual; generated provenance cannot be forged")
        return {**generated["generator"], "suggestion_id": body.suggestion_id, "created_at": generated["created_at"]}

    @router.get("/research")
    def list_research():
        with store.connect() as conn:
            return [json.loads(row["record"]) for row in conn.execute("SELECT record FROM research ORDER BY rowid DESC")]

    @router.post("/research", status_code=201)
    def create_research(body: DraftInput):
        record = {"id": str(uuid.uuid4()), **body.model_dump(mode="json"), "status": "draft", "revision": 1,
                  "experiment_id": None, "created_at": store.now(), "confirmed_at": None}
        with store.connect() as conn:
            record["generator"] = generator_for(body, conn)
            conn.execute("INSERT INTO research VALUES(?,?)", (record["id"], dumps(record)))
        return record

    @router.get("/research/{research_id}")
    def get_research(research_id: str):
        with store.connect() as conn:
            return read_record(conn, "research", research_id)

    @router.put("/research/{research_id}")
    def edit_research(research_id: str, body: EditInput):
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = read_record(conn, "research", research_id)
            if record["status"] != "draft" or body.revision != record["revision"]:
                raise HTTPException(409, "Research confirmed or revision changed")
            record["generator"] = generator_for(body, conn) if body.suggestion_id else record.get("generator")
            if body.source != "manual" and not body.suggestion_id:
                raise HTTPException(422, "Generated provenance requires a server-issued suggestion_id")
            record.update(body.model_dump(mode="json", exclude={"revision"}))
            record["revision"] += 1
            conn.execute("UPDATE research SET record=? WHERE id=?", (dumps(record), research_id))
        return record

    @router.post("/research/{research_id}/confirm")
    def confirm_research(research_id: str, body: ConfirmInput, response: Response):
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = read_record(conn, "research", research_id)
            if body.revision != record["revision"] or body.spec.model_dump(mode="json") != record["spec"]:
                raise HTTPException(409, "Confirmation must exactly match the saved revision and configuration")
            if record["status"] == "confirmed":
                job = store.decode(conn.execute("SELECT * FROM experiments WHERE id=?", (record["experiment_id"],)).fetchone())
                return {"research": record, "experiment": job}
            manifest = dataset_provider()
            if not manifest or not manifest.get("available", True) or manifest["id"] != body.spec.dataset_id:
                raise HTTPException(409, "Dataset unavailable or changed; refresh and review the draft")
            lower = manifest.get("available_start", manifest.get("start"))
            upper = manifest.get("available_end", manifest.get("end"))
            if body.spec.start < date.fromisoformat(lower) or body.spec.end > date.fromisoformat(upper):
                raise HTTPException(422, "Requested dates outside available dataset")
            job_id = str(uuid.uuid4())
            # Use the same experiment schema and serialization as store.enqueue, in this transaction.
            conn.execute("INSERT INTO experiments(id,idempotency_key,name,config,status,created_at) VALUES(?,?,?,?,?,?)",
                         (job_id, "research:" + research_id, body.spec.name,
                          json.dumps(body.spec.experiment_config(), sort_keys=True, allow_nan=False), "queued", store.now()))
            record.update(status="confirmed", experiment_id=job_id, confirmed_at=store.now())
            conn.execute("UPDATE research SET record=? WHERE id=?", (dumps(record), research_id))
            job = store.decode(conn.execute("SELECT * FROM experiments WHERE id=?", (job_id,)).fetchone())
            response.status_code = 201
            return {"research": record, "experiment": job}

    @router.get("/releases")
    def releases():
        with store.connect() as conn:
            return [json.loads(row["record"]) for row in conn.execute("SELECT record FROM releases ORDER BY rowid DESC")]

    @router.get("/releases/{release_id}")
    def release_detail(release_id: str):
        with store.connect() as conn:
            return read_record(conn, "releases", release_id)

    @router.post("/releases", status_code=201)
    def release_create(body: ReleaseInput):
        job = store.get(body.experiment_id)
        if not job:
            raise HTTPException(404, "Experiment not found")
        return create_release(job)

    from . import deployments, deployment_rpc, replay

    def simulation_call(function, *args):
        try:
            return function(*args)
        except KeyError:
            raise HTTPException(404, 'Simulation record or evidence not found') from None
        except deployment_rpc.Unavailable:
            raise HTTPException(503, 'Engine RPC unavailable') from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except (OSError, TypeError, AttributeError, zipfile.BadZipFile):
            raise HTTPException(409, 'Simulation evidence unavailable or invalid') from None

    @router.post('/deployments', status_code=201)
    def deployment_create(body: DeploymentInput, response: Response,
                          idempotency_key: str = Header(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9._:-]+$')):
        record, created = simulation_call(deployments.create, body.release_id, idempotency_key)
        response.status_code = 201 if created else 200
        return record

    @router.get('/deployments')
    def deployment_list():
        return simulation_call(deployments.list_deployments)

    @router.get('/deployments/{identifier}')
    def deployment_detail(identifier: str):
        return simulation_call(deployments.get, identifier)

    @router.post('/deployments/{identifier}/{operation}')
    def deployment_action(identifier: str, operation: str):
        if operation not in {'pause', 'resume', 'stop'}:
            raise HTTPException(404, 'Unknown deployment operation')
        return simulation_call(deployments.action, identifier, operation)

    @router.get('/experiments/{identifier}/replay')
    def experiment_replay(identifier: str):
        job = store.get(identifier)
        if not job:
            raise HTTPException(404, 'Experiment not found')
        return simulation_call(replay.build, job)

    return router
