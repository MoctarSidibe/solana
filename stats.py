import os
import threading
import time

WINDOWS = (60, 300, 1800)
IDLE_EXPIRE_S = int(os.getenv("SUNPARK_STATS_IDLE_EXPIRE_S", "7200"))


def _prune_events(events, now, window):
    cutoff = now - window
    while events and events[0][0] < cutoff:
        events.pop(0)


def _summarize(events):
    buy_sol = 0.0
    sell_sol = 0.0
    buy_count = 0
    sell_count = 0
    buyers = set()
    sellers = set()
    for timestamp, side, sol, trader in events:
        if side == "buy":
            buy_sol += sol
            buy_count += 1
            if trader:
                buyers.add(trader)
        else:
            sell_sol += sol
            sell_count += 1
            if trader:
                sellers.add(trader)
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_sol": round(buy_sol, 4),
        "sell_sol": round(sell_sol, 4),
        "net_sol": round(buy_sol - sell_sol, 4),
        "vol_sol": round(buy_sol + sell_sol, 4),
        "unique_buyers": len(buyers),
        "unique_sellers": len(sellers),
    }


class MintStats:
    __slots__ = (
        "mint",
        "events",
        "first_seen",
        "last_seen",
        "initial_liquidity_sol",
        "price_sol",
        "price_first",
        "price_first_at",
    )

    def __init__(self, mint, now=None):
        self.mint = mint
        self.events = []
        self.first_seen = now or time.time()
        self.last_seen = now or time.time()
        self.initial_liquidity_sol = None
        self.price_sol = None
        self.price_first = None
        self.price_first_at = None


class VolumeRollup:
    def __init__(self):
        self.mints = {}
        self.lock = threading.Lock()

    def record_swap(self, mint, timestamp, side, sol_amount, trader=None):
        now = timestamp or time.time()
        with self.lock:
            stat = self.mints.get(mint)
            if not stat:
                stat = MintStats(mint, now)
                self.mints[mint] = stat
            stat.last_seen = now
            stat.events.append((now, side, max(sol_amount or 0.0, 0.0), trader or ""))
            return stat

    def record_liquidity(self, mint, timestamp, initial_liquidity_sol):
        now = timestamp or time.time()
        with self.lock:
            stat = self.mints.get(mint)
            if not stat:
                stat = MintStats(mint, now)
                self.mints[mint] = stat
            stat.last_seen = now
            if stat.initial_liquidity_sol is None:
                stat.initial_liquidity_sol = initial_liquidity_sol
            return stat

    def record_price(self, mint, timestamp, price_sol):
        now = timestamp or time.time()
        with self.lock:
            stat = self.mints.get(mint)
            if not stat:
                stat = MintStats(mint, now)
                self.mints[mint] = stat
            if stat.price_sol is None:
                stat.price_sol = price_sol
            if stat.price_first is None:
                stat.price_first = price_sol
                stat.price_first_at = now
            else:
                stat.price_sol = price_sol
            return stat

    def _snapshot(self, stat, now):
        windows = {}
        for window in WINDOWS:
            bucket = [
                item
                for item in stat.events
                if item[0] >= now - window
            ]
            summary = _summarize(bucket)
            summary["first_seen"] = stat.first_seen
            summary["age_seconds"] = max(int(now - stat.first_seen), 0)
            summary["initial_liquidity_sol"] = stat.initial_liquidity_sol
            if stat.price_first is not None and stat.price_sol is not None and stat.price_sol > 0:
                summary["price_sol"] = stat.price_sol
                if stat.price_first_at and stat.price_first > 0:
                    summary["price_change_pct"] = round(
                        (stat.price_sol - stat.price_first) / stat.price_first * 100, 2
                    )
            windows[window] = summary
        total = _summarize(stat.events)
        total["age_seconds"] = max(int(now - stat.first_seen), 0)
        total["initial_liquidity_sol"] = stat.initial_liquidity_sol
        if stat.price_sol is not None:
            total["price_sol"] = stat.price_sol
        return {
            "mint": stat.mint,
            "age_seconds": total["age_seconds"],
            "total_swaps": len(stat.events),
            "total_sol_vol": total["vol_sol"],
            "net_sol": total["net_sol"],
            "buy_count": total["buy_count"],
            "sell_count": total["sell_count"],
            "unique_buyers": total["unique_buyers"],
            "unique_sellers": total["unique_sellers"],
            "initial_liquidity_sol": stat.initial_liquidity_sol,
            "first_seen": stat.first_seen,
            "last_seen": stat.last_seen,
            "price_sol": stat.price_sol,
            "price_first": stat.price_first,
            "price_first_at": stat.price_first_at,
            "events": stat.events[:5000],
            "windows": {str(window): windows[window] for window in WINDOWS},
        }

    def stats_for(self, mint, timestamp=None):
        now = timestamp or time.time()
        with self.lock:
            self._prune(now)
        stat = self.mints.get(mint)
        if not stat:
            return None
        return self._snapshot(stat, now)

    def _prune(self, now):
        cutoff = now - IDLE_EXPIRE_S
        stale = [
            mint for mint, stat in self.mints.items()
            if stat.last_seen < cutoff
        ]
        for mint in stale:
            del self.mints[mint]

    def top_mints(self, window_seconds, limit=12, timestamp=None):
        now = timestamp or time.time()
        with self.lock:
            self._prune(now)
            mints_copy = dict(self.mints)
        results = []
        for stat in mints_copy.values():
            bucket = [
                item for item in stat.events
                if item[0] >= now - window_seconds
            ]
            summary = _summarize(bucket)
            summary["mint"] = stat.mint
            summary["age_seconds"] = max(int(now - stat.first_seen), 0)
            summary["initial_liquidity_sol"] = stat.initial_liquidity_sol
            if stat.price_sol is not None:
                summary["price_sol"] = stat.price_sol
            results.append(summary)
        results.sort(key=lambda item: item["vol_sol"], reverse=True)
        return results[:limit]

    def snapshot_all(self, timestamp=None):
        now = timestamp or time.time()
        with self.lock:
            self._prune(now)
            mints_copy = dict(self.mints)
        return [
            self._snapshot(stat, now)
            for stat in mints_copy.values()
        ]

    def restore(self, rows):
        with self.lock:
            for row in rows:
                mint = row.get("mint")
                if not mint:
                    continue
                stat = self.mints.setdefault(mint, MintStats(mint, row.get("first_seen") or time.time()))
                stat.first_seen = min(stat.first_seen, row.get("first_seen") or stat.first_seen)
                stat.last_seen = max(stat.last_seen, row.get("last_seen") or stat.last_seen)
                stat.initial_liquidity_sol = row.get("initial_liquidity_sol")
                for event in row.get("events") or []:
                    if len(event) >= 4:
                        stat.events.append(
                            (float(event[0]), event[1], float(event[2]), event[3] or "")
                        )
                stat.price_sol = row.get("price_sol")
                stat.price_first = row.get("price_first")
                stat.price_first_at = row.get("price_first_at")


rollup = VolumeRollup()
