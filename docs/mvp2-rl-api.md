# MVP2 RL research: implementation and API contract

This is **standalone Stable-Baselines3 PPO training in an isolated CPU runtime, followed by real Freqtrade 2026.9 action CSV replay**. It is not native FreqAI RL. Installed `ReinforcementLearner.fit`, `MyRLEnv.calculate_reward` and `Base3ActionRLEnv.step` were inspected: those hooks require PyTorch in the Freqtrade environment. Keeping PyTorch separate avoids changing the existing LightGBM/backtest environment. These research strategies are not added to the legacy experiment, release or deployment allowlists. `release_eligible` is always false.

## Frozen protocol

`ppo-spot-v1` is BTC/USDT spot, hourly, five seeds **7, 17, 27, 37, 47**, each **8192 PPO steps**, two CPU threads, 32/32 MLP, rollout 256, batch 64, five epochs, learning rate 0.0003, gamma 0.99. The fixed budget is an engineering/research demonstration, not convergence or profitability evidence. Final checkpoints are retained for every seed; there is no validation winner selection.

UTC half-open windows: training `[2025-01-01, 2025-04-01)`, validation `[2025-04-01, 2025-07-01)`, development evaluation `[2025-07-01, 2025-10-01)`. **Both displayed evaluation windows have already been used for development. They are not blind tests.** Reserved 2025Q4 is not downloaded or evaluated. The verified `binance-week2` manifest and all input hashes are checked, and the BTC input is copied into each new job; old experiments are untouched.

Observations contain only six close-bar price features: 1/6/24-hour returns, 48-hour return volatility, close/SMA12 minus one, and close/SMA48 minus one. Rolling calculations use past/current candles only. Mean and standard deviation are fitted only on training rows, then frozen; transformed values are clipped to ±10. Wallet/position state is deliberately excluded so inferred actions do not depend on Gym's execution state.

Gym actions are hold / buy a fixed 1000 USDT / sell existing holdings. Initial cash is 10000 USDT, fee is 0.1% on each side, no borrowing or shorting. Invalid actions become holds and are counted. The observation at candle `t` is available at `t+1h`; an action fills at the next candle's open. Reward equals change in marked equity / initial wallet minus 0.1 times any increase in drawdown. Remaining holdings liquidate at the final candle close and incur exit fees. Training rewards, partial episodes and invalid counts are retained.

For each seed and both evaluation windows, deterministic frozen inference writes requested and Gym-effective actions with decision/feature/model-fit/training-information timestamps. **Freqtrade replays requested actions** and independently handles wallet/position validity, lot rounding, trade minimums, stoploss and fills. All Freqtrade rows use the same BTC window, fixed stake, wallet, fees and **5% stoploss**. SMA uses a fixed 12/48 crossover. The buy/hold comparator is a **risk-managed one-entry, 1000 USDT reference** with the same 5% stoploss; it is not all-wallet passive holding and does not re-enter after a stop.

Gym has no intrabar stoploss and uses exact quantities and last-close liquidation; Freqtrade uses its real stop/lot/terminal rules. Gym and Freqtrade metrics/curves therefore remain separate. Freqtrade is the authoritative trading comparison. No slippage, latency or market impact is modeled. Exported timing audits and prefix-invariance tests do not replace Freqtrade lookahead/recursive analysis, which remains unverified for this replay workflow.

## Runtime and operational behavior

Run `powershell -ExecutionPolicy Bypass -File scripts/setup-rl.ps1` from the project. The script creates `runtime/rl-venv` if absent, bootstraps pip if necessary, installs `requirements-rl.lock` including the CPU Torch wheel, and executes the readiness probe. Main `.venv` remains the Freqtrade runtime. The runtime used to freeze this lock is Python 3.13.5; the readiness probe checks actual imports, a tiny PPO fit, exact core versions and data hashes. Missing/incompatible environments report `available:false` with setup instructions; there is no fake-ready flag. Readiness is cached for 60 seconds.

An OS lock permits one RL scheduler; SQLite permits one queued/running/cancelling job. Workers run sequentially, isolated PPO first, then fourteen real Freqtrade subprocesses (five seeds + two controls, each on two windows). Readiness never launches a second compatibility/training probe while a research job is active: it returns previous proof or an explicit deferred result. The model child receives an environment allowlist without credentials, proxies, `PYTHONPATH` or Freqtrade overrides. The Freqtrade child may receive HTTP proxy settings for public exchange metadata but never exchange/LLM credentials. No real trading commands are used.

Whole-job deadline is 1800 seconds, training child 960 seconds, training/inference internal deadline 900 seconds, each Freqtrade replay 180 seconds. A coordinator watchdog survives scheduler death. Cancellation writes a marker and terminates the owned process tree. Startup recovery verifies persisted PID/creation time and scans exact job-specific Python argument arrays (coordinator, isolated training and Freqtrade output directory), suspends coordinators, kills and waits for descendants, then rescans. It marks interrupted jobs failed only after owned processes are verified gone. If Python processes cannot be inspected or remain alive, the slot stays `cancelling` and quarantined, with recovery retried every 30 seconds; elapsed time alone never frees the slot. Partial files and logs stay available. Terminal status is fenced against late successful completion. Source files are hashed/snapshotted before work and rechecked before publication; changes invalidate the run. Models, actions, input data, protocol, costs, raw engine ZIPs and result transforms are inventoried with SHA-256.

## HTTP contract

All endpoints inherit the main API's localhost Host/Origin protection. No endpoint accepts source code, filesystem paths, alternate dates, symbols, arbitrary seeds or unbounded training steps.

| Method and route | Contract |
| --- | --- |
| `GET /api/rl/readiness` | `{available,reasons,versions,setup_command,protocol,mode,release_eligible,implementation}` |
| `GET /api/rl/jobs` | Array of at most 100 recent persisted jobs |
| `POST /api/rl/jobs` | JSON `{name?: string, protocol?: "ppo-spot-v1"}`, required `Idempotency-Key`; 201 new, 200 repeated, 409 unavailable/busy/key conflict, 422 invalid |
| `GET /api/rl/jobs/{id}` | Job plus `progress` (`training`, `freqtrade_replay`, `baseline_replay`, `complete`) |
| `POST /api/rl/jobs/{id}/cancel` | Idempotent cancellation; queued → cancelled, running → cancelling → cancelled |
| `GET /api/rl/jobs/{id}/results` | Complete result only after success; otherwise 409; verifies result hash |
| `GET /api/rl/jobs/{id}/logs` | Last 32 KB each of worker and training logs |
| `GET /api/rl/jobs/{id}/artifacts` | Array `{path,bytes,url}`; partial artifacts remain inspectable |
| `GET /api/rl/jobs/{id}/artifacts/{path}` | Confined to this job directory; complete-job artifacts checked against inventory |

Job shape: `{id,name,config,status,created_at,started_at,finished_at,error}`. Status is `queued`, `running`, `cancelling`, `cancelled`, `failed`, `succeeded`. New UI submissions should generate a fresh idempotency key, persist it until response, and reuse the key only when retrying the same submission.

Results contain `rows` (all fourteen Freqtrade comparisons), `all_seeds` (every model hash, training reward history, separate Gym diagnostics), `summary` (mean/median/population SD/min/max across five seed net returns per window), `protocol`, `implementation`, `limitations`, and `release_eligible:false`. A row includes `window,strategy,seed,metrics,result_artifact,raw_artifact`; `metrics` includes net return, profit, drawdown, trades, win rate (null for zero trades), final/initial balance and turnover. Zero-trade seeds are retained and must not appear profitable merely because return is zero.

For selected-seed charts, fetch the row's `result_artifact` through its artifact URL. It contains real Freqtrade `equity` (realized closed-trade account balance, not marked equity), `trades`, assumptions and source hashes. For decisions, fetch `seed-{seed}/{window}-actions.csv`: use `requested_action` for policy decisions and `action`/`valid` for separate Gym validity diagnostics. Never label Gym actions as Freqtrade executions; join actual trade timestamps if explaining executions.

## Verification

Main-runtime tests: `python -m pytest tests/test_rl_store.py tests/test_rl_api.py tests/test_rl_worker.py tests/test_rl_replay.py -q`. Isolated tests: `runtime/rl-venv/Scripts/python.exe -m pytest tests/test_rl_env.py -q`. They cover causal feature-prefix invariance, train-only scaling, next-open fills, fees, invalid/terminal actions, price-only observations, single-slot/idempotency, recovery/late-result fencing, credential filtering, typed input, artifact integrity/path traversal, actual child cancellation and timeout, exact requested-action replay and rejection of future/duplicate rows. The readiness subprocess uses `Popen` plus whole-tree termination and wait on timeout; a real isolated-venv sleeper regression verifies that the Windows launcher descendant exits without doing another PPO fit. Main targeted tests passed (17 before the additional readiness descendant regression; the new regression and active-probe regression both passed), as did all four isolated environment tests.

Completed job: `rl-1db76077c9504e0995821a29263f3ee7`, 2026-10-15 06:27:19–06:35:37 UTC (about 8m18s). All five seeds completed 8192 steps; all fourteen Freqtrade CLI runs completed. Results and the 186-file SHA-256 inventory are under `artifacts/rl/rl-1db76077c9504e0995821a29263f3ee7/`. The separate audit receipt is `artifacts/rl-audits/rl-1db76077c9504e0995821a29263f3ee7.json`; recreate it with `python tests/test_rl_actual_evidence.py rl-1db76077c9504e0995821a29263f3ee7`.

| Strategy/seed | Apr–Jun net return | Jul–Sep net return | Trades Apr–Jun / Jul–Sep |
| --- | ---: | ---: | ---: |
| PPO 7 | -0.4801% | -0.9268% | 66 / 59 |
| PPO 17 | -0.8527% | -1.8990% | 103 / 87 |
| PPO 27 | -2.1675% | -2.7877% | 124 / 131 |
| PPO 37 | -1.2618% | -1.4304% | 90 / 80 |
| PPO 47 | -1.1251% | -1.8095% | 125 / 104 |
| SMA 12/48 | +0.2410% | -0.5495% | 28 / 29 |
| One-entry hold with 5% stop | -0.5191% | +0.5904% | 1 / 1 |

PPO mean/median: **-1.1775% / -1.1251%** on validation and **-1.7707% / -1.8095%** on development evaluation. Every PPO seed lost money in both windows. No seed is selected or promoted. The audit verified all inventory hashes, all 21950 exported action timestamps, all **969 actual PPO trade entries** against the prior closed candle's requested buy and next-open market price, and signal exits against requested sells and next-open prices. It also reconciled realized balances and confirmed 2025Q4 is absent. The setup script was actually run successfully on the existing uv-created environment: it bootstrapped missing pip, found locked dependencies already satisfied and returned `available:true` from an actual PPO compatibility fit. Successful execution is engineering evidence and does not imply model quality or release approval.
