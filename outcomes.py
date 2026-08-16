"""Forward-outcome labeling for picks and paper trades.

Every pick and paper entry becomes an `outcomes` row at appearance time.
At +5m and +30m maturity the live rollup price is read and the forward
return is stored, so the funnel can be scored by mechanics (score band,
rank, mode) and AI signal without trusting hindsight. If the price is
unavailable at maturity the row resolves as `nodata` and is counted
separately, never guessed.
"""

import os
import time

from stats import rollup
from storage import (
    find_open_pick_outcome,
    find_outcome,
    insert_outcome,
    list_outcomes,
    list_unresolved_outcomes,
    set_paper_exit_reason,
    update_outcome,
)

OUTCOME_5M_S = int(os.getenv("SUNPARK_OUTCOME_5M_S", "300"))
OUTCOME_30M_S = int(os.getenv("SUNPARK_OUTCOME_30M_S", "1800"))
PEAK_WINDOW_S = 600


def _mode_from_reason(reason):
    if not reason:
        return "ai"
    if reason.startswith("entry_mech"):
        return "mechanical"
    if reason.startswith("entry_ai"):
        return "ai"
    return "ai"


def _created_ts(created_at):
    if not created_at:
        return None
    if isinstance(created_at, (int, float)):
        return float(created_at)
    try:
        from datetime import datetime, timezone

        return datetime.fromisoformat(str(created_at)).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _return_pct(entry_price, price):
    if not entry_price or not price:
        return None
    return round((price / entry_price - 1) * 100, 2)


def record_pick_outcome(pick):
    """Capture a pick the first time it appears (keeps first entry_time)."""
    mint = pick.get("mint")
    if not mint:
        return
    entry_price = pick.get("entry_price_sol")
    if not entry_price:
        entry_price = (rollup.stats_for(mint) or {}).get("price_sol")
    if not entry_price:
        return
    existing = find_open_pick_outcome(mint)
    if existing:
        return
    try:
        insert_outcome(
            {
                "mint": mint,
                "kind": "pick",
                "entry_time": time.time(),
                "entry_price_sol": entry_price,
                "ai_signal": pick.get("ai_signal"),
                "ai_confidence": pick.get("ai_confidence"),
                "mode": "pick",
                "score": pick.get("score"),
                "rank": pick.get("rank"),
            }
        )
    except Exception:
        pass


def record_paper_outcome(trade):
    """Capture a paper open (keyed by entry_time) or close (updates the row)."""
    mint = trade.get("mint")
    if not mint:
        return
    if trade.get("action") == "open":
        entry_time = _created_ts(trade.get("created_at")) or time.time()
        entry_price = trade.get("price_sol")
        if not entry_price:
            return
        if find_outcome(mint, "paper", entry_time):
            return
        try:
            insert_outcome(
                {
                    "mint": mint,
                    "kind": "paper",
                    "entry_time": entry_time,
                    "entry_price_sol": entry_price,
                    "mode": _mode_from_reason(trade.get("reason")),
                    "ai_signal": trade.get("ai_signal"),
                }
            )
        except Exception:
            pass
    elif trade.get("action") == "close":
        try:
            set_paper_exit_reason(mint, trade.get("reason"))
        except Exception:
            pass


def resolve_maturities(now=None):
    now = now if now is not None else time.time()
    changed = 0
    for row in list_unresolved_outcomes():
        mint = row["mint"]
        entry = row["entry_time"]
        entry_price = row.get("entry_price_sol")
        if not entry or not entry_price:
            continue
        age = now - entry
        live = rollup.stats_for(mint) or {}
        price = live.get("price_sol")
        updates = {}
        if row.get("price_5m") is None and age >= OUTCOME_5M_S:
            if price:
                updates["price_5m"] = price
            else:
                updates["price_5m"] = -1.0
                updates["return_5m_pct"] = None
        if row.get("price_30m") is None and age >= OUTCOME_30M_S:
            if price:
                updates["price_30m"] = price
            else:
                updates["price_30m"] = -1.0
                updates["return_30m_pct"] = None
        peak = row.get("peak_price_sol")
        if price and (peak is None or price > peak):
            updates["peak_price_sol"] = price
        if not updates:
            continue
        if updates.get("price_5m") not in (None, -1.0):
            updates["return_5m_pct"] = _return_pct(entry_price, updates["price_5m"])
        if updates.get("price_30m") not in (None, -1.0):
            updates["return_30m_pct"] = _return_pct(entry_price, updates["price_30m"])
        update_outcome(row["id"], **updates)
        changed += 1
        if updates.get("price_30m") is not None:
            has_30m = updates.get("price_30m") != -1.0
            update_outcome(
                row["id"],
                resolved="ok" if has_30m else "nodata",
                resolved_at=_utc_now(),
            )
    return changed


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _group_stats(rows):
    returns = [r for r in rows if r.get("return_30m_pct") is not None]
    resolved = [r for r in rows if r.get("resolved") == "ok"]
    wins = [r for r in resolved if (r.get("return_30m_pct") or 0) > 0]
    return {
        "samples": len(rows),
        "resolved": len(resolved),
        "nodata": sum(1 for r in rows if r.get("resolved") == "nodata"),
        "win_rate_pct": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
        "avg_return_30m_pct": round(sum(r["return_30m_pct"] for r in returns) / len(returns), 2) if returns else None,
        "median_return_30m_pct": _median([r["return_30m_pct"] for r in returns]),
    }


def outcomes_summary(rows=None):
    rows = rows if rows is not None else list_outcomes()
    by_kind = {}
    by_mode = {}
    by_ai = {}
    by_rank = {}
    by_exit = {}
    for row in rows:
        kind = row.get("kind") or "?"
        by_kind.setdefault(kind, []).append(row)
        mode = row.get("mode") or "?"
        by_mode.setdefault(mode, []).append(row)
        ai = row.get("ai_signal") or "none"
        by_ai.setdefault(ai, []).append(row)
        rank = row.get("rank")
        bucket = f"r{rank}" if rank is not None else "? "
        by_rank.setdefault(bucket, []).append(row)
        exit_reason = row.get("exit_reason") or "open"
        by_exit.setdefault(exit_reason, []).append(row)
    return {
        "summary": _group_stats(rows),
        "by_kind": {key: _group_stats(value) for key, value in by_kind.items()},
        "by_mode": {key: _group_stats(value) for key, value in by_mode.items()},
        "by_ai": {key: _group_stats(value) for key, value in by_ai.items()},
        "by_rank": {key: _group_stats(value) for key, value in sorted(by_rank.items(), key=lambda item: item[0])},
        "by_exit": {key: _group_stats(value) for key, value in by_exit.items()},
    }
