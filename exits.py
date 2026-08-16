"""Mechanical paper exit engine for SunPark.

Open positions are evaluated against rollup prices every `SUNPARK_EXIT_INTERVAL`
seconds. Rules are fully mechanical (never AI); scenarios covered:
  - normal pump   -> TP1 @2x (sell 50%, house money), TP2 @5x, trail remainder
  - never moves   -> dead-capital time exit (recycle)
  - moves late    -> runner + trailing stop (no rigid timer on healthy holders)
  - dump / rug    -> stop loss (never widened)
  - distribution  -> thesis-break: big/smart sell, dev sells own token, or
                     sell pressure ratio
  - overstaying   -> hard max-hold timeout
  - portfolio     -> daily/weekly circuit breakers force-close and halt entries
After TP1 the remainder's floor ratchets to breakeven (SUNPARK_BE_AFTER_TP1).

All trade decisions are recorded to `paper_trades` for the PnL scoreboard.
`DRY_RUN` stays True; this engine never touches a real wallet.
"""

import os
import threading
import time
from datetime import datetime, timezone

from intel import dev_sell, distribution_pressure
from storage import (
    get_token_registry,
    load_paper_positions,
    load_paper_trades,
    save_paper_position,
    save_paper_trade,
    utc_now,
)

PAPER_START_SOL = float(os.getenv("SUNPARK_PAPER_START_SOL", "10"))
PAPER_ENTRY_SOL = float(os.getenv("SUNPARK_PAPER_ENTRY_SOL", "1.0"))
MAX_POSITIONS = int(os.getenv("SUNPARK_MAX_POSITIONS", "3"))
STOP_LOSS_PCT = float(os.getenv("SUNPARK_STOP_LOSS_PCT", "0.50"))
TP1_PCT = float(os.getenv("SUNPARK_TP1_PCT", "2.0"))
TP1_SELL_PCT = float(os.getenv("SUNPARK_TP1_SELL_PCT", "0.50"))
TP2_PCT = float(os.getenv("SUNPARK_TP2_PCT", "5.0"))
TP2_SELL_PCT = float(os.getenv("SUNPARK_TP2_SELL_PCT", "0.50"))
TRAIL_PCT = float(os.getenv("SUNPARK_TRAIL_PCT", "0.25"))
TIME_DEAD_S = float(os.getenv("SUNPARK_TIME_DEAD_S", str(6 * 3600)))
DEAD_VOL_SOL = float(os.getenv("SUNPARK_DEAD_VOL_SOL", "0.5"))
TIME_MAX_S = float(os.getenv("SUNPARK_TIME_MAX_S", str(24 * 3600)))
BIG_SELL_EXIT_SOL = float(os.getenv("SUNPARK_BIG_SELL_EXIT_SOL", "2.0"))
SELL_BUY_RATIO_EXIT = float(os.getenv("SUNPARK_SELL_BUY_RATIO_EXIT", "2.0"))
BE_AFTER_TP1 = os.getenv("SUNPARK_BE_AFTER_TP1", "1") == "1"
DAILY_LOSS_LIMIT_PCT = float(os.getenv("SUNPARK_DAILY_LOSS_LIMIT_PCT", "6"))
WEEKLY_LOSS_LIMIT_PCT = float(os.getenv("SUNPARK_WEEKLY_LOSS_LIMIT_PCT", "18"))


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


class Position:
    def __init__(self, mint, entry_price_sol, entry_time, size_sol):
        self.mint = mint
        self.entry_price_sol = entry_price_sol
        self.entry_time = entry_time
        self.initial_size_sol = size_sol
        self.size_sol = size_sol
        self.peak_price_sol = entry_price_sol
        self.state = "open"  # open -> tp1 -> trailing -> closed

    def to_row(self):
        return {
            "mint": self.mint,
            "entry_price_sol": self.entry_price_sol,
            "entry_time": self.entry_time,
            "size_sol": self.size_sol,
            "initial_size_sol": self.initial_size_sol,
            "peak_price_sol": self.peak_price_sol,
            "state": self.state,
        }


class PaperAccount:
    def __init__(self):
        self.balance_sol = PAPER_START_SOL
        self.positions = {}
        self.closed = []
        self.trades = []
        self.sell_price_hook = None
        self.lock = threading.Lock()
        self._restore()

    def _restore(self):
        try:
            for row in load_paper_positions():
                position = Position(
                    row["mint"],
                    row["entry_price_sol"],
                    row["entry_time"],
                    row["size_sol"],
                )
                position.initial_size_sol = row["initial_size_sol"]
                position.peak_price_sol = row["peak_price_sol"]
                position.state = row["state"]
                self.positions[row["mint"]] = position
            for trade in load_paper_trades():
                self.trades.append(trade)
        except Exception:
            pass

    def _record(self, mint, action, price_sol, sol_value, pnl_sol, reason):
        trade = {
            "mint": mint,
            "action": action,
            "price_sol": price_sol,
            "sol_value": round(sol_value, 4),
            "pnl_sol": round(pnl_sol, 4),
            "reason": reason,
            "created_at": utc_now(),
        }
        self.trades.append(trade)
        try:
            save_paper_trade(trade)
        except Exception:
            pass

    def _realized_since(self, since):
        total = 0.0
        for trade in self.trades:
            if trade.get("action") != "close":
                continue
            created = _parse_iso(trade.get("created_at"))
            if created is None or created < since:
                continue
            total += trade.get("pnl_sol") or 0.0
        return total

    def breakers(self, now=None):
        """Portfolio circuit breakers from realized closes, as % of paper start.

        A daily/weekly realized loss at or above the configured limit halts new
        entries and force-closes open positions. Baseline is PAPER_START_SOL so
        the breakers are deterministic across restarts.
        """
        now = now or time.time()
        daily = self._realized_since(now - 86400)
        weekly = self._realized_since(now - 7 * 86400)
        return {
            "daily_loss_pct": round(max(-daily, 0.0) / PAPER_START_SOL * 100, 2),
            "weekly_loss_pct": round(max(-weekly, 0.0) / PAPER_START_SOL * 100, 2),
            "daily_halt": -daily / PAPER_START_SOL * 100 >= DAILY_LOSS_LIMIT_PCT,
            "weekly_halt": -weekly / PAPER_START_SOL * 100 >= WEEKLY_LOSS_LIMIT_PCT,
        }

    def halt_trading(self, now=None):
        info = self.breakers(now)
        if info["daily_halt"] or info["weekly_halt"]:
            return info
        return None

    def open_position(self, mint, entry_price_sol, entry_time=None, entry_reason="entry"):
        with self.lock:
            if self.halt_trading():
                return False
            if mint in self.positions:
                return False
            if len(self.positions) >= MAX_POSITIONS:
                return False
            size = min(PAPER_ENTRY_SOL, self.balance_sol)
            if size <= 0:
                return False
            position = Position(mint, entry_price_sol, entry_time or time.time(), size)
            self.positions[mint] = position
            self.balance_sol -= size
            self._record(mint, "open", entry_price_sol, size, 0.0, entry_reason)
            try:
                save_paper_position(position.to_row())
            except Exception:
                pass
            return True

    def _fill_price(self, mint, size_sol, price_sol):
        """Best-effort real fill price for a close, when a hook is configured."""
        if not self.sell_price_hook:
            return price_sol
        try:
            return self.sell_price_hook(mint, size_sol, price_sol) or price_sol
        except Exception:
            return price_sol

    def _close(self, position, price_sol, reason, now):
        fill_price = self._fill_price(position.mint, position.size_sol, price_sol)
        value = position.size_sol * (fill_price / position.entry_price_sol) if position.entry_price_sol else 0.0
        pnl = value - position.size_sol
        self.balance_sol += value
        self._record(position.mint, "close", fill_price, value, pnl, reason)
        self.closed.append(
            {
                "mint": position.mint,
                "entry_price_sol": position.entry_price_sol,
                "exit_price_sol": fill_price,
                "reason": reason,
                "pnl_sol": round(pnl, 4),
                "pnl_pct": round((fill_price / position.entry_price_sol - 1) * 100, 2) if position.entry_price_sol else 0.0,
                "created_at": utc_now(),
            }
        )
        try:
            save_paper_position({"mint": position.mint, "state": "closed", "entry_price_sol": position.entry_price_sol}, closed=True)
        except Exception:
            pass
        del self.positions[position.mint]

    def _sell_share(self, position, price_sol, share, action, now):
        if share <= 0 or position.size_sol <= 0:
            return
        fill_price = self._fill_price(position.mint, position.size_sol, price_sol)
        value = position.size_sol * share * (fill_price / position.entry_price_sol)
        position.size_sol *= 1 - share
        self.balance_sol += value
        pnl = value - (position.initial_size_sol - position.size_sol) * 0  # realized basis per tranche
        self._record(position.mint, action, fill_price, value, pnl, action)
        try:
            save_paper_position(position.to_row())
        except Exception:
            pass

    def evaluate(self, snapshots_by_mint, now=None):
        now = now or time.time()
        with self.lock:
            breaker = self.halt_trading(now)
            if breaker:
                for mint in list(self.positions):
                    position = self.positions[mint]
                    price = (snapshots_by_mint.get(mint) or {}).get("price_sol")
                    if price and price > 0:
                        self._close(position, price, "circuit_breaker", now)
                return
            for mint in list(self.positions):
                position = self.positions[mint]
                snapshot = snapshots_by_mint.get(mint) or {}
                price = snapshot.get("price_sol")
                if not price or price <= 0:
                    continue
                age = now - position.entry_time
                position.peak_price_sol = max(position.peak_price_sol, price)
                window = (snapshot.get("windows") or {}).get("300") or {}
                vol = window.get("vol_sol") or 0

                if age > TIME_MAX_S:
                    self._close(position, price, "max_hold", now)
                    continue
                if age > TIME_DEAD_S and vol < DEAD_VOL_SOL:
                    self._close(position, price, "dead_capital", now)
                    continue
                if price <= position.entry_price_sol * (1 - STOP_LOSS_PCT):
                    self._close(position, price, "stop_loss", now)
                    continue

                pressure = distribution_pressure(snapshot)
                if (
                    pressure
                    and (
                        (pressure["big_sells"] >= 1 and pressure["smart_sell_sol"] > 0)
                        or pressure["sell_buy_ratio"] >= SELL_BUY_RATIO_EXIT
                    )
                ):
                    self._close(position, price, "thesis_break", now)
                    continue

                creator = (get_token_registry(position.mint) or {}).get("creator")
                if creator and dev_sell(snapshot, creator)["dev_sold"]:
                    self._close(position, price, "dev_sell", now)
                    continue

                if position.state == "trailing":
                    if price <= position.peak_price_sol * (1 - TRAIL_PCT):
                        self._close(position, price, "trailing_stop", now)
                    continue

                if position.state == "tp1":
                    if price >= position.entry_price_sol * TP2_PCT:
                        self._sell_share(position, price, TP2_SELL_PCT, "tp2", now)
                        position.state = "trailing"
                    elif price <= position.entry_price_sol * (1.0 if BE_AFTER_TP1 else 0.95):
                        self._close(position, price, "be_stop" if BE_AFTER_TP1 else "giveback_stop", now)
                    continue

                if price >= position.entry_price_sol * TP1_PCT:
                    self._sell_share(position, price, TP1_SELL_PCT, "tp1", now)
                    position.state = "tp1"

    def summary(self):
        with self.lock:
            realized = sum(item["pnl_sol"] for item in self.closed)
            return {
                "balance_sol": round(self.balance_sol, 2),
                "open_positions": len(self.positions),
                "closed_count": len(self.closed),
                "realized_pnl_sol": round(realized, 2),
                "win_count": sum(1 for item in self.closed if item["pnl_sol"] > 0),
                "closed": list(self.closed),
            }


paper = PaperAccount()
