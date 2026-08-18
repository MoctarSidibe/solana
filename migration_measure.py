"""Phase 0 migration measurement: query DB for graduation win rates.

Usage:
    python migration_measure.py

Reads the live SQLite database and computes:
  - How many tokens graduated (status = migrated)
  - Forward return at +5m and +30m for graduated tokens
  - Win rate with and without the dev quiet-period filter
  - Break-even analysis: does migration arb have positive expectancy?

No money risked. Pure measurement from historical data.
"""

import os
import sqlite3
import time

DB_PATH = os.getenv("SUNPARK_DB_PATH", "data/events.sqlite")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _graduated_tokens(conn):
    """All tokens that have graduated (status = migrated) with outcomes."""
    rows = conn.execute("""
        SELECT
            r.mint,
            r.creator,
            r.created_at,
            r.graduated_at,
            r.status,
            o.entry_time,
            o.entry_price_sol,
            o.price_5m,
            o.price_30m,
            o.return_5m_pct,
            o.return_30m_pct,
            o.peak_price_sol,
            o.peak_pct,
            o.kind
        FROM token_registry r
        LEFT JOIN outcomes o ON r.mint = o.mint AND o.kind = 'pick'
        WHERE r.status = 'migrated'
        ORDER BY r.graduated_at DESC
    """).fetchall()
    return rows


def _creator_history(conn, creator):
    """Get creator's prior graduations count and quiet days."""
    if not creator:
        return 0, None
    row = conn.execute("""
        SELECT COUNT(*) as grad_count,
               MAX(graduated_at) as latest_grad
        FROM token_registry
        WHERE creator = ? AND status = 'migrated'
    """, (creator,)).fetchone()
    grad_count = row["grad_count"] or 0
    latest_grad = row["latest_grad"]
    quiet_days = None
    if latest_grad:
        quiet_days = (time.time() - latest_grad) / 86400
    return grad_count, quiet_days


def _dev_passes(creator, quiet_days):
    """Dev quiet-period filter: has >= 1 prior graduation AND >= 7 days quiet."""
    if quiet_days is None:
        return False
    return quiet_days >= 7.0


def _print_section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _pct(num, den):
    if not den:
        return 0.0
    return round(num / den * 100, 1)


def _median(values):
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2 == 0:
        return round((clean[mid - 1] + clean[mid]) / 2, 2)
    return round(clean[mid], 2)


def _avg(values):
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 2)


def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Start the stream + worker first to collect data.")
        return

    conn = _connect()
    try:
        _print_section("Phase 0: Migration Arb Measurement Report")

        # Total tokens tracked
        total = conn.execute("SELECT COUNT(*) as c FROM token_registry").fetchone()["c"]
        migrated = conn.execute("SELECT COUNT(*) as c FROM token_registry WHERE status = 'migrated'").fetchone()["c"]
        print(f"\n  Total tokens tracked:          {total}")
        print(f"  Graduated (migrated):          {migrated}")
        print(f"  Graduation rate:               {_pct(migrated, total)}%")

        rows = _graduated_tokens(conn)
        has_outcome = [r for r in rows if r["entry_time"] is not None]
        no_outcome = [r for r in rows if r["entry_time"] is None]

        print(f"\n  Graduated with outcome data:   {len(has_outcome)}")
        print(f"  Graduated without outcome:     {len(no_outcome)}")

        if not has_outcome:
            print("\n  No graduated tokens with outcome data yet.")
            print("  Let the system run to collect migration outcomes.")
            print("  The worker records outcomes for every pick it sees.")
            return

        _print_section("Outcome Summary (all graduated tokens)")

        # Win rate at 2x (100% return)
        wins_2x = [r for r in has_outcome if r["return_30m_pct"] is not None and r["return_30m_pct"] >= 100]
        wins_3x = [r for r in has_outcome if r["return_30m_pct"] is not None and r["return_30m_pct"] >= 200]
        peak_2x = [r for r in has_outcome if r["peak_pct"] is not None and r["peak_pct"] >= 100]

        returns_5m = [r["return_5m_pct"] for r in has_outcome if r["return_5m_pct"] is not None]
        returns_30m = [r["return_30m_pct"] for r in has_outcome if r["return_30m_pct"] is not None]
        peaks = [r["peak_pct"] for r in has_outcome if r["peak_pct"] is not None]
        nodata_5m = sum(1 for r in has_outcome if r["price_5m"] is not None and r["price_5m"] < 0)
        nodata_30m = sum(1 for r in has_outcome if r["price_30m"] is not None and r["price_30m"] < 0)

        print(f"\n  Win rates:")
        print(f"    Peak reached 2x+:            {len(peak_2x)}/{len(has_outcome)} ({_pct(len(peak_2x), len(has_outcome))}%)")
        print(f"    Still 2x+ at +30m:           {len(wins_2x)}/{len(has_outcome)} ({_pct(len(wins_2x), len(has_outcome))}%)")
        print(f"    Still 3x+ at +30m:           {len(wins_3x)}/{len(has_outcome)} ({_pct(len(wins_3x), len(has_outcome))}%)")

        print(f"\n  Returns:")
        print(f"    +5m avg:                     {_avg(returns_5m)}%")
        print(f"    +5m median:                  {_median(returns_5m)}%")
        print(f"    +30m avg:                    {_avg(returns_30m)}%")
        print(f"    +30m median:                 {_median(returns_30m)}%")
        print(f"    Peak avg:                    {_avg(peaks)}%")
        print(f"    Peak median:                 {_median(peaks)}%")

        print(f"\n  No-data (price unavailable at maturity):")
        print(f"    +5m:                         {nodata_5m}")
        print(f"    +30m:                        {nodata_30m}")

        # Dev filter analysis
        _print_section("Dev Quiet-Period Filter Analysis")

        dev_pass = []
        dev_fail = []
        no_creator = []

        for r in has_outcome:
            creator = r["creator"]
            if not creator:
                no_creator.append(r)
                continue
            grad_count, quiet_days = _creator_history(conn, creator)
            if _dev_passes(creator, quiet_days):
                r["_dev_pass"] = True
                r["_quiet_days"] = quiet_days
                r["_prior_grads"] = grad_count
                dev_pass.append(r)
            else:
                r["_dev_pass"] = False
                r["_quiet_days"] = quiet_days
                r["_prior_grads"] = grad_count
                dev_fail.append(r)

        print(f"\n  Dev filter pass (>=1 prior grad, >=7d quiet):  {len(dev_pass)}")
        print(f"  Dev filter fail:                              {len(dev_fail)}")
        print(f"  No creator data:                              {len(no_creator)}")

        if dev_pass:
            dp_2x = sum(1 for r in dev_pass if r.get("peak_pct") and r["peak_pct"] >= 100)
            dp_30m_2x = sum(1 for r in dev_pass if r.get("return_30m_pct") and r["return_30m_pct"] >= 100)
            dp_returns = [r["return_30m_pct"] for r in dev_pass if r["return_30m_pct"] is not None]
            dp_peaks = [r["peak_pct"] for r in dev_pass if r["peak_pct"] is not None]
            print(f"\n  Filter PASS results:")
            print(f"    Peak 2x+:                    {dp_2x}/{len(dev_pass)} ({_pct(dp_2x, len(dev_pass))}%)")
            print(f"    2x+ at +30m:                 {dp_30m_2x}/{len(dev_pass)} ({_pct(dp_30m_2x, len(dev_pass))}%)")
            print(f"    +30m avg:                    {_avg(dp_returns)}%")
            print(f"    +30m median:                 {_median(dp_returns)}%")
            print(f"    Peak avg:                    {_avg(dp_peaks)}%")

        if dev_fail:
            df_2x = sum(1 for r in dev_fail if r.get("peak_pct") and r["peak_pct"] >= 100)
            df_30m_2x = sum(1 for r in dev_fail if r.get("return_30m_pct") and r["return_30m_pct"] >= 100)
            df_returns = [r["return_30m_pct"] for r in dev_fail if r["return_30m_pct"] is not None]
            df_peaks = [r["peak_pct"] for r in dev_fail if r["peak_pct"] is not None]
            print(f"\n  Filter FAIL results:")
            print(f"    Peak 2x+:                    {df_2x}/{len(dev_fail)} ({_pct(df_2x, len(dev_fail))}%)")
            print(f"    2x+ at +30m:                 {df_30m_2x}/{len(dev_fail)} ({_pct(df_30m_2x, len(dev_fail))}%)")
            print(f"    +30m avg:                    {_avg(df_returns)}%")
            print(f"    +30m median:                 {_median(df_returns)}%")
            print(f"    Peak avg:                    {_avg(df_peaks)}%")

        # Break-even analysis
        _print_section("Break-Even Analysis")

        # Simple model: fixed entry size, 50% exit at 2x, rest trail
        # Break-even win rate with 2:1 reward:risk = ~33% (need 2x win to offset 1x loss)
        # With 50% sell at 2x, actual break-even is lower

        all_30m = [r["return_30m_pct"] for r in has_outcome if r["return_30m_pct"] is not None]
        if all_30m:
            avg_return = sum(all_30m) / len(all_30m)
            positive = sum(1 for r in all_30m if r > 0)
            negative = sum(1 for r in all_30m if r < 0)
            print(f"\n  Average +30m return:           {round(avg_return, 2)}%")
            print(f"  Positive outcomes:             {positive}/{len(all_30m)} ({_pct(positive, len(all_30m))}%)")
            print(f"  Negative outcomes:             {negative}/{len(all_30m)} ({_pct(negative, len(all_30m))}%)")
            if avg_return > 0:
                print(f"\n  >>> POSITIVE EXPECTANCY: avg +30m return is +{round(avg_return, 2)}%")
                print(f"  >>> Migration arb appears profitable with current data.")
            else:
                print(f"\n  >>> NEGATIVE EXPECTANCY: avg +30m return is {round(avg_return, 2)}%")
                print(f"  >>> Migration arb is NOT profitable with current data.")
                print(f"  >>> Need dev filter OR larger sample before risking money.")

        # Recent performance
        _print_section("Recent Graduations (last 10)")
        recent = has_outcome[:10]
        for r in recent:
            mint = r["mint"][:12]
            ret5 = f"{r['return_5m_pct']:.1f}%" if r["return_5m_pct"] is not None else "n/a"
            ret30 = f"{r['return_30m_pct']:.1f}%" if r["return_30m_pct"] is not None else "n/a"
            peak = f"{r['peak_pct']:.1f}%" if r["peak_pct"] is not None else "n/a"
            dev_pass = "DEV_OK" if r.get("_dev_pass") else ("DEV_FAIL" if r.get("_quiet_days") is not None else "no_data")
            quiet = f"{r.get('_quiet_days', '?')}d" if r.get("_quiet_days") is not None else "?"
            print(f"  {mint}...  +5m:{ret5:>8}  +30m:{ret30:>8}  peak:{peak:>8}  {dev_pass} ({quiet})")

        _print_section("Verdict")
        if not all_30m:
            print("  INSUFFICIENT DATA — need more graduated tokens with outcomes.")
            print("  Let the system run for a few days.")
        elif avg_return > 10 and _pct(len(wins_2x), len(has_outcome)) > 10:
            print("  PROCEED to Phase 1 (paper trading with real Jupiter quotes).")
            print(f"  Win rate at 2x: {_pct(len(wins_2x), len(has_outcome))}%, avg return: {round(avg_return, 2)}%")
        elif avg_return > 0:
            print("  MARGINAL — dev filter may improve. Collect more data.")
            print(f"  Win rate at 2x: {_pct(len(wins_2x), len(has_outcome))}%, avg return: {round(avg_return, 2)}%")
        else:
            print("  STOP — negative expectancy. Do not risk money.")
            print(f"  Avg return: {round(avg_return, 2)}%")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
