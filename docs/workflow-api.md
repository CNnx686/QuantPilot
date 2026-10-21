# Research and immutable release API

All writes inherit the API's localhost Host/Origin restrictions. No request starts an
exchange connection. Research confirmation only adds an existing worker queue job.

## ResearchSpec

Fields: `schema_version` (`"1"`), `timeframe` (`"1h"`), `name`, `hypothesis`,
`pairs` (unique BTC/USDT and/or ETH/USDT), `strategy` (SmaTrend or FreqaiTrend),
`start`, `end`, `wallet`, `stake`, `fee`, and required `dataset_id`.
Dates are ISO UTC dates, half-open, within `[2025-04-01,2025-10-01)`.
Defaults are April 1–July 1, both pairs, SmaTrend, wallet 10000, stake 1000,
fee 0.001. Numeric strings, booleans as numbers, nonfinite values, extra fields,
arbitrary strategy/code, and unsupported markets/timeframes fail with 422.
The queue receives the existing ExperimentInput configuration semantics, with
research metadata removed and the exact reviewed dataset ID preserved.

| Route | Contract |
| --- | --- |
| GET `/api/research/templates` | Supported templates, splits, and `online_llm: {configured,model,reason}`; never a key or provider response |
| POST `/api/research/suggest` | `{prompt,provider?:"local_template"|"llm"}` → `{source,spec,suggestion_id,limitations}`; local is default |
| GET `/api/research` | Records newest first |
| POST `/api/research` | `{spec,source?:"manual"|"local_template"|"llm",prompt?:string,suggestion_id?:string}` → draft, 201 |
| GET `/api/research/{id}` | Full saved record, or 404 |
| PUT `/api/research/{id}` | Create-body fields plus required `revision`; returns edited record with incremented revision |
| POST `/api/research/{id}/confirm` | `{revision,spec}` must match the saved normalized spec exactly → `{research,experiment}`, first 201, replay 200 |

A record contains `id`, `spec`, `source`, `prompt`, `suggestion_id`, `generator`,
`status` (`draft`/`confirmed`), `revision`, `experiment_id`, `created_at`, and
`confirmed_at`. Confirmation and queue insertion share one SQLite transaction;
concurrent/repeated matching confirmations return the same experiment. A changed
snapshot, stale revision, or edit of a confirmed record returns 409. A matching
replay remains available after the current dataset disappears. Confirmed records
also have database update/delete guards.

Local suggestions use a conservative supported keyword grammar and are explicitly
`source=local_template`, never described as LLM output. Unsupported asks return
422. Online output undergoes the same strict schema validation. A saved
`source=llm` or `source=local_template` requires the exact server-issued suggestion and its ID; edited output
must use `source=manual`, while retaining `suggestion_id` preserves generator
provenance. Original generated configuration and prompt remain server-side.

Online configuration comes from three allowlisted entries in `$QUANTPILOT_HOME/.env`
(project root by default), with nonempty process variables taking precedence:
`QUANTPILOT_LLM_BASE_URL`, `QUANTPILOT_LLM_MODEL`, `QUANTPILOT_LLM_API_KEY`.
The file is parsed as plain text, without interpolation or execution. See
`.env.example`. Only an explicit `provider=llm` request calls the compatible
`/chat/completions` endpoint. Requests use a 20-second total async deadline (including
headers and streaming), a 20-second HTTP timeout, 1800 output tokens, and a 64 KiB
response limit. Missing configuration returns 409; upstream
failures return a sanitized 502; unsupported/malformed configuration returns 422.

## Releases

POST `/api/releases` takes only `{experiment_id}` and returns a new immutable audit
(201). Repeating the request creates a new independent audit; it never overwrites
an earlier decision. GET `/api/releases` lists audits; GET `/api/releases/{id}`
retrieves one. There is no edit/delete API and database triggers forbid either.

An audit contains `id`, `experiment_id`, `created_at`, `status`, `policy`, `checks`,
`versions`, and `evidence`. Status is `rejected` for a failed required check,
otherwise `insufficient_evidence` for an unverified required check, otherwise
`approved`. Each check has `name,status,detail,required`.

The frozen teaching-simulation-v1 policy requires a succeeded source job, at least
30 trades and 60 days, drawdown in [0,0.10], finite and reconciled balances, raw
archive reconciliation, effective-config/provenance/dataset/strategy integrity,
current strategy matching the run snapshot, and a successful sampled lookahead
with at least 20 signals bound to this exact experiment and result SHA. Diagnostic
CSV, log hash, and strategy snapshot are checked. FreqaiTrend is explicitly blocked
because the original diagnostic flagged it and the historical-only model refuses
live training; no strategy substitution is allowed. Final holdout remains
`unverified`, explicitly non-gating. Approval is for teaching simulation and is
not a profit certification or permission for real-money trading.

`evidence` freezes source `config,result,provenance,manifest,effective_config` and
diagnostic reports/hashes. `versions` records `experiment_config_sha256`,
`result_sha256`, `strategy_sha256`, `config_sha256`, `dataset_id`, `dataset_sha256`,
`manifest_sha256`, `raw_sha256`, `engine_version`, `adapter_sha256`, optional model
hash, and `artifact_sha256` (relative path → SHA-256). Missing evidence can leave
some fields absent. The old manifest format did not record its original byte
hash; the initial audit validates its content-addressed file list and dataset ID,
then freezes its byte hash for subsequent checks.

For deployment code, `quantpilot.releases.verify_release(id)` returns
`(frozen_release,source_job)` or raises `ValueError`. It re-audits current source
and strategy, compares frozen versions, and verifies frozen diagnostic hashes.
Use `release["evidence"]["config"]` as the deployment configuration source.
Call verification before preparing/starting, then verify the copied strategy and
configuration against frozen hashes to close the external-file mutation window.
Verification never updates an existing release. Original experiment artifacts
are read-only throughout this workflow.
