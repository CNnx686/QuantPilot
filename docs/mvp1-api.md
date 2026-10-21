# MVP1 read-only API contract

`GET /api/experiments/{id}/report` returns:
- `schema_version: "1"`, `generated_at: UTC ISO string`
- `experiment: {id, name, config, created_at}`
- `metrics`: original result metrics (undefined values stay null)
- `research: [{id, status, created_at, experiment_id, revision, source}]` (only records bound to this experiment)
- `releases: [{id, status, created_at, experiment_id, checks, policy}]` (saved decisions, not fresh approvals)
- `diagnostic_status`, `diagnostics`: result-hash-bound original replay evidence
- `provenance`: replay provenance plus `result_read_sha256`, `assumptions`, `period`
- `limitations: string[]`, including final holdout unverified and historical-only constraints
- `trades: [{...original trade fields, id: "trade:0", original_index: 0}]` in original artifact order; timestamps unchanged
- `narrative: string[]`: deterministic Chinese factual summary with no LLM or inferred decisions

`GET /api/experiments/{id}/report/download?format=html|json` defaults to HTML. Both use attachment Content-Disposition. HTML is self-contained UTF-8, escaped, printable with browser Print, and includes every trade, provenance, assumptions, linked evidence and limitations. JSON has the report shape above. Unknown experiment 404; unsupported format 422; unsuccessful/missing/corrupt/inconsistent evidence 409. Downloads are regenerated and generated_at is read time; narrative is deterministic.

`GET /api/system` and `python -m quantpilot.doctor --json` return the same shape:
- `version: "0.4.0-mvp1"`, `python_version`, `engine_version: string|null`, `generated_at`
- `frontend: {built: bool}`
- `dataset: {available: bool, metadata: object|null}` (public manifest summary)
- `database: {exists: bool, readable: bool}`
- `worker: {alive: bool, heartbeat_at: string|null}` (saved lease only)
- `llm: {configured: bool, optional: true}` (no values or request)
- `saved_counts: {experiments: number, research: number, releases: number}`
- `deployments: {live_verified: false, database_readable: bool, records: [{id, release_id, state, created_at}]}` (persisted state only, no RPC)
- `checklist: [{id, status: "ok"|"warning"|"error", detail, action: string|null}]`
- `core_ready: bool`: engine version 2026.9, built frontend, available dataset, readable DB and fresh worker lease. Optional LLM never affects this.

Checks are read-only, never create SQLite or runtime directories, never call an engine, exchange, RPC or LLM. CLI exits 0 when core_ready, 1 otherwise. Missing deployment DB is an empty persisted list and a warning, not a live status assertion.
