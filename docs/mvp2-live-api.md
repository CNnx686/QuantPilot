# Local live control API (MVP2 implementation)

This is runnable local control code for Binance spot, SmaTrend, BTC/USDT and ETH/USDT. It has **offline fixture validation only**. No real account, exchange permission response, testnet order, live order or actual 24-hour simulation admission has been validated during development. Passing tests is not admission or evidence of profitability.

## Local enrollment

From the project virtual environment, run these interactively:

```powershell
.venv/Scripts/python.exe -m quantpilot.live_cli init-auth
.venv/Scripts/python.exe -m quantpilot.live_cli enroll-account
```

The first command requests a 14-character minimum local password and stores a salted scrypt hash. The second requests a dedicated Binance key and secret with `getpass`, stores Windows CurrentUser DPAPI ciphertext in ignored `runtime/live`, and applies a current-user-only directory ACL. No credential argv, environment variable, `.env` import or HTTP credential upload exists. DPAPI does not isolate credentials from programs running as the same Windows user. Never reuse the research LLM credential as the control password.

Permissions require authoritative Boolean values: reading and spot trading enabled, withdrawals/internal transfer/universal transfer/futures/margin disabled, IP restriction enabled. Missing fields fail. Initial account must hold only USDT with no open orders; no dust exception. Enrollment cannot replace an account while its deployment owns the singleton lease.

## Admission and operation

1. Collect historical SmaTrend evidence meeting live-sma-v1: 30 trades, 60 days, at most 10% drawdown, 20 sampled lookahead signals. The admission recomputes the historical audit; teaching release approval does not authorize live trading.
2. Run the existing SmaTrend dry-run deployment until both pairs have 24 consecutive hourly decisions observed within 10 minutes of availability and at least one closed trade in the actual engine SQLite database. The validator checks frozen strategy/configuration and decision provenance.
3. Log in and enqueue `preflight`; poll the authenticated account endpoint. The bounded child process performs actual read-only CCXT requests after user enrollment. Development tests never invoke that transport.
4. Create the deployment with fixed evidence references and explicit limits. It launches paused; the Freqtrade worker continues processing exits. Activation requires a fresh preflight and a recent engine handshake bound to the owned process, source hashes, installed guard and successful startup reconciliation. Process existence alone is insufficient. Every entry is checked below strategy callbacks at the exchange instance and at the original CCXT transport.
5. Pause blocks entries and preserves managed exits. `stop_process` requires explicit acknowledgment that local stoploss protection ceases. It does not force liquidation or implicitly cancel orders. Global service stop refuses a deployment unless it has explicitly been stopped.
6. Restart launches paused and requires current evidence and account preflight again. An unresolved intent or orphan holds the lease and blocks entries across process restarts.

The original funding balance and daily risk checkpoints are separate and persist across restart. At midnight the new day's equity and cumulative external-flow checkpoint are reconstructed from complete timestamped cashflow/fill history. This currently supports an account that was cash-only at UTC midnight. Overnight assets require a verified historical bid valuation; entries remain blocked when that valuation is unavailable, while exits continue. A later provable cash-only midnight resolves that gate. Flat recovery never relabels the original funding amount as today's equity. Deposits/withdrawals and supported-pair fills are read with bounded stable pagination. Unsupported assets, fee currencies, unknown orders or incomplete pages block entries. A deposit is not profit. Exposure includes total held assets marked at fresh bid, remaining remote buys and local reservations without counting matching client IDs twice.

## HTTP contract

The service must run at `http://127.0.0.1:8765`. Unsafe requests require exact `Origin: http://127.0.0.1:8765` and `Host: 127.0.0.1:8765`, including login. Other ports, missing Origin, `null`, and `localhost` are rejected. Session cookies are HttpOnly and SameSite=Strict, expire after 1800 seconds, and use path `/api/live`.

| Method and path | Request / result |
|---|---|
| GET `/api/live/status` | Public: `auth_configured`, `accounts_configured`, `policy`, supported pairs/venue, `fixture_only_evidence`, capabilities, gates, sanitized deployment ID/status/revision, read job status |
| POST `/api/live/login` | `{password}` → `{csrf_token, expires_in}` and session cookie; five attempts per five minutes |
| POST `/api/live/logout` | Session + Origin + CSRF; invalidates session |
| GET `/api/live/account` | Authenticated account reference, preflight, last 100 intents, read job and deployment |
| POST `/api/live/read-jobs` | `{kind: "preflight" | "reconcile" | "recover_flat"}` → queued |
| POST `/api/live/deployment` | `{experiment_id,dryrun_id,revision:0,limits:{capital,per_order,exposure,daily_loss,max_spread:0.002,buffer:0.02}}` |
| POST `/api/live/deployment/{action}` | `activate`, `pause`, `restart`, `stop_process`; body `{revision,acknowledge_local_protection_ceases:false}` |

Deployment and read-job mutations require `X-CSRF-Token` and `Idempotency-Key`. Reusing a key with another path/body returns 409. Interrupted requests remain pending, preventing a blind duplicate execution. Mutations return the new resource revision. No arbitrary strategy, engine configuration, shell command or credential payload is accepted.

Lifecycle state/revision changes are atomic. Slow process operations claim a durable operation token and finalize only against that token; an API restart pauses the deployment and quarantines an interrupted operation until explicitly stopped. Read jobs have unique IDs, deadlines and account bindings; every ledger commit checks that lease. A late child or watchdog cannot publish into a replacement job. Expiry alone does not free the slot: owned process termination must be verified. Operator reads require the deployment paused.

`GET /status` additionally distinguishes `deployment.process_alive` and `deployment.engine_ready`. Authenticated `/account` includes `daily_risk`, `daily_risk_error`, `reconciliation_required`, `engine_ready` and redacted `last_order_denial` with a fixed reason code. These are current or timestamped observations, never admission bypasses.

## Recovery and limitations

Only Freqtrade submits orders. The controller has no force-entry, transfer or liquidation endpoint. Deterministic journal IDs are injected as Binance `newClientOrderId`; uncertain responses are never retried. Exact `origClientOrderId` lookup plus Freqtrade's persisted order ID establishes managed state. Merely discovering an exchange order does not repair a missing Freqtrade Trade/Order.

For an orphan, an operator can manage the account outside this program while entries are blocked, then request `recover_flat`. Recovery requires all known orders to be terminal, exact journal resolutions, complete observed histories and an account with USDT only and no open orders. A no-response intent can be resolved only by authoritative OrderNotFound together with those flat checks. Automatic ORM reconstruction and automatic liquidation are intentionally absent.

The implementation still requires sequential specification and quality review before being represented as live-ready. Real Binance permission-field availability, historical pagination semantics and private-account execution have not been proven. Offline tests boot the installed Worker, Binance Exchange, strategy and RPC from the actual historical config shape with all transport replaced by fixtures. They verify credential injection before private reads, guard installation before RPC and strategy startup, reconfiguration, initial/replacement/position-adjustment entries, callback exceptions, and the installed decorated stoploss path. The targeted bootstrap/control suite currently has 27 passing tests, including timeout and post-response persistence failures, concurrent reservations, protective sells, authentication, partial-fill orphan recovery, fee replay, unexplained transfers and daily profit/loss separation. The receipt is fixture evidence and never satisfies admission gates.
