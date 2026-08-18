# SunPark Migration Arbitrage Strategy

## What this is (plain English)

When someone creates a new memecoin on Solana, it starts on **Pump.fun** —
a tiny shop where almost nobody trades. If the coin gets enough buyers and
reaches ~$69,000 market cap (~85 SOL in the bonding curve), it
**"graduates"** and automatically moves to **PumpSwap** (pump.fun's own
DEX, since March 2025; historically Raydium). This migration is instant
and free — LP is burned (no liquidity-rug risk post-graduation).

Graduation is a big moment: more people can see the coin, it appears on
trading dashboards, and there's real liquidity. The price often jumps
from $69K to $150-500K market cap within hours (2-7x). This is the
**"graduation pump."**

**Migration arb** = buying the coin at or just after the PumpSwap
migration, then selling when the price spikes. You profit from the
difference.

**Important:** Since March 2025, graduated tokens go to **PumpSwap**
(program `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`), NOT Raydium.
Raydium is now the rare legacy path.

---

## How it works (the flow)

```
Token created on Pump.fun (bonding curve)
          |
          |  1.7% of tokens reach this point
          |  (~440 graduations/day measured Jun-Jul 2026)
          v
Bonding curve fills (~85 SOL / ~$69K market cap)
          |
          |  Migration event fires on-chain
          |  stream.py detects "Migrate" instruction
          v
Token lands on PumpSwap (LP burned, ~$12K liquidity)
          |
          |  Price spike: 2-7x within hours
          |  (NOT guaranteed — many dump immediately)
          v
Buy at migration → Sell the pump → Profit
```

### The three jobs

1. **Detect** — the moment the coin graduates (SunPark's stream already
   does this — `stream.py` watches for `Migrate` instructions).
2. **Decide** — is this coin likely to pump, or is it garbage? (SunPark's
   gate + dev reputation filter + holders + intel do this).
3. **Execute** — buy on PumpSwap the moment it lands, sell when price
   spikes (needs real Jupiter swaps + Jito bundles + own node for speed).

---

## The numbers (2026 data, sourced)

### Graduation statistics

| Metric | Value | Source |
| --- | --- | --- |
| Total Pump.fun launches (Jun 23 - Jul 19, 2026) | 693,331 | Scorp Trader measurement |
| Graduations in that window | 11,877 | Scorp Trader measurement |
| **Graduation rate** | **1.71%** (~1 in 58) | Scorp Trader measurement |
| Graduations per day | ~440 | 11,877 / 27 days |
| Graduation pump range | $69K → $150-500K market cap | Multiple sources |
| Typical spread duration | 30 minutes to several hours | Multiple sources |

### Dev-reputation filter (lifts odds)

| Filter | Graduation rate | Notes |
| --- | --- | --- |
| No filter (baseline) | 1.71% | Random token |
| Dev with prior graduation + quiet ≥7 days | **~12%** | Scorp Trader filter (95% CI: 10-14.4%) |
| Realized after execution friction | **~6.5%** | Scorp Trader real-capital test |

### The honest profit reference

| Source | Test | Result |
| --- | --- | --- |
| Scorp Trader (Jul 2026) | 77 real-capital positions, dev-reputation filter | **−0.220 SOL net** (lost money) |
| Reason | Entry premium + latency + missed fills | Break-even needs ~8-9% realized win rate |

**Translation:** the money exists on paper, but execution friction eats
the edge unless you have speed infrastructure (own node + Jito) and a
tight filter.

---

## What SunPark already has (reuse)

| Capability | Status | File |
| --- | --- | --- |
| Detect Migrate events on Pump.fun | **Live** — `stream.py` watches Pump.fun logs | `stream.py` |
| Track post-migration price (1m/5m/30m windows) | **Live** — `stats.py` rollup | `stats.py` |
| Record 5m/30m forward outcomes | **Live** — `outcomes.py` | `outcomes.py` |
| Dev reputation from token_registry | **Live** — `intel.py` | `intel.py` |
| Holder concentration check | **Live** — `holders.py` | `holders.py` |
| Contract safety (freeze/mint authority) | **Live** — `safety.py` | `safety.py` |
| Selection gate (hard rejects) | **Live** — `filters.py` | `filters.py` |
| Mechanical ranker | **Live** — `rank.py` | `rank.py` |
| Exit engine (TP/stop/trail/time/thesis-break) | **Live** — `exits.py` | `exits.py` |
| Paper account | **Live** — mechanical auto-paper mode | `worker.py` |
| Jupiter quote pricing (dry-run) | **Live** — quote-only, no execution | `jupiter.py` |

---

## What needs to be built (the gaps)

| Gap | Why | Priority |
| --- | --- | --- |
| **Own Solana full node** | Faster data (1-50ms vs 100-500ms shared RPC), no rate limits, no Helius dependency | **Phase 0** — foundational |
| **Geyser/Yellowstone gRPC** | Stream block data in real-time from your own node; detect migration events in <50ms | **Phase 0** — on your node |
| **Jito bundle integration** | Send buy transactions as bundles with tips for priority block inclusion; without this, you lose to faster bots | **Phase 1** |
| **Real Jupiter swaps** | Currently dry-run only; need to enable real `swap` endpoint (not just `quote`) | **Phase 1** |
| **Migration-specific entry rule** | Detect PumpSwap pool creation (not just bonding curve completion) and trigger entry at the right moment | **Phase 1** |
| **Graduation-pump expectancy report** | Measure historical win rate from SunPark's own DB before risking real money | **Phase 0** — data, no money |
| **Position sizing for migration plays** | Smaller, faster positions than the momentum lane; different risk profile | **Phase 1** |

---

## The speed stack

```
┌─────────────────────────────────────────────────────┐
│  YOUR FULL NODE (Solana validator-adjacent)          │
│  ┌──────────────────────────────────────────────┐   │
│  │  Geyser / Yellowstone gRPC plugin            │   │
│  │  - Streams block data in real-time           │   │
│  │  - Detects Migrate event in ~1-50ms          │   │
│  └──────────────────────────────────────────────┘   │
└──────────────────┬──────────────────────────────────┘
                   │  You see the event FIRST
                   v
┌─────────────────────────────────────────────────────┐
│  SUNPARK GATE + RANK (existing)                     │
│  - selection_gate: dev rep, holders, safety, vol    │
│  - rank.py: score top candidates                    │
│  - Decision: buy or skip (~100-200ms)               │
└──────────────────┬──────────────────────────────────┘
                   │  Top candidate passes gate
                   v
┌─────────────────────────────────────────────────────┐
│  JUPITER SWAP (real, not dry-run)                   │
│  - Build swap transaction (SOL → token on PumpSwap) │
│  - Use keyless quote API for routing                │
└──────────────────┬──────────────────────────────────┘
                   │
                   v
┌─────────────────────────────────────────────────────┐
│  JITO BUNDLE + TIP                                 │
│  - Wrap swap in Jito bundle                         │
│  - Add tip ($0.01-0.10) for priority inclusion     │
│  - Submit to next block                             │
└──────────────────┬──────────────────────────────────┘
                   │
                   v
┌─────────────────────────────────────────────────────┐
│  YOU OWN THE TOKEN ON PUMPSWAP                      │
│  - Price spike begins (2-7x)                        │
│  - Exit engine handles TP/stop/trail                │
└─────────────────────────────────────────────────────┘

Total latency target: <1 second (event → buy)
```

---

## The money math ($2,000/month target)

### Conservative model

| Parameter | Value |
| --- | --- |
| Plays per month | ~90 (top 3/day from ~440 graduations) |
| Position size | 0.2 SOL (~$30 at SOL $150) |
| Win rate needed | ~35-40% |
| Average win | +2.5x to +3x |
| Average loss | -0.5x (50% stop) |
| Jito tip per trade | ~$0.05 |
| Net per month | ~5-15 SOL (~$750-$2,250) |

### The math breakdown

```
90 plays × 0.2 SOL = 18 SOL at risk per month

Winners (35%): 31 plays × +0.5 SOL avg win = +15.5 SOL
Losers (65%):  59 plays × -0.1 SOL avg loss = -5.9 SOL
Jito tips: 90 × $0.05 = ~$4.50 = -0.03 SOL
─────────────────────────────────────────────
Net: ~+9.6 SOL ≈ $1,440/month (at SOL $150)
```

At 40% win rate:
```
Winners (40%): 36 plays × +0.6 SOL avg win = +21.6 SOL
Losers (60%):  54 plays × -0.1 SOL avg loss = -5.4 SOL
Jito tips: -0.03 SOL
─────────────────────────────────────────────
Net: ~+16.2 SOL ≈ $2,430/month (at SOL $150)
```

### What changes the number

- **Win rate**: the single biggest variable. Your dev-reputation filter
  is the edge here — measure it from your own data first.
- **SOL price**: higher SOL = more USD per SOL profit, but also more
  USD per SOL loss.
- **Position size**: scaling from 0.2 to 0.5 SOL doubles everything
  (including risk).
- **Speed**: slower execution = worse entry price = lower win rate.

---

## Risk management (non-negotiable)

### Per-trade rules

| Rule | Value | Why |
| --- | --- | --- |
| Max position size | 0.5 SOL | Never bet more than you can lose |
| Stop loss | -50% of position | Cut losses fast |
| Take profit 1 | +2x, sell 50% | Recover initial ("house money") |
| Take profit 2 | +5x, sell 50% of remainder | Lock in gains |
| Trailing stop | 25% from peak | Let winners run |
| Max hold time | 6 hours | Dead capital → exit |
| Max concurrent positions | 3-5 | Don't spread too thin |

### Portfolio rules

| Rule | Value | Why |
| --- | --- | --- |
| Daily loss limit | -6% of account | Circuit breaker |
| Weekly loss limit | -18% of account | Circuit breaker |
| Max plays per day | 5 | Avoid tilt / overtrading |
| Min gate score | Pass selection_gate | Never enter unfiltered junk |

### The "never" list

- **Never** enter a graduation that fails the selection gate
- **Never** widen a stop loss
- **Never** add to a losing position
- **Never** trade more than max concurrent positions
- **Never** skip the Phase 0 data measurement before going live

---

## Phased plan

### Phase 0 — Measure (no money, no risk)

**Goal:** answer "would this have worked?" using SunPark's own data.

**What to build:**
1. Query `mint_stats` for every token where `graduated_at` is set
2. Look at 5m/30m/1h price change after graduation
3. Apply the dev-reputation filter (creator with prior graduation + quiet ≥7 days)
4. Calculate: of filtered graduations, how many pumped 1.5x/2x/3x within 30m?
5. Output: win rate, average payoff, and what the $2,000/month model would have returned

**Expected timeline:** 1-2 days of data analysis
**Risk:** None (read-only query on existing data)
**Decision:** if win rate > 30% after filter → proceed to Phase 1. If not → stop.

### Phase 1 — Paper execution (no real money)

**Goal:** prove the execution pipeline works.

**What to build:**
1. Wire migration-triggered entries into the existing paper account
   (mechanical mode already does this for momentum picks)
2. Set entry: buy at PumpSwap pool creation (or first rollup price
   after migration)
3. Set exits: SunPark's existing TP/stop/trail/time engine
4. Run 2-4 weeks of paper trading on graduation plays only

**Expected timeline:** 1-2 weeks to build, 2-4 weeks to paper-test
**Risk:** None (paper money only)
**Decision:** if paper PnL is positive over 2+ weeks → proceed to Phase 2

### Phase 2 — Live small (real money, tiny positions)

**Goal:** prove real execution works with skin in the game.

**What to build:**
1. Enable real Jupiter swaps (behind a risk cap)
2. Add Jito bundle integration
3. Set position size: 0.1 SOL per play (max)
4. Set max concurrent: 3 positions
5. Set daily loss limit: -2% of account

**Infrastructure needed:**
- Own Solana full node (with Geyser gRPC)
- Jito sidecar (for bundle submission)
- Real Jupiter swap endpoint

**Expected timeline:** 2-4 weeks to build, 4 weeks to live-test
**Risk:** max ~0.3 SOL per day at risk (~$45)
**Decision:** if live PnL is positive over 4 weeks → proceed to Phase 3

### Phase 3 — Scale (real money, normal positions)

**Goal:** reach $2,000/month target.

**What to change:**
1. Position size: 0.2-0.5 SOL per play
2. Max concurrent: 5 positions
3. Max plays per day: 5
4. Daily loss limit: -6% of account

**Expected timeline:** ongoing
**Risk:** ~$150-375 per day at risk (at SOL $150)
**Decision:** continue if positive; halt if negative for 2+ weeks

---

## Infrastructure costs

| Item | Monthly cost | One-time | Notes |
| --- | --- | --- | --- |
| Solana full node (dedicated server) | $100-200 | — | 32GB RAM, fast NVMe, good bandwidth |
| Geyser/Yellowstone gRPC plugin | $0 (self-hosted) | — | Runs on your node |
| Jito sidecar | $0 (open source) | — | Runs on your node |
| Jito bundle tips (~90 trades/mo) | ~$5 | — | $0.05 avg per trade |
| Jupiter API | $0 | — | Keyless public endpoint |
| Your own Solana RPC node | $0 (self-hosted) | — | Also serves SunPark |
| **Total overhead** | **~$105-205/mo** | — | — |

**Capital required:** ~$1,000-2,000 working (rotating in and out, not locked).

---

## Honest reality check

### What works in your favor

- SunPark already detects graduations and tracks post-migration prices
  — you have the data to measure win rate for free
- Your own node is the speed edge most retail traders don't have
- Jupiter integration is 80% done (quote-only, need swap endpoint)
- The exit engine already handles TP/stop/trail/time — no re-invention
- Fully anonymous (no CEX, no KYC, just a wallet)
- Capital-light (~$1-2k vs Pocket's ~$20-40k locked)

### What works against you

- **Execution friction is the real enemy.** Scorp Trader proved that a
  12% theoretical win rate became 6.5% in practice. Your edge must come
  from speed (own node + Jito) and better filtering (your gate + intel).
- **Most graduations don't pump.** Even filtered, 88% of flagged tokens
  still fail. You must survive 88% losers.
- **Speed war at the migration moment.** You're competing with dedicated
  sniper bots that have sub-100ms latency. Your own node helps, but
  you're not a validator with block-building priority.
- **SOL price volatility.** Your PnL is denominated in SOL, then
  converted to USD. A SOL crash eats your USD returns.
- **This is NOT passive income.** It requires active monitoring,
  decision-making, and discipline. More like a trading job than rent.

### The dealbreaker test

Before going live, you MUST run Phase 0 (measure from your own data).
If the win rate after your filter is below 8-9%, **stop**. That's the
break-even threshold. Don't hope — measure.

---

## Decision framework

```
START
  │
  v
Phase 0: Measure win rate from SunPark DB
  │
  ├── Win rate < 8%? → STOP. Strategy doesn't work with your filter.
  │
  ├── Win rate 8-12%? → MARGINAL. Proceed with extreme caution (Phase 1 only).
  │
  └── Win rate > 12%? → PROCEED to Phase 1 (paper execution).
          │
          v
      Phase 1: Paper test 2-4 weeks
          │
          ├── Paper PnL negative? → STOP. Execution kills the edge.
          │
          └── Paper PnL positive? → PROCEED to Phase 2 (live small).
                  │
                  v
              Phase 2: Live 0.1 SOL/play, 4 weeks
                  │
                  ├── Live PnL negative? → STOP or reduce size.
                  │
                  └── Live PnL positive? → PROCEED to Phase 3 (scale).
                          │
                          v
                      Phase 3: Scale to 0.2-0.5 SOL/play
                          │
                          ├── Hit $2,000/month? → MAINTAIN.
                          │
                          └── Not hitting? → RE-EVALUATE filter + sizing.
```

---

## Files to reference

| File | Role in migration arb |
| --- | --- |
| `stream.py` | Detects Migrate events (already live) |
| `stats.py` | Tracks post-migration price windows |
| `outcomes.py` | Records 5m/30m forward outcomes |
| `filters.py` | Selection gate (dev rep, holders, vol, age) |
| `intel.py` | Dev reputation, wash trade, whale signals |
| `rank.py` | Mechanical ranker for top candidates |
| `exits.py` | TP/stop/trail/time/thesis-break exits |
| `worker.py` | Paper entry + exit orchestration |
| `jupiter.py` | Jupiter quote API (needs real swap endpoint) |
| `safety.py` | Mint/freeze authority checks |
| `holders.py` | Top-20 holder concentration |
| `storage.py` | DB schema for outcomes, mint_stats, picks |

---

*Created 2026-08-16. Strategy is DEX-only, fully pseudonymous, capital-light
(~$1-2k working), skill-heavy. Phase 0 (measure) is mandatory before any
real money is risked.*
