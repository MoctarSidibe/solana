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
    append_activity,
    get_token_registry,
    load_paper_positions,
    load_paper_state,
    load_paper_trades,
    save_paper_position,
    save_paper_state,
    save_paper_trade,
    utc_now,
)

PAPER_START_SOL = float(os.getenv("SUNPARK_PAPER_START_SOL", "10"))
PAPER_ENTRY_SOL = float(os.getenv("SUNPARK_PAPER_ENTRY_SOL", "1.0"))
MAX_POSITIONS = int(os.getenv("SUNPARK_MAX_POSITIONS", "7"))
STOP_LOSS_PCT = float(os.getenv("SUNPARK_STOP_LOSS_PCT", "0.30"))
TP1_PCT = float(os.getenv("SUNPARK_TP1_PCT", "2.0"))
TP1_SELL_PCT = float(os.getenv("SUNPARK_TP1_SELL_PCT", "0.50"))
TP2_PCT = float(os.getenv("SUNPARK_TP2_PCT", "5.0"))
TP2_SELL_PCT = float(os.getenv("SUNPARK_TP2_SELL_PCT", "0.50"))
TRAIL_PCT = float(os.getenv("SUNPARK_TRAIL_PCT", "0.25"))
THESIS_BREAK_GRACE_S = float(os.getenv("SUNPARK_THESIS_BREAK_GRACE_S", "120"))
TIME_DEAD_S = float(os.getenv("SUNPARK_TIME_DEAD_S", str(6 * 3600)))
DEAD_VOL_SOL = float(os.getenv("SUNPARK_DEAD_VOL_SOL", "0.5"))
TIME_MAX_S = float(os.getenv("SUNPARK_TIME_MAX_S", str(24 * 3600)))
BIG_SELL_EXIT_SOL = float(os.getenv("SUNPARK_BIG_SELL_EXIT_SOL", "2.0"))
SELL_BUY_RATIO_EXIT = float(os.getenv("SUNPARK_SELL_BUY_RATIO_EXIT", "2.0"))
BE_AFTER_TP1 = os.getenv("SUNPARK_BE_AFTER_TP1", "1") == "1"
DAILY_LOSS_LIMIT_PCT = float(os.getenv("SUNPARK_DAILY_LOSS_LIMIT_PCT", "6"))
WEEKLY_LOSS_LIMIT_PCT = float(os.getenv("SUNPARK_WEEKLY_LOSS_LIMIT_PCT", "18"))
MINT_EXIT_COOLDOWN_S = float(os.getenv("SUNPARK_MINT_EXIT_COOLDOWN_S", "300"))


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


class Position:
    def __init__(self, mint, entry_price_sol, entry_time, size_sol, entry_source="rollup"):
        self.mint = mint
        self.entry_price_sol = entry_price_sol
        self.entry_time = entry_time
        self.initial_size_sol = size_sol
        self.size_sol = size_sol
        self.peak_price_sol = entry_price_sol
        self.state = "open"  # open -> tp1 -> trailing -> closed
        self.entry_source = entry_source  # "jupiter" or "rollup"

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
        self.exit_cooldowns = {}
        self._restore()

    def _restore(self):
        try:
            saved_balance = load_paper_state("balance_sol")
            if saved_balance is not None:
                self.balance_sol = float(saved_balance)
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
            self._prune_old_trades()
        except Exception:
            pass

    def _prune_old_trades(self):
        from storage import get_db
        try:
            connection = get_db()
            try:
                connection.execute(
                    "DELETE FROM paper_trades WHERE id NOT IN "
                    "(SELECT id FROM paper_trades ORDER BY id DESC LIMIT 1000)"
                )
                connection.commit()
            finally:
                connection.close()
        except Exception:
            pass

    def _record(self, mint, action, price_sol, sol_value, pnl_sol, reason, entry_source="rollup"):
        is_wash = 0
        if action in ("close", "tp1", "tp2"):
            try:
                from intel import wash_trade_suspicion
                from stats import rollup as _rollup
                snap = _rollup.stats_for(mint) or {}
                wash = wash_trade_suspicion(snap)
                if wash["wash_share"] >= 0.20 and wash["total_swaps"] >= 10:
                    is_wash = 1
            except Exception:
                pass
        trade = {
            "mint": mint,
            "action": action,
            "price_sol": price_sol,
            "sol_value": round(sol_value, 4),
            "pnl_sol": round(pnl_sol, 4),
            "reason": reason,
            "is_wash": is_wash,
            "entry_source": entry_source,
            "created_at": utc_now(),
        }
        self.trades.append(trade)
        try:
            save_paper_trade(trade)
        except Exception:
            pass

    def _save_balance(self):
        try:
            save_paper_state("balance_sol", round(self.balance_sol, 4))
        except Exception:
            pass

    def _realized_since(self, since):
        total = 0.0
        for item in self.closed:
            created = _parse_iso(item.get("created_at"))
            if created is None or created < since:
                continue
            total += item.get("pnl_sol") or 0.0
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
            append_activity(
                "warn", "exits", "circuit breaker tripped",
                {"daily_pct": info["daily_loss_pct"], "weekly_pct": info["weekly_loss_pct"], "daily_halt": info["daily_halt"], "weekly_halt": info["weekly_halt"], "open": len(self.positions)},
            )
            return info
        return None

    def open_position(self, mint, entry_price_sol, entry_time=None, entry_reason="entry", entry_source="rollup"):
        with self.lock:
            if self.halt_trading():
                return False
            if mint in self.positions:
                return False
            if len(self.positions) >= MAX_POSITIONS:
                return False
            cooldown_until = self.exit_cooldowns.get(mint, 0)
            if time.time() < cooldown_until:
                return False
            size = min(PAPER_ENTRY_SOL, self.balance_sol)
            if size <= 0:
                return False
            position = Position(mint, entry_price_sol, entry_time or time.time(), size, entry_source=entry_source)
            self.positions[mint] = position
            self.balance_sol -= size
            self._record(mint, "open", entry_price_sol, size, 0.0, entry_reason, entry_source=entry_source)
            self._save_balance()
            append_activity(
                "info", "exits", "paper entry opened",
                {"mint": mint[:16], "price": round(entry_price_sol, 10), "size": round(size, 4), "reason": entry_reason, "source": entry_source, "balance": round(self.balance_sol, 4)},
            )
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

    def _close(self, position, price_sol, reason, now, fill_price=None):
        if fill_price is None:
            fill_price = self._fill_price(position.mint, position.size_sol, price_sol)
        value = position.size_sol * (fill_price / position.entry_price_sol) if position.entry_price_sol else 0.0
        pnl = value - position.size_sol
        self.balance_sol += value
        self._record(position.mint, "close", fill_price, value, pnl, reason, entry_source=getattr(position, "entry_source", "rollup"))
        self._save_balance()
        level = "warn" if reason in ("stop_loss", "circuit_breaker", "dead_capital", "dev_sell") else "info"
        append_activity(
            level, "exits", f"paper closed: {reason}",
            {"mint": position.mint[:16], "reason": reason, "entry": round(position.entry_price_sol, 10), "exit": round(fill_price, 10), "pnl_sol": round(pnl, 4), "pnl_pct": round((fill_price / position.entry_price_sol - 1) * 100, 2) if position.entry_price_sol else 0, "size": round(position.size_sol, 4), "balance": round(self.balance_sol, 4)},
        )
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
        self.exit_cooldowns[position.mint] = time.time() + MINT_EXIT_COOLDOWN_S

    def _sell_share(self, position, price_sol, share, action, now, fill_price=None):
        if share <= 0 or position.size_sol <= 0:
            return
        if fill_price is None:
            fill_price = self._fill_price(position.mint, position.size_sol, price_sol)
        original_size = position.size_sol
        value = original_size * share * (fill_price / position.entry_price_sol)
        position.size_sol = original_size * (1 - share)
        self.balance_sol += value
        pnl = value - original_size * share
        self._record(position.mint, action, fill_price, value, pnl, action, entry_source=getattr(position, "entry_source", "rollup"))
        self._save_balance()
        append_activity(
            "info", "exits", f"paper {action}",
            {"mint": position.mint[:16], "action": action, "share": round(share * 100), "fill": round(fill_price, 10), "pnl_sol": round(pnl, 4), "remaining": round(position.size_sol, 4), "balance": round(self.balance_sol, 4)},
        )
        try:
            save_paper_position(position.to_row())
        except Exception:
            pass

    def evaluate(self, snapshots_by_mint, now=None):
        now = now or time.time()
        actions = []

        with self.lock:
            breaker = self.halt_trading(now)
            if breaker:
                for mint in list(self.positions):
                    position = self.positions[mint]
                    price = (snapshots_by_mint.get(mint) or {}).get("price_sol")
                    if price and price > 0:
                        actions.append(("close", position, price, "circuit_breaker", None))
                if actions:
                    for action_type, pos, price, reason, extra in actions:
                        if pos.mint in self.positions:
                            self._close(pos, price, reason, now)
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
                    actions.append(("close", position, price, "max_hold", None))
                    continue
                if age > TIME_DEAD_S and vol < DEAD_VOL_SOL:
                    actions.append(("close", position, price, "dead_capital", None))
                    continue
                if price <= position.entry_price_sol * (1 - STOP_LOSS_PCT):
                    actions.append(("close", position, price, "stop_loss", None))
                    continue
                if (
                    position.state == "open"
                    and position.peak_price_sol >= position.entry_price_sol * 1.2
                    and price <= position.entry_price_sol
                ):
                    actions.append(("close", position, price, "be_floor", None))
                    continue

                if age > THESIS_BREAK_GRACE_S:
                    pressure = distribution_pressure(snapshot)
                    if (
                        pressure
                        and (
                            (pressure["big_sells"] >= 1 and pressure["smart_sell_sol"] > 0)
                            or pressure["sell_buy_ratio"] >= SELL_BUY_RATIO_EXIT
                        )
                    ):
                        actions.append(("close", position, price, "thesis_break", None))
                        continue

                creator = (get_token_registry(position.mint) or {}).get("creator")
                if creator and dev_sell(snapshot, creator)["dev_sold"]:
                    actions.append(("close", position, price, "dev_sell", None))
                    continue

                if position.state == "trailing":
                    if price <= position.peak_price_sol * (1 - TRAIL_PCT):
                        actions.append(("close", position, price, "trailing_stop", None))
                    continue

                if position.state == "tp1":
                    if price >= position.entry_price_sol * TP2_PCT:
                        actions.append(("tp2", position, price, "tp2", TP2_SELL_PCT))
                    elif price <= position.entry_price_sol * (1.0 if BE_AFTER_TP1 else 0.95):
                        actions.append(("close", position, price, "be_stop" if BE_AFTER_TP1 else "giveback_stop", None))
                    continue

                if price >= position.entry_price_sol * TP1_PCT:
                    actions.append(("tp1", position, price, "tp1", TP1_SELL_PCT))

        if not actions:
            return

        fills = {}
        for action_type, pos, price, reason, extra in actions:
            if pos.mint in self.positions:
                fills[pos.mint] = self._fill_price(pos.mint, pos.size_sol, price)

        with self.lock:
            for action_type, pos, price, reason, extra in actions:
                if pos.mint not in self.positions:
                    continue
                fill = fills.get(pos.mint)
                if action_type == "close":
                    self._close(pos, price, reason, now, fill_price=fill)
                elif action_type == "tp1":
                    pos.state = "tp1"
                    self._sell_share(pos, price, extra, "tp1", now, fill_price=fill)
                elif action_type == "tp2":
                    pos.state = "trailing"
                    self._sell_share(pos, price, extra, "tp2", now, fill_price=fill)

    def _db_closed_trades(self):
        """Read all close/partial trades from DB (survives restarts)."""
        from storage import load_paper_trades
        trades = load_paper_trades(limit=2000)
        return [t for t in trades if t.get("action") in ("close", "tp1", "tp2")]

    def summary(self):
        closed = self._db_closed_trades()
        realized = sum(t.get("pnl_sol", 0) for t in closed)
        return {
            "balance_sol": round(self.balance_sol, 2),
            "open_positions": len(self.positions),
            "closed_count": len(closed),
            "realized_pnl_sol": round(realized, 2),
            "win_count": sum(1 for t in closed if t.get("pnl_sol", 0) > 0),
            "closed": closed,
        }

    def honest_summary(self):
        """PnL adjusted for wash trades, phantom TP2s, slippage, and Jupiter fees.

        Jupiter-quoted trades already have slippage in the fill price (both
        entry and exit), so we only subtract the fee.  Rollup-fallback trades
        need the full slippage + fee haircut because the rollup price ignores
        market impact.
        """
        SLIPPAGE_PCT = float(os.getenv("SUNPARK_JUPITER_SLIPPAGE_BPS", "3000")) / 10000.0
        FEE_PCT = float(os.getenv("SUNPARK_JUPITER_FEE_BPS", "30")) / 10000.0

        closed = self._db_closed_trades()
        honest_realized = 0.0
        wash_realized = 0.0
        phantom_realized = 0.0
        honest_wins = 0
        honest_count = 0
        total_raw = 0.0
        total_raw_wins = 0
        wash_mints = set()

        for t in closed:
            mint = t["mint"]
            raw_pnl = t.get("pnl_sol", 0)
            is_wash = t.get("is_wash", 0)
            is_phantom = t.get("is_phantom", 0)
            total_raw += raw_pnl
            if raw_pnl > 0:
                total_raw_wins += 1

            if is_wash:
                wash_mints.add(mint)

            if t.get("entry_source") == "jupiter":
                adj_pnl = raw_pnl * (1 - FEE_PCT)
            else:
                adj_pnl = raw_pnl * (1 - SLIPPAGE_PCT) / (1 + SLIPPAGE_PCT) * (1 - FEE_PCT)

            if is_wash:
                wash_realized += adj_pnl
            elif is_phantom:
                phantom_realized += adj_pnl
            else:
                honest_realized += adj_pnl
                honest_count += 1
                if adj_pnl > 0:
                    honest_wins += 1

        honest_balance = PAPER_START_SOL + honest_realized
        return {
            "balance_sol": round(self.balance_sol, 2),
            "honest_balance_sol": round(honest_balance, 2),
            "open_positions": len(self.positions),
            "closed_count": len(closed),
            "honest_closed_count": honest_count,
            "wash_count": len(wash_mints),
            "phantom_count": sum(1 for t in closed if t.get("is_phantom")),
            "realized_pnl_sol": round(total_raw, 2),
            "honest_pnl_sol": round(honest_realized, 2),
            "wash_pnl_sol": round(wash_realized, 2),
            "phantom_pnl_sol": round(phantom_realized, 2),
            "win_count": total_raw_wins,
            "honest_win_count": honest_wins,
            "slippage_pct": round(SLIPPAGE_PCT * 100, 1),
            "fee_pct": round(FEE_PCT * 100, 2),
            "closed": closed,
        }


paper = PaperAccount()
