"""Deterministic ranking of flow survivors into a small picks board.

Mechanics only: rank is computed from the rollup + intel; DeepSeek is never
part of the score. `compute_rankings` feeds the Top-5 and the AI last-pass.
"""

import os
import time

from filters import selection_gate
from intel import assess as intel_assess
from stats import rollup

TOP_PICKS = int(os.getenv("SUNPARK_TOP_PICKS", "5"))
SCAN_LIMIT = int(os.getenv("SUNPARK_RANK_SCAN_LIMIT", "60"))


def _clamp(value, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, value))


def score_mint(mint, snapshot, intel_block, now=None):
    """0-100 momentum+quality score. Higher is better."""
    window = (snapshot.get("windows") or {}).get("300") or {}
    vol = window.get("vol_sol") or 0
    buyers = window.get("unique_buyers") or 0
    buy_sol = window.get("buy_sol") or 0
    sell_sol = window.get("sell_sol") or 0
    net = window.get("net_sol") or 0
    ratio = (buy_sol / sell_sol) if sell_sol and sell_sol > 0 else 0.0

    score = 0.0
    parts = []
    volume_score = _clamp(vol, 0, 50) / 50 * 30
    score += volume_score
    parts.append(f"vol={volume_score:.0f}")
    buyer_score = _clamp(buyers, 0, 50) / 50 * 25
    score += buyer_score
    parts.append(f"buyers={buyer_score:.0f}")
    ratio_score = _clamp(ratio, 0, 3) / 3 * 15
    score += ratio_score
    parts.append(f"ratio={ratio_score:.0f}")
    net_score = _clamp(net, 0, 20) / 20 * 10
    score += net_score
    parts.append(f"net={net_score:.0f}")

    quality = (intel_block or {}).get("quality", 0)
    quality_score = quality / 100 * 10
    score += quality_score
    parts.append(f"quality={quality:.0f}")

    whale = (intel_block or {}).get("whale_inflow") or {}
    convergence = bool(whale.get("convergence"))
    whale_bonus = min(
        whale.get("smart_buyers", 0) * 3
        + whale.get("big_buys", 0) * 1
        + (5 if convergence else 0),
        15,
    )
    score += whale_bonus
    parts.append(f"whale={whale_bonus:.0f}" + ("+conv" if convergence else ""))

    return round(score, 2), parts


def compute_rankings(limit=None):
    """Return ranked picks list; only gate-clean mints with flow survive."""
    limit = limit or TOP_PICKS
    now = time.time()
    rows = rollup.top_mints(300, limit=SCAN_LIMIT, timestamp=now)
    ranked = []
    for row in rows:
        mint = row["mint"]
        snapshot = rollup.stats_for(mint, now)
        if not snapshot:
            continue
        intel_block = intel_assess({"primary_mint": mint, "stats": snapshot})
        card = {"primary_mint": mint, "stats": snapshot, "intel": intel_block, "event_category": "swap"}
        allowed, reasons = selection_gate({"event_category": "swap"}, card)
        if not allowed:
            continue
        score, parts = score_mint(mint, snapshot, intel_block, now)
        window = (snapshot.get("windows") or {}).get("300") or {}
        whale = intel_block.get("whale_inflow") or {}
        smart_line = f"smart:{whale.get('smart_buyers')}" + ("+conv" if whale.get("convergence") else "")
        ranked.append(
            {
                "mint": mint,
                "score": score,
                "reasons": [
                    f"{mint[:8]}:{window.get('vol_sol') or 0:.1f} SOL 5m",
                    f"buyers:{window.get('unique_buyers') or 0}",
                    f"intel:{intel_block.get('quality')}",
                    smart_line,
                ],
                "details": " ".join(parts),
                "intel_quality": intel_block.get("quality"),
                "whale_inflow": intel_block.get("whale_inflow"),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    for index, pick in enumerate(ranked[:limit], start=1):
        pick["rank"] = index
    return ranked[:limit]
