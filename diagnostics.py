"""Read-only funnel diagnostics for SunPark.

Runs against SUNPARK_DB_PATH and prints a report of the candidate funnel:
events -> mints -> how many would survive strict selection floors. This
script changes nothing; it exists to set floor defaults with real numbers.

Usage:
    python diagnostics.py [--hours 24]
"""

import argparse
import json
import os
import sys

from storage import get_db, load_mint_stats

DAY = 24 * 3600


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    return int(_env_float(name, default))


def _hours_ago_query(hours):
    return "datetime('now', ?)"  # placeholder string, replaced by caller


def _card(row):
    try:
        return json.loads(row) if row else {}
    except (TypeError, ValueError):
        return {}


def _window_stats(card):
    stats = card.get("stats") or {}
    return (stats.get("windows") or {}).get("300") or stats


def overview(connection, hours):
    since = hours * DAY / 3600.0
    rows = connection.execute(
        "SELECT event_type, received_at, payload_json, analysis_json "
        "FROM candidates WHERE datetime(received_at) >= datetime('now', ?)",
        (f"-{int(since)} hours",),
    ).fetchall()

    total = len(rows)
    categories = {}
    sources = {}
    mints = set()
    timed = []
    for event_type, received_at, payload_json, analysis_json in rows:
        payload = json.loads(payload_json)
        card = _card(analysis_json)
        category = card.get("event_category") or payload.get("event_category") or event_type or "other"
        categories[category] = categories.get(category, 0) + 1
        source = card.get("source") or payload.get("source") or "?"
        sources[source] = sources.get(source, 0) + 1
        mint = card.get("primary_mint") or payload.get("primary_mint")
        if mint:
            mints.add(mint)
        timed.append((received_at, category, card))
    return total, categories, sources, mints, timed


def print_overview(total, categories, sources, mints, hours):
    print(f"== Candidates in last {hours}h ==")
    print(f"total:        {total}")
    print(f"distinct mints: {len(mints)}")
    print("by category:")
    for category, count in sorted(categories.items(), key=lambda item: -item[1]):
        print(f"  {category:<18} {count:>6}")
    print("by source:")
    for source, count in sorted(sources.items(), key=lambda item: -item[1]):
        print(f"  {source:<12} {count:>6}")


def funnel(timed):
    print("\n== Funnel: would-pass under strict floors (card stats) ==")
    floors = [
        ("buyers>=10", {"min_buyers": 10}),
        ("buyers>=20", {"min_buyers": 20}),
        ("buyers>=50", {"min_buyers": 50}),
        ("vol5m>=5 SOL", {"min_vol": 5}),
        ("vol5m>=10 SOL", {"min_vol": 10}),
        ("vol5m>=25 SOL", {"min_vol": 25}),
        ("buy_ratio>=1.0", {"min_ratio": 1.0}),
        ("age<=15m", {"max_age_min": 15}),
        ("age<=30m", {"max_age_min": 30}),
        ("ALL (10/5/1.0/15)", {"min_buyers": 10, "min_vol": 5, "min_ratio": 1.0, "max_age_min": 15}),
        ("ALL (20/10/1.0/15)", {"min_buyers": 20, "min_vol": 10, "min_ratio": 1.0, "max_age_min": 15}),
        ("ALL (5/5/1.0/30)", {"min_buyers": 5, "min_vol": 5, "min_ratio": 1.0, "max_age_min": 30}),
    ]
    cards_with_stats = [card for _, _, card in timed if card.get("stats")]
    print(f"candidates with stats at processing time: {len(cards_with_stats)}")
    if not cards_with_stats:
        print("no stats available yet - pipeline may need to run longer")
        return
    for label, rules in floors:
        passed = 0
        for card in cards_with_stats:
            stats = card.get("stats") or {}
            window = _window_stats(card)
            age = stats.get("age_seconds")
            if rules.get("max_age_min") and age is not None and age / 60 > rules["max_age_min"]:
                continue
            if rules.get("min_buyers") and window.get("unique_buyers", 0) < rules["min_buyers"]:
                continue
            if rules.get("min_vol") and window.get("vol_sol", 0) < rules["min_vol"]:
                continue
            if rules.get("min_ratio") and window.get("sell_sol", 0) > 0:
                if window.get("buy_sol", 0) / window.get("sell_sol", 0) < rules["min_ratio"]:
                    continue
            passed += 1
        print(f"  {label:<22} {passed:>5}")


def decisions(connection, hours):
    print("\n== Track decisions (last 24h) ==")
    rows = connection.execute(
        "SELECT track, signal, status, COUNT(*), AVG(latency_ms) "
        "FROM track_decisions "
        "WHERE datetime(created_at) >= datetime('now', '-1 day') "
        "GROUP BY track, signal, status ORDER BY track, status, signal"
    ).fetchall()
    for track, signal, status, count, avg in rows:
        avg = f"{avg:.0f}ms" if avg else "-"
        print(f"  {track:<5} {signal:<8} {status:<9} {count:>6}  (avg {avg})")

    ai = connection.execute(
        "SELECT AVG(latency_ms), COUNT(*) FROM track_decisions "
        "WHERE track='ai' AND status='ok' "
        "AND datetime(created_at) >= datetime('now', '-1 day')"
    ).fetchone()
    ai_errors = connection.execute(
        "SELECT COUNT(*) FROM track_decisions WHERE track='ai' AND status='error'"
    ).fetchone()[0]
    if ai[1]:
        print(f"ai avg latency: {ai[0]:.0f}ms over {ai[1]} calls; total ai_errors={ai_errors}")


def live_mints():
    print("\n== Live rollup (mint_stats snapshot) ==")
    rows = load_mint_stats()
    if not rows:
        print("no live mints in mint_stats yet")
        return
    rows.sort(key=lambda row: (row.get("windows") or {}).get("300", {}).get("vol_sol", 0), reverse=True)
    for row in rows[:15]:
        window = (row.get("windows") or {}).get("300") or {}
        sell = window.get("sell_sol", 0) or 0
        ratio = window.get("buy_sol", 0) / sell if sell > 0 else float("inf")
        print(
            f"  {str(row.get('mint'))[:12]:<14} vol5m={window.get('vol_sol', 0):>8.1f} "
            f"buyers={window.get('unique_buyers', 0):>4} ratio={ratio:>5.2f}"
            f" age={int((row.get('age_seconds') or 0) / 60)}m"
        )
    strict = 0
    mb = _env_int("SUNPARK_MIN_BUYERS", 5)
    mv = _env_float("SUNPARK_MIN_VOL_5M_SOL", 5.0)
    mr = _env_float("SUNPARK_MIN_BUY_RATIO", 1.0)
    ma = _env_float("SUNPARK_MAX_AGE_MIN", 30.0)
    for row in rows:
        window = (row.get("windows") or {}).get("300") or {}
        if (
            window.get("unique_buyers", 0) >= mb
            and window.get("vol_sol", 0) >= mv
            and window.get("sell_sol", 0) > 0
            and window.get("buy_sol", 0) / window.get("sell_sol", 0) >= mr
            and (row.get("age_seconds") or 0) / 60 <= ma
        ):
            strict += 1
    print(
        f"  gate-floor survivors (env: {int(mb)} buyers / {mv:g} SOL / ratio>={mr:g} / age<={int(ma)}m): {strict}/{len(rows)}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()

    connection = get_db()
    try:
        total, categories, sources, mints, timed = overview(connection, args.hours)
        print_overview(total, categories, sources, mints, args.hours)
        funnel(timed)
        decisions(connection, args.hours)
    finally:
        connection.close()
    live_mints()
    print("\ndone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
