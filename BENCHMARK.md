# SunPark Benchmark: How the Big Solana Bots Actually Make Money (2026)

Research summary that shapes Phase A. Distilled from public 2026 reporting on
the dominant Solana trading/terminal products. The goal is not to copy a
product - it is to copy the *edge* that survives: selectivity + exits, not
latency.

## The big names and their numbers

| Product | What it is | Reported numbers |
| --- | --- | --- |
| Axiom (ex-Photon) | Swap/terminal, ~60-72% of Solana bot volume | ~280k MAU, ~$25M/mo revenue at peak, $200M+ cumulative |
| GMGN | Memecoin data + smart-money terminal | Holder counts, creator analysis, sniper/copy-trade flows; leads smart-money visibility |
| BullX | X-chart swap/terminal | Filter-first UX, copy trading, limit orders, anti-snipe lens |
| Trojan | Multichain TG bot | ~$23.4B all-time volume |
| Banana Gun | TG sniper + limit | ~$16B volume |
| Sniperoo | MEV/anti-MEV sniping | sub-500ms target |
| Maestro / BonkBot | TG bots | ~$13-14B volume each |
| Infra | Jito ShredStream (+~270ms), bloXroute, Yellowstone gRPC | The latency arms race only pays for the sniping lane |

## The 6-step playbook we already mirror

1. **Program-level streams, never RPC polling** for detection -> `stream.py`
   (logsSubscribe on Pump/Raydium).
2. **Context before trust** (holder concentration, dev reputation,
   bundler/sniper/wash) -> `token_registry` + `intel.py` + `wallet_intel`.
3. **Mechanical pre-filter** -> strict `selection_gate` (floors 5 buyers /
   30 min age / 5 SOL 5m, tuned 2026-08-16).
4. **AI last-pass ranker, not decision-maker** -> `rank.py` Top-5 + DeepSeek
   sanity check only.
5. **Exits planned before entry** -> `exits.py` (TP1/TP2/trail/stop/dead/time/
   thesis-break).
6. **Dashboard + ops** -> `dashboard.py` Live Flow board.

## What Phase A adds (2026-08-16)

- **A1 contract safety** - mint/freeze authority parse + `freezable` hard
  reject, `mintable_after_migration` context-aware (bonding-curve mints keep
  authority pre-graduation by design).
- **A2 smart-money convergence** - two+ smart wallets buying the same mint in
  the window gets a ranker bonus (whale is a weighted input, never blind copy).
- **A3 dev-sell trigger** - creator wallet selling own token = intel red flag
  + immediate thesis-break exit.
- **A4 portfolio circuit breakers** - daily 6% / weekly 18% realized loss
  halts new entries and force-closes open paper positions.
- **A5 breakeven stop after TP1** - after the 2x 50% sell (house money
  recovered), the remainder's floor ratchets to entry instead of 0.95x.

## Gap analysis (they have it, we don't - yet)

| Capability | Needed? | When |
| --- | --- | --- |
| Holder-count/supply analytics (GMGN-grade) | Nice-to-have, paid data | Post-validation, optional |
| Copy-trading, limit orders, MEV protection | Only for the copy/snipe lanes | Yellowstone upgrade path |
| Sub-500ms sniping (Sniperoo/ShredStream) | Not for a ~$1k momentum lane | NEVER as the primary edge |
| AI-verified contract/token checks | Yes - A1 covers the MVP | Now |

## What NOT to copy

- **The latency arms race.** ShredStream/Jito/bloXroute fees only pay back when
  front-running other snipers; at our size selectivity and exits beat latency.
- **Blind whale copying.** Split wallets, decoys and wash trades game it; we
  use whales as a weighted input and an exit trigger only.
- **Alert firehoses.** ~37-50k tokens launch/day, ~98.6% flagged scam/rug.
  Rejecting junk IS the edge; our gate already rejects the top of funnel.
- **All-in sizing.** 95% of retail losses come from position sizing, not
  selection; we size each entry at ~10% of a $10 paper account with a 50% stop.

## Roadmap

1. Phase A (this branch): A1-A5 implemented and smoked locally, no deploy.
2. Paper outcome validation on the live server (already recording PnL).
3. Deploy Phase A, then re-validate gate + exits on live data.
4. Post-validation only: dashboard auth, optional Jupiter execution, then the
   Yellowstone sniping/copy lanes if the paper results justify the cost.

## Deep-dive research round (2026-08-16): how they really make money

Follow-up research on the playbooks, with harder numbers and the 2026 shifts.

### Revenue model reality
- **They sell infrastructure, not direction.** Axiom: $200M+ cumulative
  revenue, ~$20.5B volume, ~57% bot-volume share (July), ~1% gross fee, YC
  backed, "fastest app to $200M revenue." Feb 2026 front-running scandal: an
  employee used internal order data for personal trades - the "edge" was flow
  abuse, not skill. Photon's share fell ~30.7% -> <10% during 2025.
- **Fee stacks, not strategies.** Trojan (DeFiLlama): $224.9M cumulative fees,
  $167.4M revenue, $1.09M 30d fees. Banana Gun: $16B+ lifetime volume, 24M
  trades, 1.2M users, 40% revenue share to token holders. BonkBot: $94.8M
  fees. GMGN: 1% flat fee. BullX: 0.5%.
- **Durability = charging winners AND losers the same fee.** Nobody durable
  survives by being a better direction-trader on public data; that's a zero-sum
  lottery against the same order flow everyone sees.

### The data moat (GMGN, the leader)
- **"3+ smart wallets buy the same token in minutes" = strongest signal.**
  This validates SunPark's A2 `convergence` (>=2 smart wallets, +5 ranker
  bonus) as the right analog.
- **Copy-trading loses for most users**: survivorship bias on leaderboards,
  priority-fee overhead per mirrored trade, always-later entry, and per-trade
  fees. GMGN itself: "filters are the product."
- **Wallet vetting checklist** (we partially mirror): insider/rat/sniper/
  bundled labels, holding-time (seconds-long round trips = bots/farmers),
  7D vs 30D PnL + win rate, funding-source tracing, buy-vs-sell volume.
- **Only ~1% of Pump.fun tokens graduate** (Dune); >70% of new Solana tokens
  launch there. Selectivity is the edge, not discovery.

### Detection / sniping playbook
- Sniper = first buy <1s; bundler = 5-50 wallets funded from one master
  (0.1-0.5 SOL each, funding trace); wash circles = self-trading wallet sets.
- Pump graduation = ~$69k market cap, three transactions in one slot (close
  curve, seed Raydium pool, liquidate curve); needs `account_required` filters
  vs ~10k false positives/day.
- Jito: 95%+ of stake, tips >60% of priority-fee volume, Bundles (1-5 tx
  atomic), ShredStream sub-slot. Yellowstone gRPC: $49/mo (2 streams),
  $149/mo (7), $449/mo (25).

### The 2026 shift: AI agents
- Agent-native terminals are the new growth axis: Fasol Trade (bring your own
  Claude/Codex agent, you write rules + risk limits), GMGN OpenAPI agent
  skills (500+ data dimensions, sub-0.3s end-to-end orders), Coinbase "Agents"
  for AI-managed trading, BankrBot Claude plugins.
- This is where SunPark's DeepSeek-last-pass architecture fits: our picks are
  already machine-readable decision cards, ready to expose as agent skills/MCP
  later without changing the pipeline.

### Holder/whale data sources (the missing analytics)
| Source | Type | Notes |
| --- | --- | --- |
| Free public RPC | `getTokenSupply` + `getTokenLargestAccounts` + `getMultipleAccounts` | Top-20 concentration only; no full holder count. **Phase B implements this.** |
| Birdeye Data Services | Paid | Holder distribution ranges, top-holder lists, holder-count chart API |
| Vybe Network | Paid | Top-1000 holders, % of supply, entity labels, 3h refresh |
| Moralis | Paid | Holder count + acquisition + distribution buckets |
| Septim Labs | Free tool | Read-only public RPC concentration demo |

### What this round changes for SunPark
- **Phase B (implemented locally, no deploy): holder concentration via free
  RPC** - `whale_heavy` (positions 2+3, top-1 pool excluded) and `dev_heavy`
  (creator >= 10% supply) hard rejects. Closes the biggest intel gap at $0.
- **No paid infrastructure yet.** Yellowstone/Jito only justified for the
  sniping/copy lanes, after paper results validate the momentum lane.
- **Copy-trading stays out** - the data confirms it's a fee product for the
  platform, not an edge for the user.
