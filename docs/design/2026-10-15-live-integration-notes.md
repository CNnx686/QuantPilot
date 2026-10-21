# Installed Freqtrade 2026.9 live integration review

Read-only source review during MVP2 development. These are implementation constraints, not completed live validation.

- FreqtradeBot.execute_entry calls confirm_trade_entry only for initial orders and its safe wrapper defaults true on callback exception. Replacement and position adjustment paths bypass that callback. Never rely on it alone for entry risk enforcement.
- Dedicated live_runner must install ExchangeResolver.load_exchange guard before Worker construction. Wrap returned instance create_order before it can be accessed by RPC or strategy startup. Reconfiguration must reinstall and revalidate version/config/account hashes.
- Restrict spot and allowlisted exchange IDs. Guard every buy regardless of order mode. Spot sells do not set reduceOnly, so classify by side and verified spot mode. Journal create_stoploss separately; protective sell orders remain available during entry blocking.
- Ordinary pre-submit denial raises sanitized DependencyException, not OperationalException (which can stop position management). No risk-check exception can fall through to submission.
- Commit intent and account reservation before original transport call. Timeout or failure after possible submit is unknown, retains reservation and blocks subsequent buys until authoritative reconciliation. Client order IDs are venue-dependent; never assume retry is safe.
- Freqtrade persists an Order after exchange response, so a separate durable intent journal is required for response-loss gaps. Startup must reconcile orphan exchange orders, not just locally known trades.
- One account has one deployment lease. Process death or stopping does not release a lease with unresolved orders/positions.
- Frozen config sets cancel_open_orders_on_exit=false if stop_process semantics promise no cancellation. PAUSED manages exits; STOPPED does not. Current SMA uses local stoploss, so process stop does not preserve local protection.
- Existing dry_run launcher/RPC readiness rules remain unchanged. Live gets independently authenticated APIs, exact origin+CSRF, credential references and dedicated effective-config verification.

Required offline tests: initial/replacement/position adjustment calls all hit guard; callback exception cannot bypass; stale/nonfinite/incomplete risk snapshot denies; protective sell/stoploss allowed while entries blocked; uncertain submit never retried across restart; post-response journal failure preserves uncertain state; guard installed before fake RPC startup and after reconfiguration; concurrent account leases/reservations have one winner.

## Concrete first live scope

- Binance spot only, SmaTrend only, BTC/USDT and ETH/USDT; one dedicated account/one global deployment. Other venues remain public monitoring. No leverage, transfers, external writers, position adjustment, forced entries, or liquidation API.
- Enrollment is an interactive local CLI using getpass and Windows CurrentUser DPAPI; credential files have restricted ACL and only opaque references leave the credential module. DPAPI protects storage, not hostile processes under the same Windows identity.
- Independent local password setup, salted scrypt password hash, short-lived HttpOnly SameSite=Strict sessions and per-session CSRF. Live mutations require the exact configured loopback origin/host, idempotency body hash and expected revision. Never reuse the LLM key as control authentication.
- A new immutable live-sma-v1 decision recomputes historical integrity/diagnostic evidence and validates actual same-strategy simulation records. A teaching release status is not live authorization. Missing historical, dry-run or account evidence remains a concrete, collectable gate.
- Installed Binance CCXT exposes sapi_get_account_apirestrictions(), private_get_account(), fetch_open_orders(), fetch_my_trades(), fetch_deposits() and fetch_withdrawals(). Permission/account checks must use authoritative read responses, preserve unknown fields and never accept client booleans as proof.
- Start with an initially flat dedicated account and USDT funding. Equity includes total USDT and fresh bid-marked BTC/ETH holdings; locked/free are not double-counted. Exposure includes holdings, remaining remote buy orders and unresolved local reservations without duplicate counting. Unknown assets/fees/transfers or incomplete history block new entries.
- Persist equity/loss baselines and verified net external flows. Restart or day rollover cannot silently reset loss limits. Explicitly label partial-day baselines; unknown midnight state requires reconciliation.
- Create durable deterministic Binance client order IDs, injected into the original CCXT create_order request by a scoped guard. Installed fetch_order(None, symbol, {'origClientOrderId': id}) supports authoritative lookup. Never match an orphan merely by approximate time/amount or repeat a timed-out submission.
- Reconciliation must account for canceled-but-partially-filled orders and ensure Freqtrade actually manages any recovered holding. Discovering a remote order alone does not establish repaired engine state. Unresolved orphan state stays blocked and exposes a specific recovery reason.
- Global service.stop() currently recursively kills API descendants. Integrating a live runner requires an explicit shutdown contract: refuse unattended shutdown while a live engine needs protection, or require explicit stop-process acknowledgment. Never silently terminate a live child through generic service cleanup.

These are implementation requirements, not evidence of any connected account, real order or verified exchange behavior. Offline fake-transport tests must remain labeled as such in public status and the UI.
