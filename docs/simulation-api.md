# Simulation API

All endpoints are local `/api` endpoints. Dry-run is current public-market simulation; replay is historical and read-only.

- `POST /deployments`, JSON `{ "release_id": "..." }`, required `Idempotency-Key`: returns deployment, HTTP 201 (200 for exact retry). Only approved SmaTrend releases. One active deployment globally; conflicting token or active deployment returns 409. Creation starts **paused**, never enables entries automatically.
- `GET /deployments` and `GET /deployments/{id}` reconcile persisted engine ownership and return public deployment records.
- `POST /deployments/{id}/pause`, `/resume`, `/stop`: pause prevents new entries while managing positions; resume explicitly enables entries and requires fresh readiness; stop terminates the owned process without forcing positions closed. Saved simulated positions remain in its private DB.
- `GET /experiments/{id}/replay`: completed experiment only, chronological trade open/close events, realized balance; no engine writes.

Deployment fields: `id`, `release_id`, `experiment_id`, `mode` (`dry_run`), `status` (`starting`, `paused`, `running`, `unavailable`, `stopped`, `failed`), `ready`, `created_at`, `updated_at`, `error`, `versions`, `snapshot`, `snapshot_stale`, `decisions`. `snapshot` is null until available, otherwise `{observed_at, show_config, health, balance, positions, trades}`. Always label stale saved snapshots. Unavailable RPC is not stopped. Engine state `stopped` with live process is unavailable. Readiness requires a verified owned process, expected dry-run config, and successful process timestamp no older than 90 seconds. New deployment startup may need public exchange connectivity. Poll every 3–5 seconds.

Never show Resume until `ready && status === 'paused'`. Pause is available for running deployments. Stop is available for any active/unavailable deployment. Display explicit warning: stopping engine stops position management and does not close positions.

`decisions` contains recent recorded closed-candle SMA observations, signal validity and strategy hashes. `risk_state` and `order_reference` are null with `not_recorded` labels; signals do not prove orders. No secrets, private engine config, RPC URL, credentials or process identity are public.

Replay fields: `experiment_id`, `mode` (`historical_replay`), `strategy`, `period`, `initial_balance`, `final_balance`, `events`, `provenance`, `limitations`. Events have `id`, `time`, `type` (`trade_open`/`trade_close`), `pair`, `trade`, `realized_balance`, `decision_evidence` (exact recorded ML prediction if present; otherwise null). UI owns playback clock/speed; original timestamps remain visible. Balance changes only at closes, not mark-to-market. Risk decisions are not reconstructed.

Runtime nested shapes (Freqtrade 2026.9):

- `snapshot.show_config`: `dry_run: true`, `runmode: "dry_run"`, `strategy: "QuantPilotSmaDryRun"`, `state: "paused" | "running"`, `force_entry_enable: false`, plus non-secret engine settings.
- `snapshot.health`: `last_process_ts` is epoch **seconds**; `last_process` is original UTC string.
- `snapshot.balance`: `{total, total_bot, stake: "USDT", note: "Simulated balances", starting_capital, currencies: [{currency, free, balance, used, est_stake, stake, ...}], ...}`.
- `snapshot.positions`: array of engine trade objects. Common fields: `trade_id`, `pair`, `is_open`, `open_date`, `open_rate`, `stake_amount`, `amount`, `profit_abs`, `profit_ratio`, `orders`. No positions is `[]`.
- `snapshot.trades`: `{trades: [engine trade objects], trades_count, offset, total_trades}`. Closed engine trades carry `close_date`, `close_rate`, `close_profit_abs`, `close_profit`, `exit_reason`; render missing fields as unrecorded.
- `decisions`: `[{pair, candle_open_at, available_at, observed_at, sma_fast: number|null, sma_slow: number|null, signal_valid: boolean, enter_long: boolean, exit_long: boolean, risk_state: null, risk_evidence: "not_recorded", order_reference: null, order_evidence: "not_recorded", versions}]`. All timestamps retain UTC offsets. These are indicator/signal observations, not completed order decisions. Latest 100 observations returned.
- `versions`: `{engine_version, base_strategy_sha256, wrapper_sha256, source_config_sha256, effective_config_sha256, adapter_sha256, release_id}`.
- Replay `trade`: `{pair, open_date, close_date, open_rate, close_rate, profit_abs, profit_ratio, exit_reason, stake_amount, amount, fee_open, fee_close, enter_tag}`. `decision_evidence` is null or CSV record `{pair, model_id, decision_at, feature_available_at, model_fit_at, model_label_max_at, prediction, do_predict}` (CSV values are strings). Only exact pair and opening timestamp matches are attached. Events also have `risk_state: null, risk_evidence: "not_recorded"`.

All three control endpoints return the same full deployment shape. Polling refreshes ownership and status; controls may return 409 for unsafe/not-ready transitions or 503 for RPC outage. A create/stop race can briefly return 409 while launch ownership is being recorded; retry polling.

Replay also returns `diagnostic_status` (`flagged`, `passed_sampled`, or `unverified`) and `diagnostics` containing original diagnostic `status`, `rows`, `scope`, `exit_code`, experiment/result binding and report read-time SHA. Always show `limitations` and preserve a visible flagged warning; these original reports do not constitute a new audit. ML remains historical-only even when replay succeeds. Exact-token create retries refresh ownership/readiness before returning, and may return stale/unavailable or failed state.

Private runtime storage lives under `HOME/runtime/deployments/ID/` and `HOME/runtime/deployments.sqlite3`. Do not publish or export these directories: engine configs contain local RPC credentials. Public API exposes only hash identities. Preserve these files for restart reconciliation and simulated-position review.
