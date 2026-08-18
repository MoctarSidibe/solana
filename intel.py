"""RPC-free on-chain intelligence for SunPark.

All signals are derived from data we already ingest (stream swaps, rollup
windows, token_registry) - this module never makes its own RPC calls.

- `assess(card)`: red flags + 0-100 quality score for the selection gate.
- `WalletBook`: cross-mint wallet scoreboard from fee_payer/from-to accounts;
  `smart_wallets`, `whale_inflow`, `distribution_pressure` power ranking and
  the exit engine's thesis-break trigger.
"""

import os
import threading
import time

from stats import rollup

BIG_BUY_SOL = float(os.getenv("SUNPARK_BIG_BUY_SOL", "3.0"))
BIG_SELL_SOL = float(os.getenv("SUNPARK_BIG_SELL_SOL", "3.0"))
WASH_SHARE_MAX = float(os.getenv("SUNPARK_WASH_SHARE_MAX", "0.20"))
TOP_BUYER_SHARE_MAX = float(os.getenv("SUNPARK_TOP_BUYER_SHARE_MAX", "0.40"))
TOP3_BUYER_SHARE_MAX = float(os.getenv("SUNPARK_TOP3_BUYER_SHARE_MAX", "0.70"))
DEV_MAX_TOKENS = int(os.getenv("SUNPARK_DEV_MAX_TOKENS", "10"))
WALLET_MAX_MINTS = int(os.getenv("SUNPARK_WALLET_MAX_MINTS", "8"))
SMART_MIN_MINTS = int(os.getenv("SUNPARK_SMART_MIN_MINTS", "2"))
SMART_MIN_MARGIN = float(os.getenv("SUNPARK_SMART_MIN_MARGIN", "0.10"))
WALLET_EXPIRE_S = float(os.getenv("SUNPARK_WALLET_EXPIRE_S", "7200"))
WALLET_CAP = int(os.getenv("SUNPARK_WALLET_CAP", "50000"))
DEV_HOLDER_SHARE_MAX = float(os.getenv("SUNPARK_MAX_DEV_HOLDER_SHARE", "0.10"))
WHALE_SHARE_MAX = float(os.getenv("SUNPARK_MAX_WHALE_SHARE", "0.20"))


class WalletBook:
    """In-memory cross-mint wallet activity, bounded and thread-safe."""

    def __init__(self):
        self.wallets = {}
        self.lock = threading.Lock()

    def record_swap(self, trader, mint, side, sol_amount, timestamp=None):
        if not trader or not mint:
            return
        now = timestamp or time.time()
        with self.lock:
            wallet = self.wallets.get(trader)
            if not wallet:
                if len(self.wallets) >= WALLET_CAP:
                    oldest = min(
                        self.wallets.items(),
                        key=lambda item: item[1].get("last_seen", 0),
                    )
                    del self.wallets[oldest[0]]
                wallet = {
                    "buy_sol": 0.0,
                    "sell_sol": 0.0,
                    "buys": 0,
                    "sells": 0,
                    "mints": set(),
                    "first_seen": now,
                    "last_seen": now,
                }
                self.wallets[trader] = wallet
            wallet["last_seen"] = now
            wallet["mints"].add(mint)
            if side == "buy":
                wallet["buy_sol"] += max(sol_amount or 0.0, 0.0)
                wallet["buys"] += 1
            else:
                wallet["sell_sol"] += max(sol_amount or 0.0, 0.0)
                wallet["sells"] += 1

    def _prune(self, now):
        cutoff = now - WALLET_EXPIRE_S
        stale = [
            wallet for wallet, data in self.wallets.items()
            if data.get("last_seen", 0) < cutoff
        ]
        for wallet in stale:
            del self.wallets[wallet]

    def score(self, wallet):
        """-1 unknown; otherwise net-profit margin, negative = net loser."""
        with self.lock:
            data = self.wallets.get(wallet)
        if not data:
            return -1.0
        total = data["buy_sol"] + data["sell_sol"]
        if total <= 0:
            return 0.0
        return (data["sell_sol"] - data["buy_sol"]) / total

    def is_smart(self, wallet):
        with self.lock:
            data = self.wallets.get(wallet)
        if not data:
            return False
        return (
            len(data["mints"]) >= SMART_MIN_MINTS
            and data["sell_sol"] > data["buy_sol"] * (1 + SMART_MIN_MARGIN)
            and data["sell_sol"] + data["buy_sol"] >= BIG_BUY_SOL
        )

    def mints_traded(self, wallet):
        with self.lock:
            data = self.wallets.get(wallet)
        return len(data["mints"]) if data else 0

    def top_wallets(self, limit=20):
        now = time.time()
        with self.lock:
            self._prune(now)
            items = []
            for wallet, data in self.wallets.items():
                if data["sell_sol"] + data["buy_sol"] < BIG_BUY_SOL:
                    continue
                items.append(
                    {
                        "wallet": wallet,
                        "margin": round((data["sell_sol"] - data["buy_sol"]) / (data["sell_sol"] + data["buy_sol"] + 1e-9), 3),
                        "mints": len(data["mints"]),
                        "buy_sol": round(data["buy_sol"], 2),
                        "sell_sol": round(data["sell_sol"], 2),
                    }
                )
        items.sort(key=lambda item: item["margin"], reverse=True)
        return items[:limit]


wallet_book = WalletBook()


def _events_in_window(snapshot, window):
    if not snapshot:
        return []
    now = snapshot.get("last_seen") or time.time()
    return [event for event in snapshot.get("events") or [] if event[0] >= now - window]


def wash_trade_suspicion(snapshot, window=300):
    """Share of swaps where a wallet appears on both buy and sell sides."""
    events = _events_in_window(snapshot, window)
    if not events:
        return {"wash_share": 0.0, "wash_swaps": 0, "total_swaps": 0}
    both_sides = set()
    buyers = set()
    sellers = set()
    for _, side, _, trader in events:
        if side == "buy" and trader:
            buyers.add(trader)
        elif side == "sell" and trader:
            sellers.add(trader)
    both_sides = buyers & sellers
    wash_swaps = sum(1 for _, side, _, trader in events if trader in both_sides)
    return {
        "wash_share": round(wash_swaps / len(events), 3),
        "wash_swaps": wash_swaps,
        "total_swaps": len(events),
    }


def buyer_concentration(snapshot, window=300):
    """Top-1 and top-3 buyer share of buy SOL in the window."""
    events = _events_in_window(snapshot, window)
    buyers = {}
    total = 0.0
    for _, side, sol, trader in events:
        if side != "buy" or not trader:
            continue
        buyers[trader] = buyers.get(trader, 0.0) + max(sol or 0.0, 0.0)
        total += max(sol or 0.0, 0.0)
    if not total:
        return {"top1_share": 0.0, "top3_share": 0.0, "buy_sol": 0.0}
    ranked = sorted(buyers.values(), reverse=True)
    return {
        "top1_share": round(ranked[0] / total, 3),
        "top3_share": round(sum(ranked[:3]) / total, 3),
        "buy_sol": round(total, 3),
    }


def dev_reputation(creator):
    """Mass-launcher detection + quiet-period filter from token_registry (no new RPC)."""
    from storage import creator_tokens

    if not creator:
        return {"token_count": 0, "migrated": 0, "red_flag": False,
                "prior_graduations": 0, "quiet_days": None, "quiet_pass": False}
    tokens = creator_tokens(creator)
    migrated = [t for t in tokens if t.get("status") == "migrated"]
    prior_graduations = len(migrated)
    quiet_pass = False
    quiet_days = None
    if migrated:
        latest_grad = max((t.get("graduated_at") or 0) for t in migrated)
        if latest_grad:
            quiet_days = (time.time() - latest_grad) / 86400
            if quiet_days >= 7:
                quiet_pass = True
    result = {
        "token_count": len(tokens),
        "migrated": prior_graduations,
        "prior_graduations": prior_graduations,
        "quiet_days": round(quiet_days, 1) if quiet_days is not None else None,
        "quiet_pass": quiet_pass,
        "red_flag": len(tokens) >= DEV_MAX_TOKENS and prior_graduations < 2,
    }
    return result


def whale_inflow(snapshot, window=300):
    """Smart-wallet buyers + notable single buys in the window.

    `convergence` is True when two or more distinct smart wallets buy the same
    mint inside the window - the ranker gives that a score bonus.
    """
    events = _events_in_window(snapshot, window)
    smart_buyers = set()
    big_buys = 0
    for _, side, sol, trader in events:
        if side != "buy":
            continue
        if trader and wallet_book.is_smart(trader):
            smart_buyers.add(trader)
        if (sol or 0.0) >= BIG_BUY_SOL:
            big_buys += 1
    return {
        "smart_buyers": len(smart_buyers),
        "convergence": len(smart_buyers) >= 2,
        "big_buys": big_buys,
    }


def dev_sell(snapshot, creator, window=300):
    """Did the creator wallet sell the token it launched, in the window?"""
    if not creator:
        return {"dev_sold": False, "dev_sell_sol": 0.0, "dev_buy_sol": 0.0}
    events = _events_in_window(snapshot, window)
    dev_sell_sol = 0.0
    dev_buy_sol = 0.0
    for _, side, sol, trader in events:
        if trader != creator:
            continue
        value = sol or 0.0
        if side == "sell":
            dev_sell_sol += value
        else:
            dev_buy_sol += value
    return {
        "dev_sold": dev_sell_sol > 0,
        "dev_sell_sol": round(dev_sell_sol, 3),
        "dev_buy_sol": round(dev_buy_sol, 3),
    }


def distribution_pressure(snapshot, window=300):
    """Sell-side stress for the exit engine's thesis-break trigger."""
    events = _events_in_window(snapshot, window)
    big_sells = 0
    smart_sell_sol = 0.0
    sell_sol = 0.0
    buy_sol = 0.0
    for _, side, sol, trader in events:
        value = sol or 0.0
        if side == "sell":
            sell_sol += value
            if value >= BIG_SELL_SOL:
                big_sells += 1
            if trader and wallet_book.is_smart(trader):
                smart_sell_sol += value
        else:
            buy_sol += value
    return {
        "big_sells": big_sells,
        "smart_sell_sol": round(smart_sell_sol, 3),
        "sell_sol": round(sell_sol, 3),
        "buy_sol": round(buy_sol, 3),
        "sell_buy_ratio": round(sell_sol / buy_sol, 3) if buy_sol > 0 else 0.0,
    }


def assess(card):
    """Red flags + 0-100 quality for the strict selection gate.

    A single red flag is grounds for hard rejection. quality is only used
    as a tiebreaker inside the ranker.
    """
    mint = card.get("primary_mint")
    stats = card.get("stats") or {}
    snapshot = stats or None
    red_flags = []
    details = {}
    penalty = 0

    creator = None
    registry = None
    safety = None
    if mint:
        from storage import get_token_registry, get_token_holders, get_token_safety

        registry = get_token_registry(mint)
        creator = (registry or {}).get("creator")
        safety = get_token_safety(mint)
        details["registry"] = bool(registry)
        details["safety"] = safety
        if safety and safety.get("status") == "ok":
            if safety.get("freeze_authority"):
                red_flags.append("freezable")
                penalty += 50
            elif safety.get("mint_authority") and (registry or {}).get("status") == "migrated":
                red_flags.append("mintable_after_migration")
                penalty += 50

        holders = get_token_holders(mint)
        details["holders"] = holders
        if holders and holders.get("status") == "ok":
            if (holders.get("whale_share") or 0.0) >= WHALE_SHARE_MAX:
                red_flags.append("whale_heavy")
                penalty += 40
            if creator:
                dev_share = next(
                    (
                        entry.get("share") or 0.0
                        for entry in (holders.get("owners") or [])
                        if entry.get("owner") == creator
                    ),
                    0.0,
                )
                if dev_share >= DEV_HOLDER_SHARE_MAX:
                    red_flags.append("dev_heavy")
                    penalty += 50

    if creator:
        dev = dev_reputation(creator)
        details["dev"] = dev
        if dev["red_flag"]:
            red_flags.append("mass_launcher")
            penalty += 40
        elif dev["token_count"] >= 5:
            penalty += 15
        if dev.get("quiet_pass"):
            penalty = max(0, penalty - 10)
        dev_sell_info = dev_sell(snapshot, creator)
        details["dev_sell"] = dev_sell_info
        if dev_sell_info["dev_sold"]:
            red_flags.append("dev_sold")
            penalty += 50

    wash = wash_trade_suspicion(snapshot)
    details["wash"] = wash
    if wash["wash_share"] >= WASH_SHARE_MAX and wash["total_swaps"] >= 10:
        red_flags.append("wash_trade")
        penalty += 50

    concentration = buyer_concentration(snapshot)
    details["concentration"] = concentration
    if concentration["top1_share"] >= TOP_BUYER_SHARE_MAX:
        red_flags.append("bundled_top_buyer")
        penalty += 40
    elif concentration["top3_share"] >= TOP3_BUYER_SHARE_MAX:
        red_flags.append("bundled_insiders")
        penalty += 30

    fee_payer = card.get("fee_payer")
    if fee_payer:
        traded = wallet_book.mints_traded(fee_payer)
        details["fee_payer_mints"] = traded
        if traded >= WALLET_MAX_MINTS:
            red_flags.append("serial_sniper")
            penalty += 25

    if not stats:
        red_flags.append("no_flow_stats")
        penalty += 10

    return {
        "red_flags": red_flags,
        "quality": max(0, 100 - penalty),
        "details": details,
        "whale_inflow": whale_inflow(snapshot) if snapshot else {"smart_buyers": 0, "big_buys": 0},
        "distribution": distribution_pressure(snapshot) if snapshot else None,
    }


def snapshots_for(mints, limit=60):
    """Live rollup snapshots for the ranker (top_mints + intel in one pass)."""
    now = time.time()
    rows = rollup.top_mints(300, limit=limit, timestamp=now)
    by_mint = {}
    for row in rows:
        mint = row["mint"]
        if mint not in mints:
            continue
        by_mint[mint] = rollup.stats_for(mint, now)
    return by_mint
