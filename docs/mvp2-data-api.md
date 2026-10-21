# MVP2 public data and news API

All writes inherit localhost Host/Origin checks. All GETs read SQLite only; no network requests. POST refresh/analyze returns a persisted job, usually `202`. Source choices are fixed; invalid IDs, URL fields or providers return `422`. Data lives in `runtime/mvp2-data.sqlite3`, separate from existing experiments. No exchange keys, orders or account calls are used.

## Exchange views

- `GET /api/data/exchanges` returns an array of three views for `binance`, `okx`, `kraken`.
- `GET /api/data/exchanges/{venue}` returns one view.
- `POST /api/data/exchanges/{venue}/refresh` (no body) enqueues public spot market metadata, BTC/USDT + ETH/USDT tickers and up to 48 hourly candles each.
- View shape: `{source, data, last_success, last_attempt, error, stale, availability, execution_enabled:false}`. Empty cache has `data:null, availability:"unverified", stale:true`. TTL is 300 seconds. Failure preserves previous data and last_success; error is a generic non-secret category. `availability:"unavailable"` means last whole-source fetch failed, while preserved data may still be displayed as cached. Also inspect each market's `availability` and `errors` for partial failures.
- `data`: `{source, adapter:"ccxt", adapter_version, market_type:"spot", timeframe:"1h", observed_at, markets, capabilities, execution_enabled:false, purpose}`.
- Market: `{symbol, venue_symbol, active, precision_mode, precision, limits, availability, ticker, candles, errors}`. Missing spot symbol contains `{symbol, availability:"unavailable", error:"spot_market_not_listed"}`. Precision/minimums are venue metadata, not order validation. CCXT `precision_mode` controls whether precision means decimal places/tick sizes/significant digits. Null values mean unknown; never substitute zero.
- Ticker: numeric-or-null `timestamp,last,bid,ask,baseVolume,quoteVolume,percentage` and `observed_at` (UTC ISO). Candles: `{timestamp,open,high,low,close,volume,closed_at_ms,closed,observed_at}`. Timestamp/closed_at_ms are Unix milliseconds. The final open candle is marked `closed:false`.
- Capabilities: public markets/ticker/OHLCV are `verified` only after the corresponding successful request; historical_backtest/dry_run/live_trading remain `unverified`. This does not add other venue datasets to historical research. Existing research uses its fixed dataset and execution adapter.

## Jobs

`GET /api/data/jobs/{id}` -> `{id,kind,source,payload,status,created_at,started_at,finished_at,result,error}`. States: queued/running/succeeded/failed. Poll ~2 seconds and stop on terminal state. Errors are deliberately generic and never upstream exception text.

The API starts one lightweight scheduler at startup; a global SQLite execution slot prevents simultaneous jobs across server processes. Each execution runs in a subprocess with a 75-second wall deadline. CCXT request timeout is 12 seconds, no retries. Up to 8 outstanding jobs; identical pending requests deduplicate, and identical requests within 60 seconds reuse their latest job. A crashed running lease becomes failed after 95 seconds; queued work resumes at startup. Interrupted paid analysis is not automatically retried. Queue/budget rejection returns `429`.

## News

- `GET /api/news/sources` -> array `{id,name,url,data,last_success,last_attempt,error,stale,availability}`. Sources: `federal_reserve`, `ecb`; TTL 1800s.
- `POST /api/news/refresh` body `{"source":"federal_reserve"}` -> job.
- `GET /api/news?source=ecb&asset=BTC&event=monetary_policy&limit=50` -> latest version per source/item. All filters optional; limit 1–100. Asset choices BTC/ETH; event choices monetary_policy/regulation/security/market/other. Asset filter means explicit asset mention, so broad macro announcements usually have no asset tags.
- `GET /api/news/{version_id}` -> immutable version plus `analyses` array.
- News fields: `id,source,title,summary,url,published_at,assets,event,classification,relevance,item_key,content_hash,first_seen_at,revision_seen_at,available_at,previous_version_id,availability_note`. `classification:"local_keyword_rules"` is honest local extraction; `relevance` is direct_asset_mention or macro_context.
- Store only title and short feed excerpt (max 500 characters), never full articles. Render all strings as text (React interpolation); never HTML. Links are HTTPS publisher-domain links. The fetcher accepts only the two fixed RSS endpoints, pins an already validated public DNS address, verifies TLS, forbids redirects/compression and limits XML to 512 KB/50 items. DTD/entities and non-UTF8 XML are rejected. No article URLs are fetched.
- Exact repeat -> no new version; changed content -> new immutable ID/hash, with preserved first_seen_at and new revision_seen_at. Publication time is publisher-reported; **available_at is local revision observation time**, never retroactively publication time. Historical versions remain accessible by ID.

## Analysis and research

- `POST /api/news/{version_id}/analyze` body `{"provider":"local"}` or explicitly `{"provider":"deepseek"}` -> job. Default local uses deterministic keyword extraction, no paid call. Success appears at `job.result` and in detail `analyses`.
- Analysis shape: `{version_id,content_hash,provider,model,created_at,analysis:{summary,event,assets,hypothesis,limitations},tools_enabled:false,historical_feature_eligible:false,notice}`.
- DeepSeek is opt-in per POST; uses existing configured LLM transport only with api.deepseek.com HTTPS and deepseek-chat/deepseek-reasoner. Max 1200 output tokens, transport 20-second deadline/64KB response cap, schema validation, no tools, structured untrusted-text prompt. Immutable-version/provider results are cached. Hard budget: 10 attempts per UTC day, including failures; no automatic paid retries. No paid request was used during development verification.
- `POST /api/news/{version_id}/research` (no body) creates and returns a saved research draft in the **existing** `/api/research` flow; repeated calls for that version+dataset return the same record. It uses supported trend template and 2025 development dates, with explicit contemporary-context-only caveat, not news as a historical feature. Historical dataset must be available (`409` otherwise).
- Draft contains existing fields plus `news_evidence:{id,source,url,title,content_hash,published_at,first_seen_at,revision_seen_at,available_at,historical_feature_eligible:false,usage:"hypothesis_context_only"}`. Existing draft edits retain this server-created evidence; clients do not send or edit it. Existing save/confirm API remains unchanged. Display the evidence link and unavailable-at-historical-time caveat near the hypothesis. One-click only saves a draft; review/confirmation still follows existing flow.

## Verification of official feeds

On 2026-10-15 direct bounded GETs returned HTTP 200 from both https://www.federalreserve.gov/feeds/press_all.xml (text/xml) and https://www.ecb.europa.eu/rss/press.html (application/rss+xml). Reachability is a point-in-time observation, not a guarantee. Runtime source health shows subsequent outcomes. Both sources are macro-policy news, not comprehensive cryptocurrency coverage.
