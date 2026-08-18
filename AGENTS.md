# SunPark Agent Notes

## Project
- This is a flat Python project, not a package: `brain.py` contains reusable DeepSeek judges, `bot.py` is the new-token dry-run path, `onchain.py` is the Solana JSON-RPC client, `stream.py` is the free WSS Pump.fun/Raydium ingress, `stats.py` is the in-memory per-mint volume/liquidity rollup, `safety.py` is the mint/freeze-authority RPC checker (background-cached, metadata.py pattern), `holders.py` is the top-20 holder-concentration RPC checker (background-cached, same pattern), and `webhook.py` is the Flask receiver/fallback ingress.
- `stats.py` rolls 1m/5m/30m buy/sell SOL volume, unique buyers/sellers, net flow, price change, and initial liquidity per primary mint from events the stream already fetched; it never makes its own RPC calls. The worker applies each candidate to the rollup and the dashboard reads `rollup.top_mints` for the Live Flow board. Snapshots persist to the `mint_stats` table every `SUNPARK_STATS_SNAPSHOT_SECONDS` (default 60) and restore on restart; mints idle past `SUNPARK_STATS_IDLE_EXPIRE_S` (default 2h) are pruned.
- `filters.py` `selection_gate` is the strict pre-rank gate (replaced `signal_gate`): it hard-rejects on `watch_only_creation`/`not_tradable_category`/`no_primary_mint`, any intel red flag (`intel_*`), and floor misses (`low_volume`, `few_buyers`, `bad_buy_ratio`, `old_token`, `no_age_data`, `thin_liquidity`). Rejections store explicit reasons. Thresholds are `.env` config (tuned 2026-08-16 from live diagnostics to buyers>=5 / age<=30m / vol>=5 SOL): `SUNPARK_MIN_VOL_5M_SOL` (default 5.0), `SUNPARK_MIN_BUYERS` (default 5), `SUNPARK_MIN_BUY_RATIO` (default 1.0), `SUNPARK_MAX_AGE_MIN` (default 30.0), `SUNPARK_MIN_INIT_LIQ_SOL` (default 0.0). `intel.py` attaches red flags + a 0-100 quality score to each analysis card. Phase A added contract-safety flags: `intel_freezable` (freeze authority present, hard reject) and `intel_mintable_after_migration` (mint authority retained post-graduation; pre-graduation bonding-curve mints are NOT rejected).
- `intel.py` and the `WalletBook` (`wallet_intel`) are 100% RPC-free: dev reputation from `token_registry`, wash/bundler/concentration from rollup windows, wallet scores from stream `fee_payer`/from-to accounts. `whale_inflow` (with smart-money `convergence` when >=2 smart wallets buy the same mint) ranks picks; `distribution_pressure` triggers the exit engine's thesis-break; `dev_sell` flags the creator wallet selling its own token (gate red flag + instant exit). `token_registry` records creator/created_at/decimals/graduated_at for every token seen (stream-derived only).
- `rank.py` is the mechanical ranker (score = vol/buyers/ratio/net + intel quality + whale inflow incl. a convergence bonus). A `picks_loop` thread persists the Top-5 to the `picks` table and runs DeepSeek as a LAST-PASS sanity check only on those picks; per-candidate AI track rows are `filtered`/`pending`, never a DeepSeek call. `exits.py` runs the paper exit engine (`exits_loop` thread) on rollup prices with TP tiers, stop/trailing, dead/time exits, thesis-break (incl. dev-sell), portfolio circuit breakers, and a breakeven floor after TP1.
- SunPark's product goal is to detect new SPL/Pump.fun launches, Raydium liquidity events, profitable whale activity, and large SOL transfers from on-chain data, then use DeepSeek to decide whether each candidate is a genuine opportunity.
- `webhook.py` acknowledges Helius after normalization, deduplication, persistence, and candidate enqueue. `worker.py` runs Track A and Track B asynchronously; never call DeepSeek in the request handler.
- `storage.py` owns the SQLite schema for processed events, queued candidates, both track decisions, `token_registry`, and the `token_safety` cache (mint/freeze authority rows, background-enriched). `filters.py` contains the light AI noise filter and deterministic comparison rules.
- `storage.py` also owns bounded `activity_logs`; this is observability only, not trading-decision data. Candidate payloads, analysis cards, and `track_decisions` remain the decision record.
- `filters.py` also builds a bounded analysis card (event metadata, mints, transfer counts, SOL size, and selected token facts); send this structured card to AI rather than expanding raw Helius payloads in prompts.
- `metadata.py` resolves and caches Metaplex name/symbol metadata from free RPC; missing metadata is valid and must remain `metadata_status=missing`, not be guessed.
- `safety.py` resolves mint/freeze authority from a free `getAccountInfo` on the mint account (SPL Mint COption layout, Token-2022 safe) into the `token_safety` cache; enrichment is background (`safety_loop` thread, deduped per mint), so `intel.assess` sees warm authority facts at picks time without blocking the webhook. Unknown/missing authority data must NOT be guessed into flags.
- `holders.py` resolves top-20 holder concentration from free RPC (`getTokenSupply` + `getTokenLargestAccounts` + jsonParsed `getMultipleAccounts` for owners) into the `token_holders` cache; enrichment is background (`holders_loop` thread, deduped per mint, plus a top-mint sweep in `picks_loop`). `intel.assess` reads the cache and hard-rejects `whale_heavy` (positions 2+3 combined >= `SUNPARK_MAX_WHALE_SHARE`, default 0.20 - the top-1 account is normally the bonding curve/AMM pool and is excluded to avoid pool noise) and `dev_heavy` (creator wallet holds >= `SUNPARK_MAX_DEV_HOLDER_SHARE`, default 0.10 of supply). Unknown/missing holder data must NOT be guessed into flags.
- The worker selects a primary non-quote mint, resolves metadata before both tracks, and persists the final `analysis_json` card on each candidate. This card is the decision input and dashboard data.
- Swap decisions do not wait on network metadata; they use cached identity when available and queue background enrichment. `metadata_status=pending` may later become `ok` or `missing` on the saved card.
- `filters.py` classifies Helius labels and transfer facts into `token_creation`, `liquidity`, `swap`, `whale_trade`, `large_sol_transfer`, `token_transfer`, or `other`; worker routing uses these categories to select the DeepSeek judge.
- `stream.py` subscribes one `logsSubscribe` stream per configured program and enriches signatures with `getTransaction` before enqueueing; it uses `SOLANA_WSS_URLS` and `SOLANA_RPC_URLS` comma-separated failover lists.
- `stream.py` prefilters logs to Pump `Create`/`Migrate`, PumpSwap swaps, and Raydium initialization/liquidity instructions, then derives bounded token and native SOL transfer facts from transaction balance changes. It prints periodic notification/candidate/RPC-failure counters.
- Compare mechanical ranking against DeepSeek as a last-pass sanity check on the Top-5 only: store AI signal, confidence, reason, and latency so AI's value can be measured rather than assumed. Per-candidate AI rows are `filtered` (gate-rejected, reason stored) or `pending` (gate-clean, awaiting picks ranking).
- `bot.py` has a real-buy stub but `DRY_RUN = True`; leave this enabled unless live trading is explicitly being implemented and reviewed.

## Strategy Constraints
- On-chain data is the source of truth; social data is optional context, not a critical-path dependency. Discord/web alerting is deliberately NOT integrated (no off-chain channels); it is a later add-on, not a pipeline dependency.
- Token creation is an early-warning event, not automatically tradable. Pump.fun graduation and Raydium liquidity are the important tradability events.
- DeepSeek is a LAST-PASS sanity check on the Top-5 picks only (`BUY`, `SELL`, or `IGNORE`); the strict `selection_gate` rejects junk before ranking, so AI never sees obvious junk.
- DeepSeek currently has a 20-second timeout and is too slow for a strict sub-second sniper path. Yellowstone/LaserStream is a later latency upgrade, not a reason to block current delivery.
- Do not enable live Jupiter execution until exits, risk limits, persistence, and outcome-labeled paper results validate the strategy. Record later 5-minute/30-minute outcomes where possible.

## Setup And Commands
- Install dependencies with `pip install -r requirements.txt` (`flask`, `requests`, `python-dotenv`, `websocket-client`).
- Put local secrets in a root `.env`; it is ignored by Git. AI uses `DEEPSEEK_API_KEY`, and `onchain.py` optionally uses `SOLANA_RPC_URL` before its public RPC fallbacks.
- Webhook configuration is read from `SUNPARK_HOST` (default `127.0.0.1`), `SUNPARK_PORT` (default `5010`), `HELIUS_AUTH_HEADER`, and `SUNPARK_DB_PATH` (default `data/events.sqlite`).
- The monitor is served publicly at `/sunpark/` temporarily; restore Basic Auth before exposing sensitive controls or trading operations.
- Run the standalone scripts with `python brain.py`, `python bot.py`, or `python onchain.py`; the latter performs live RPC and AI requests. Start the receiver with `python webhook.py`.
- `python webhook.py` starts the webhook and its background worker. `worker.py` can also be run through focused imports, but is not a separate systemd service.
- `python stream.py` starts the free Pump.fun/Raydium WSS ingress; deployment uses `sunpark-stream.service` separately from `sunpark.service`.
- There is no configured test runner, linter, formatter, or CI. For a syntax check, use `python -m py_compile brain.py bot.py onchain.py webhook.py storage.py filters.py worker.py stream.py stats.py dashboard.py metadata.py intel.py rank.py exits.py safety.py holders.py jupiter.py outcomes.py diagnostics.py`.

## Runtime Details
- `webhook.py` requires the request `Authorization` header to exactly match `HELIUS_AUTH_HEADER`; if unset, webhook requests are unauthorized. `POST /` and `POST /webhook` accept a dict or list, while `GET /health` is unauthenticated.
- Webhook events are stored in SQLite and deduplicated by transaction `signature`; the database directory is created on first use. Keep `SUNPARK_DB_PATH` writable in deployments.
- DeepSeek wrappers return parsed JSON or `None` on missing credentials/request/JSON errors. Callers must handle `None`; requests use a 20-second timeout and JSON-object response format.
- Token names/symbols are not guaranteed on-chain; use `primary_mint`, `metadata_status`, and decimals-aware amounts when identity is missing.
- The worker stores `rules` and `ai` rows in `track_decisions`; AI latency is measured separately from the fast rules track. AI failures become stored `error` decisions rather than crashing the worker.
- `dashboard.py` exposes monitor data: stream counters, queue depth, recent candidates (with gate flags), both signals, AI latency, AI errors, a Live Flow board (`/sunpark/api/top_mints`) of top mints by 5m SOL volume, forward-outcome Edge panels (`/sunpark/api/edge`), and RPC enrichment health (`/sunpark/api/enrichment`). It is currently public temporarily; do not add controls while auth is disabled.
- `onchain.py` tries the configured RPC endpoint first, then public fallbacks, and returns `None` after all endpoints fail.

## Deployment Caveat
- `README.md` references a remote `deploy.sh` that is not present in this checkout. `sunpark.service` is a systemd unit for `webhook.py`, but its paths and `sunpark` user are deployment-specific; verify them before using it.

## Deployment Target And Sequence
- The intended deployment host is reachable as `root@169.58.87.221`; do not SSH to another host unless the user changes the target.
- Deployment runs only the Flask webhook through systemd. It does not start `bot.py`, and live trading must remain disabled with `DRY_RUN = True`.
- The stream service is a second systemd unit for ingress only; it must enqueue candidates and never call DeepSeek or execute trades.
- On the server, verify the repository location, create the `sunpark` user, install Python dependencies in `/var/www/sunpark/.venv`, and review `sunpark.service` before installing it.
- Create `/var/www/sunpark/.env` on the server, never commit or print it. Set `HELIUS_AUTH_HEADER`, `DEEPSEEK_API_KEY`, optional `SOLANA_RPC_URL`, `SUNPARK_HOST=127.0.0.1`, `SUNPARK_PORT=5010`, and writable `SUNPARK_DB_PATH=/var/www/sunpark/data/events.sqlite`.
- For the Helius-free stream, set optional comma-separated `SOLANA_WSS_URLS` and `SOLANA_RPC_URLS`; `PUMP_FUN_PROGRAM_ID` and `RAYDIUM_PROGRAM_ID` have verified defaults in `stream.py`.
- Because systemd runs as `sunpark` and `python-dotenv` reads `.env`, keep the file `root:sunpark` with mode `640`; do not use `root:root 600` unless the service is changed to load credentials another way.
- Before exposing an ingress, run `python -m py_compile brain.py bot.py onchain.py webhook.py storage.py filters.py worker.py stream.py stats.py dashboard.py metadata.py intel.py rank.py exits.py safety.py holders.py jupiter.py outcomes.py diagnostics.py`, start the service(s), verify `curl http://127.0.0.1:5010/health`, and test an authenticated POST to `/webhook` or inspect `sunpark-stream` logs.
- Confirm `/var/www/sunpark/data` is writable by `sunpark`; events are persisted there and deduplicated by transaction signature.
- On this server, `ombia-sas.com` already resolves to `169.58.87.221` and has a valid Certbot certificate; its Nginx vhost proxies exact `/webhook` requests to `127.0.0.1:5010/webhook`. Preserve the existing website locations when changing this route.
- Configure Helius to use `https://ombia-sas.com/webhook`, then inspect `systemctl status sunpark` and `journalctl -u sunpark -f`.
- Helius is optional after the WSS stream is enabled; use `systemctl status sunpark-stream` and `journalctl -u sunpark-stream -f` for the replacement ingress.
- The monitor URL is `https://ombia-sas.com/sunpark/`; use `systemctl status sunpark sunpark-stream` and the monitor before investigating application silence. Restore dashboard auth before adding controls.
- The structured activity log is public temporarily at `https://ombia-sas.com/sunpark/logs`; it shows sanitized service events, not raw journal output. Do not log secrets or full transactions.

## Strategy Direction (locked 2026-08-16)
- Lane: FILTER-THEN-RANK. Mechanics reject the junk; DeepSeek is a LAST-PASS
  sanity check on the Top-5 only. `signal_gate` was REPLACED outright by the
  strict `selection_gate` (no toggle); rejections store explicit reasons.
- Goal: a handful of high-quality setups, not 1000 alerts, sized for a ~$1k
  budget. Entry, exit, and risk rules are all mechanical; AI only ranks/checks.
- Free WSS remains correct TODAY (momentum/selection lane). Yellowstone/Geyser
  is a LATER upgrade for sniping/copy-trading lanes, not a blocker for delivery.

## Benchmarked Knowledge (global research, 2026)
- Pros do: program-level streams (never RPC polling for detection) -> context
  (holder concentration, dev reputation, bundler/sniper/wash) -> mechanical
  pre-filter -> AI last-pass ranker. ~37k-50k tokens launch per day; 98.6% are
  flagged scam/rug, so selectivity IS the edge.
- Exits matter more than entries; plan exits BEFORE entry. Universal standard:
  TP1 @ 2x sell ~50% (recover initial = "house money"), TP2 @ 5x sell ~50% of
  remainder, rest trails 25-30%. Stops ~ -50% default, NEVER widened. Size so a
  stop costs <= 1-2% of account risk. Sell during volume spikes, not after.
- Scenario handling: token never moves -> dead-capital time exit (recycle);
  token moves late -> runner + trailing stop (no rigid short timer on healthy
  positions); dump/rug -> stop loss; distribution -> thesis-break exit (volume
  fade, big/smart-wallet sell, net flow flip negative); overstaying -> hard
  max-hold timeout.
- Outcome reality (MoonHydra benchmark, 20 trades): ~12 -> ~0, 5 -> 1.5-3x,
  2 -> 3-10x, 1 -> 10x+. The edge is surviving the 12 and not giving back the 2.
- Whale tracking works but is gamed: split wallets, decoys, wash trades, time
  lag. Use as a weighted input + exit trigger, never blind copy. Bundler/wash
  checks run BEFORE whale signals (a whale buying a bundled rug traps you).
- Stack notes: Axiom ~60-72% of Solana bot volume (Photon rebrand), GMGN leads
  data/smart-money; Yellowstone gRPC adds 50-200ms but is paid (~$49-100+/mo).

## Build Plan (phases, status tracked in commits)
1. diagnostics.py - funnel report (events -> mints -> survivors under floors);
   its numbers set the default floor values.
2. token_registry - creator, created_at, source, decimals, graduated_at;
   populated from stream events only (zero new RPC).
3. intel.py - dev_reputation, wash_trade_suspicion, bundler/concentration,
   wallet_reuse. 100% RPC-free, stream/rollup/registry derived.
3.5 wallet_intel - wallet scoreboard from our own stream (fee_payer + from/to);
   `whale_inflow` ranks picks; big/smart-holder distribution = exit trigger.
4. selection_gate - strict gate REPLACING signal_gate; hard rejects on intel
   red flags + floors (buyers/vol/ratio/age/liquidity), reasons stored.
5. ranker -> Top-5 picks (picks table + picks_loop thread); DeepSeek last-pass
   only on the Top-5, deduped by mint.
6. exits.py + paper account - TP tiers/stop/trailing/time/thesis-break; ~$1k
   virtual, fixed entries, max 3-5 concurrent, 1-2% risk/trade; PnL scoreboard.
7. dashboard - Live Picks, rejection counters, PnL scoreboard, funnel summary.
8. verify (py_compile + smoke) -> deploy /var/www/sunpark/ -> restart both units.

## Phase A (implemented + DEPLOYED 2026-08-16)
- A1 contract safety - `safety.py` + `token_safety` table: background mint/freeze
  authority RPC enrichment (safety_loop). Gate red flags: `intel_freezable`
  (hard reject) and `intel_mintable_after_migration` (context-aware - only fires
  when `token_registry.status == migrated`; bonding-curve mints keep authority
  pre-graduation by design). Smoke-validated 45/45, including COption parse.
- A2 smart-money convergence - `whale_inflow.convergence` (>=2 distinct smart
  wallets buying the same mint) gives the ranker a +5 bonus (whale part cap 15);
  pick reasons show `smart:N+conv`.
- A3 dev-sell - `intel.dev_sell(snapshot, creator)`; assess() adds the
  `intel_dev_sold` gate red flag and the exit engine force-closes with reason
  `dev_sell` (instant thesis-break when the creator wallet sells its token).
- A4 portfolio circuit breakers - `SUNPARK_DAILY_LOSS_LIMIT_PCT` (default 6%)
  and `SUNPARK_WEEKLY_LOSS_LIMIT_PCT` (default 18%), computed as realized
  close PnL vs `SUNPARK_PAPER_START_SOL` baseline. Halt new entries (open
  returns False + worker logs) and force-close open positions (`circuit_breaker`).
- A5 breakeven stop - `SUNPARK_BE_AFTER_TP1` (default 1): after the TP1 2x
  50% sell the remainder's floor ratchets from 0.95x to entry (`be_stop`).
- Local `.env` may override the new vars; server `.env` was left untouched.
  Deploy order (executed): py_compile all (incl. safety.py) -> copy to
  /var/www/sunpark/ -> restart sunpark -> verify /health + status 0.1s.

## Phase B (implemented + DEPLOYED 2026-08-16)
- B1 holder concentration - `holders.py` + `token_holders` table: background
  top-20 supply distribution RPC enrichment (`holders_loop` thread, deduped per
  mint via `holders_seen`; `queue_holders_job` from `process_candidate` plus an
  `enqueue_top_holder_jobs` sweep of `rollup.top_mints(300)` inside `picks_loop`).
  `getTokenSupply` + `getTokenLargestAccounts` + jsonParsed `getMultipleAccounts`
  resolve owners; `whale_share` = positions 2+3 combined (top-1 excluded to skip
  bonding-curve/AMM pool noise). Gate red flags from `intel.assess` reading the
  cache: `intel_whale_heavy` (whale_share >= `SUNPARK_MAX_WHALE_SHARE`, default
  0.20) and `intel_dev_heavy` (creator wallet >= `SUNPARK_MAX_DEV_HOLDER_SHARE`,
  default 0.10 of supply). Unknown/missing holder data is stored `missing` and
  never guessed into flags. Free-RPC only; paid holder APIs (Birdeye/Vybe/
  Moralis) deferred unless public RPC proves insufficient.

## Phase C (implemented + DEPLOYED 2026-08-16)
- C1 mechanical auto-paper - the paper account no longer needs DeepSeek to
  collect samples. `SUNPARK_AUTO_PAPER_MODE` (`mechanical` default | `ai` | `off`)
  + `SUNPARK_AUTO_PAPER_MAX` (default 1). Mechanical mode enters the top-N picks
  at rollup price every picks cycle (`try_paper_entry`, reason
  `entry_mech_top{rank}`); `ai` mode keeps the old AI-`BUY`-gated entry
  (`entry_ai_buy`). Entries still respect halt_trading/MAX_POSITIONS/balance and
  skip held mints. AI still runs as a last-pass overlay recording `ai_signal`.
- C2 forward outcomes - `outcomes.py` + `outcomes` table: every pick and paper
  open becomes a row at appearance time; `outcomes_loop` (60s tick) records
  price at +5m/+30m maturity from the LIVE rollup (NOT `mint_stats`, which is
  INSERT OR REPLACE so only the latest snapshot survives) and stores forward
  return %. Missing price at maturity resolves `nodata` (counted separately,
  never guessed). Paper closes set `exit_reason` on the latest open row
  (`set_paper_exit_reason`). `outcomes_summary()` gives win rate / avg / median
  +30m by mode, AI signal, rank and exit reason -> `/sunpark/api/edge` + Edge
  dashboard panel.
- C3 Jupiter paper-fill pricing - `jupiter.py` uses the keyless public
  `quote-api.jup.ag/v6/quote` (ExactIn, `SUNPARK_JUPITER_SLIPPAGE_BPS` default
  300, `SUNPARK_JUPITER_FEE_BPS` default 30) to price paper entries and exits at
  realistic fills: worker `paper_entry_fill` (Jupiter buy quote, rollup
  fallback + source recorded) and `paper.sell_price_hook` (Jupiter sell quote
  applied in `_close`/`_sell_share`; decision price stays rollup, PnL uses the
  quoted fill). Toggle with `SUNPARK_JUPITER_QUOTE` (default 1). The swap
  endpoint needs a wallet key and stays a dry-run stub behind `DRY_RUN=True`.
- Local `.env` may override the new vars; server `.env` was left untouched.
  Deploy order (executed): py_compile all (incl. safety.py, holders.py, jupiter.py,
  outcomes.py) -> copy to /var/www/sunpark/ -> restart sunpark -> verify /health
  + status 0.1s.

## Phase 0 (implemented 2026-08-16, migration arb measurement)
- Phase 0 fixes bugs and adds infrastructure to measure whether DEX-only
  migration arb (buy at Pump.fun graduation → PumpSwap) has positive expectancy.
  No money risked — pure measurement from historical DB data.
- Bug fix: `_sell_share()` PnL was always 0 due to `* 0` on exits.py:220.
  Fixed to `pnl = value - position.size_sol * share` (realized basis per tranche).
- PumpSwap tracking: `stream.py` now tracks program `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`
  (`pump_swap` in PROGRAMS dict). Classifies `PUMPSWAP_SWAP`/`PUMPSWAP_ACTIVITY` events
  so we observe post-migration token life. Configurable via `PUMP_SWAP_PROGRAM_ID` env.
- Dev quiet-period filter: `intel.py` `dev_reputation()` now returns `prior_graduations`,
  `quiet_days`, and `quiet_pass` (True when creator has >= 1 prior graduation AND >= 7
  days since last graduation). `assess()` applies a -10 penalty reduction for quiet_pass.
  This is the key filter that lifts graduation win rate from ~2% to ~12% (Scorp Trader).
- Graduation-aware age gate: `filters.py` `selection_gate()` now checks time since
  `graduated_at` (not `created_at`) for tokens with status=migrated. Freshly graduated
  tokens are no longer rejected as "old_token".
- `entry_category` column added to both `outcomes` and `picks` tables (migration-safe
  ALTER). `outcomes.py` records `event_category` on pick/paper outcomes; `storage.py`
  `insert_outcome()` and `save_picks()` include the new column. Worker propagates
  `event_category` through the picks pipeline.
- `migration_measure.py` (new): standalone script that queries the DB for graduation
  win rates with/without dev quiet-period filter, computes +5m/+30m returns, peak 2x+
  rates, average/median returns, and break-even analysis. Run with `python migration_measure.py`.
- DEX Migrations dashboard: new `/sunpark/api/migrations` endpoint and panel on the
  monitor showing total graduations, win rates at 2x/3x, dev filter pass/fail stats,
  and recent graduations with returns. Cached 120s. Dashboard HTML + JS fetch
  `/sunpark/api/migrations` every 10s.

## Deployment 2026-08-16 (Phase A/B/C live on 169.58.87.221)
- Phase A/B/C code shipped via scp to `/var/www/sunpark/` (owner sunpark:sunpark,
  `.venv` at `/var/www/sunpark/.venv`); pre-deploy copies backed up to
  `/var/www/sunpark/backup-20260816-*` (rollback point). Server `.env` untouched
  (`root:sunpark` 640). `sunpark.service` (Flask/worker) restarted; separate
  `sunpark-stream.service` ingress unchanged. `/health` + all `/sunpark/api/*`
  endpoints return 200 (sub-100ms; one-off status spike ~5s seen, not recurring).
- Production bug found during verify: `token_price_sol` NEVER worked on live data.
  The stream emits raw `tokenAmount` + `decimals`, NOT `amount_ui`, so prices were
  always None (0/878 `mint_stats` rows had a price -> no paper entries, no
  outcomes). Rewrote it (worker.py): pair the primary token leg's shared account
  with its WSOL leg; fall back to largest WSOL leg, then native SOL ONLY when no
  WSOL/USDC leg exists; USDC-only events return None (never fabricate). Verified
  live: ~480/1060 mint_stats rows now carry `price_sol`. Mechanical entries then
  fired end-to-end (`entry_mech_top1` opens, exit engine `stop_loss` close, open
  positions, `outcomes` rows).
- Production bug: `outcomes_loop` crashed every 60s tick
  (`name 'load_paper_trades' is not defined`) - added `load_paper_trades` to the
  `from storage import` block in worker.py.
- `holders_loop` livelock: transient RPC failures were immediately requeued, so
  one mint starved the queue forever. Now transient discards the mint from
  `holders_seen` (no requeue); `picks_loop`'s `enqueue_top_holder_jobs` sweep
  re-queues top mints each cycle -> self-heals without starving others.
- RPC reality check (2026-08-16): server Helius key in `.env` is EXHAUSTED
  (`max usage reached`, plain-text 429 -> JSONDecodeError caught and skipped).
  `api.mainnet-beta.solana.com` 429s; `solana-rpc.publicnode.com` serves light
  calls (metadata, safety, getTokenSupply ~66ms) but times out (5-13s) and
  rate-limits `getTokenLargestAccounts`; ankr=403 (needs key), drpc=solana not on
  free plan, metaplex/rpcpool unreachable/403. `metadata.RPC_URLS` = env +
  mainnet-beta + publicnode (cap [:3], was [:2] which dropped publicnode);
  `holders._fetch` timeout 2s->5s. Consequence: `token_holders` stays empty and
  the stream's `getTransaction` enrichment shows `rpc_failures` (~18k) until a
  working RPC exists. Holder enrichment retries each picks cycle and populates
  automatically when the RPC recovers.
- Enrichment health panel: `dashboard.enrichment_payload()` +
  `/sunpark/api/enrichment` + "RPC Enrichment Health" cards on the monitor
  (holders/safety/metadata/prices = ok/total, green/amber/red). holders is red
  until a working RPC exists; prices green once derived.
- Own-node upgrade path (user plans their own Solana node for more data): NO code
  changes needed, only server `.env` - `SOLANA_RPC_URL=http://<node>:8899` and,
  for the stream, `SOLANA_WSS_URLS=ws://<node>:8900` + `SOLANA_RPC_URLS=http://<node>:8899`.
  That restores holders + stream enrichment + full rate headroom; later add
  Geyser/Yellowstone gRPC for the sniping lane.
- DeepSeek API key has NO credits (HTTP 402): AI last-pass records
  `ai_signal`/`ai_reason` = "AI returned no result"; mechanical entries and exits
  are unaffected. Top-up the key to get real AI verdicts back.
- Repo status: git `origin` = `github.com/MoctarSidibe/solana`; only commit is the
  initial one (`31f1011`). All Phase A/B/C work plus dashboard/service files were
  deployed via scp and were NOT committed/pushed as of 2026-08-16.

## Phase D (implemented + DEPLOYED 2026-08-17)
- D1 tighter stop loss - `STOP_LOSS_PCT` default 0.50→0.30 (env override
  `SUNPARK_STOP_LOSS_PCT`). Micro-caps that drop30% from entry rarely
  recover. Confirmed working:3 bleeding positions stopped at -25%/-30%/-45%.
- D2 auto entry count - `SUNPARK_AUTO_PAPER_MAX` default 1→2. Faster capital
  deployment with MAX_POSITIONS=3 still capping concurrent exposure.
- D3 max-age scan filter - `rank.py` `MAX_SCAN_AGE_S` from
  `SUNPARK_MAX_SCAN_AGE_MIN` (default60). Excludes tokens >60 min from the
  ranking scan to avoid wasting cycles on stale/blue-chip mints (Fartcoin,
  BOME, USDT). Gate still applies its own30min max age.
- D4 WebSocket keepalive - `stream.py` `listen()` sends `ws.ping()` every25s
  on `WebSocketTimeoutException`, preventing publicnode's~30s idle timeout
  from dropping pump_swap subscriptions.
- D5 RPC timeout reduction - `rpc_get_transaction` timeout 10s→5s, removed
  double-retry loop (now single pass across URLs). Reduces worst-case stall
  per failed notification from~60s to~15s.
- D6 early break-even floor - After stop_loss check, if a position's peak
  reached1.2x entry and price drops back to entry, close with `be_floor`.
  Protects gains on volatile tokens before TP1.
- Paper account reset: cleared old bleeding trades from DB to reset circuit
  breakers for fresh session. Old DB (16GB) backup at events_old.sqlite.
- Stream WSS: dropped `api.mainnet-beta.solana.com` from `WSS_URLS` and
  `RPC_URLS` (constant 429s); publicnode only. `pump_swap` still disconnects
  every~30s but reconnects instantly with keepalive ping.
- First live paper trade with new config: "Job Application Inu" (Gg96tGwL)
  entered at 6.49e-9, pumped~100x, TP1+TP2 fired for~81 SOL paper profit.
  Demonstrates the full entry→TP pipeline working end-to-end.
- D7 event-driven picks - `worker.py` `picks_loop` replaced 60s sleep with
  `threading.Event` signaled by `process_candidate`. Picks fire within seconds
  of a qualifying candidate being processed, not every 60s. `SUNPARK_PICKS_MIN_INTERVAL`
  (default 5) prevents thrashing. Latency dropped from avg 30s to <5s.
- D8 per-mint exit cooldown - `exits.py` `MINT_EXIT_COOLDOWN_S` (default 300)
  prevents re-entering a mint for 5 minutes after a close. Stops churn where
  the same token gets entered→stopped→re-entered in a loop.
- Second massive winner: F9C4dqtdr7vW entered via event-driven pipeline,
  hit trailing_stop at +4506% (45x). Paper balance reached 462 SOL from
  10 SOL start. Validates the faster pipeline catches more winning moves.

## Phase E (implemented 2026-08-17)
- E1 _sell_share state bug fix - `exits.py` `evaluate()` now sets
  `position.state` BEFORE calling `_sell_share()`, so `position.to_row()`
  saves the correct post-transition state. Previously TP1 saved state="open"
  and TP2 saved state="tp1", causing double-TP2 phantom profit on Gg96tGwL.
- E2 Jupiter fallback endpoints - `jupiter.py` tries 3 endpoints in sequence:
  V6 (`quote-api.jup.ag/v6/quote`, no fee) -> Swap V2 (`api.jup.ag/swap/v2/order`,
  5-10 bps) -> Lite (`lite-api.jup.ag/v1/quote`, backup). First success wins.
  Total worst-case 9s (3s per endpoint). `SUNPARK_JUPITER_TIMEOUT` env override.
- E3 Jupiter slippage 30% - `SUNPARK_JUPITER_SLIPPAGE_BPS` default 300→3000
  (30%). Realistic for Pump.fun thin-pool fills; slippage is enforced on-chain
  by DEX programs, Jupiter cannot cheat. MEV bots can extract up to the
  slippage tolerance via sandwich attacks.
- E4 migration registry fix - `worker.py` `update_registry()` now only sets
  `status='migrated'` and `graduated_at` for `PUMP_MIGRATE` events (not all
  `liquidity` category events). Preserves existing `graduated_at` if already set.
  PumpSwap/Raydium liquidity events no longer overwrite migration timestamps.
- E5 honest paper PnL - `exits.py` `honest_summary()` applies 30% slippage
  + 0.3% fee to every closed trade and filters wash-traded mints
  (`wash_share >= 20%`). `dashboard.py` shows both raw and honest
  balance/PnL/win-count; honest cards include slippage%, wash count, and
  adjusted win rate. Starting balance = `SUNPARK_PAPER_START_SOL` (10 SOL).
- E6 persistent fake-trade flags - `paper_trades` table gained `is_wash`
  (INTEGER, set at close time from rollup wash_trade_suspicion, survives
  restarts) and `is_phantom` (INTEGER, auto-detected: second TP2 with no
  intervening open for same mint = pre-E1 state-bug phantom). Migration
  backfill marks F9C4 as wash (>8 trades) and 3 phantom TP2s (Gg96tGwL,
  GvUC, F9C4). `honest_summary()` excludes both wash and phantom trades
  from honest PnL/win-count. Dashboard shows wash count + phantom count
  cards. Result: raw balance 469.93 SOL, honest balance 121.11 SOL
  (honest PnL +111.11 from 10 SOL start, 6/15 honest wins).
- E7 double-slippage fix + MAX_POSITIONS 7 - `exits.py` `honest_summary()`
  was double-penalizing Jupiter-quoted trades: Jupiter buy/sell quotes already
  include 30% slippage in the fill price, then the formula applied another
  `(1-S)/(1+S)` haircut. Fixed by adding `entry_source` column to `paper_trades`
  (migration in `storage.py`): `jupiter` entries get fee-only adjustment
  `(1-FEE)`, `rollup` fallback entries keep the full slippage+fee haircut.
  `Position` class carries `entry_source`; threaded through `open_position()` ->
  `_record()` -> close/tp1/tp2 records. `worker.py` `try_paper_entry()` passes
  the source from `paper_entry_fill()`. MAX_POSITIONS default 3->7 (env
  `SUNPARK_MAX_POSITIONS`). Old trades default to `entry_source=rollup`.

## Yellowstone Upgrade Path (LATER, not now)
- `stream.py` `make_event` output is the ingress contract; swap WSS +
  `getTransaction` enrichment for Geyser/`yellowstone-grpc` tx streams with
  server-side program filters. worker/storage/stats/intel/exits stay untouched.
- Paid add-on (Chainstack Growth+, dRPC marketplace, or self-hosted plugin);
  keepalive pings, backpressure via bounded channel, reconnect with jitter and
  slot tracking. Justified only when we add sniping/copy-trading lanes.
