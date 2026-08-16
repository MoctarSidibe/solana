def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_float(name, default):
    import os

    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


from intel import assess as intel_assess  # noqa: E402


def categorize_event(event):
    """Map Helius labels and transfer facts to a strategy category."""
    event_type = (event.get("event_type") or "").lower()
    raw = event.get("raw") or {}
    labels = f"{event_type} {raw.get('type', '')}".lower()
    token_transfers = event.get("token_transfers") or []
    native_transfers = event.get("native_transfers") or []

    if any(term in labels for term in ("liquidity", "pool", "initialize", "addliquidity", "migration", "graduation")):
        return "liquidity"
    if any(term in labels for term in ("token_mint", "mint", "create_token", "token_creation", "pump_create")):
        return "token_creation"
    if any(term in labels for term in ("swap", "buy", "sell", "trade")):
        return "swap"
    if "volume" in labels:
        return "volume"
    if any(term in labels for term in ("whale", "smart_money")):
        return "whale_trade"
    if token_transfers:
        return "token_transfer"

    native_lamports = sum(
        value
        for value in (_number(item.get("amount")) for item in native_transfers if isinstance(item, dict))
        if value is not None
    )
    if native_lamports >= 10_000_000_000:
        return "large_sol_transfer"
    if native_transfers:
        return "sol_transfer"
    return "other"


def primary_mint(event):
    quote_mints = {
        "So11111111111111111111111111111111111111112",
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    }
    facts = [
        item for item in (event.get("token_transfers") or [])
        if isinstance(item, dict) and item.get("mint") not in quote_mints
    ]
    if not facts:
        return next(iter(event.get("token_mints") or []), None)
    return max(facts, key=lambda item: _number(item.get("tokenAmount", item.get("amount"))) or 0).get("mint")


def build_analysis_card(event, metadata=None):
    """Create a bounded, consistent fact set for rules and DeepSeek."""
    token_transfers = event.get("token_transfers") or []
    native_transfers = event.get("native_transfers") or []
    mints = list(event.get("token_mints") or [])
    token_facts = []
    for transfer in token_transfers[:20]:
        if not isinstance(transfer, dict):
            continue
        mint = transfer.get("mint")
        if mint and mint not in mints:
            mints.append(mint)
        token_facts.append(
            {
                "mint": mint,
                "amount_raw": _number(transfer.get("tokenAmount", transfer.get("amount"))),
                "amount_ui": (
                    _number(transfer.get("tokenAmount", transfer.get("amount")))
                    / 10 ** transfer["decimals"]
                    if transfer.get("decimals") is not None
                    and _number(transfer.get("tokenAmount", transfer.get("amount"))) is not None
                    else None
                ),
                "decimals": transfer.get("decimals"),
                "from": transfer.get("fromUserAccount"),
                "to": transfer.get("toUserAccount"),
            }
        )

    lamports = [
        _number(transfer.get("amount"))
        for transfer in native_transfers[:20]
        if isinstance(transfer, dict)
    ]
    total_lamports = sum(value for value in lamports if value is not None)
    raw = event.get("raw") or {}
    mint = primary_mint(event)
    metadata = metadata or {}
    quality_flags = []
    if mint:
        quality_flags.append("has_primary_mint")
    if metadata.get("status") == "ok":
        quality_flags.append("has_metadata")
    if total_lamports >= 1_000_000_000:
        quality_flags.append("sol_above_1")
    stats = None
    if mint:
        try:
            from stats import rollup

            stats = rollup.stats_for(mint, event.get("timestamp"))
        except Exception:
            stats = None
    card = {
        "signature": event.get("signature"),
        "slot": event.get("slot"),
        "timestamp": event.get("timestamp"),
        "event_type": event.get("event_type"),
        "event_category": event.get("event_category") or categorize_event(event),
        "source": event.get("source"),
        "fee_payer": event.get("fee_payer"),
        "primary_mint": mint,
        "name": metadata.get("name") or raw.get("name"),
        "symbol": metadata.get("symbol") or raw.get("symbol"),
        "decimals": metadata.get("decimals") or next(
            (item.get("decimals") for item in token_transfers if item.get("mint") == mint and item.get("decimals") is not None),
            None,
        ),
        "metadata_status": metadata.get("status", "not_requested"),
        "description": raw.get("description"),
        "token_mints": mints,
        "token_transfer_count": len(token_transfers),
        "token_transfers": token_facts,
        "native_transfer_count": len(native_transfers),
        "native_lamports": total_lamports,
        "native_sol": total_lamports / 1_000_000_000,
        "tradable": event.get("event_category") in {"liquidity", "swap"},
        "stats": stats,
        "quality_flags": quality_flags,
    }
    try:
        card["intel"] = intel_assess(card)
    except Exception:
        card["intel"] = None
    return card


def light_filter(event):
    """Remove only obvious junk before the AI track sees a candidate."""
    raw = event.get("raw") or {}
    if raw.get("transactionError") or raw.get("error"):
        return False, ["failed_transaction"]
    if not event.get("signature"):
        return False, ["missing_signature"]
    return True, []


def compact_stats(card):
    """Bounded summary of the rollup for prompts and dashboard rows."""
    stats = card.get("stats") or {}
    if not stats:
        return None
    window = (stats.get("windows") or {}).get("300") or stats
    return {
        "age_seconds": stats.get("age_seconds"),
        "total_sol_vol": stats.get("total_sol_vol"),
        "initial_liquidity_sol": stats.get("initial_liquidity_sol"),
        "buy_ratio_5m": (
            round(window["buy_sol"] / window["sell_sol"], 2)
            if window.get("sell_sol") and window.get("sell_sol") > 0
            else None
        ),
        "vol_5m_sol": window.get("vol_sol"),
        "net_5m_sol": window.get("net_sol"),
        "unique_buyers_5m": window.get("unique_buyers"),
        "unique_sellers_5m": window.get("unique_sellers"),
        "price_change_5m_pct": window.get("price_change_pct"),
    }


def selection_gate(event, card=None):
    """Strict pre-rank gate: hard-reject junk with explicit reasons.

    Rejects outright on watch-only categories, missing mints, any intel red
    flag, or flow stats below the configured floors. Every rejection reason
    is returned to callers so it can be persisted and counted.
    """
    card = card or build_analysis_card(event)
    reasons = []

    category = event.get("event_category") or card.get("event_category")
    if category == "token_creation":
        reasons.append("watch_only_creation")
    if category == "token_transfer" or category == "other":
        reasons.append("not_tradable_category")

    mint = card.get("primary_mint")
    if not mint:
        reasons.append("no_primary_mint")

    if not reasons:
        intel_block = card.get("intel") or {}
        for flag in intel_block.get("red_flags") or []:
            reasons.append(f"intel_{flag}")

    stats = card.get("stats") or {}
    window = (stats.get("windows") or {}).get("300") or {}
    min_vol = _env_float("SUNPARK_MIN_VOL_5M_SOL", 5.0)
    max_age = _env_float("SUNPARK_MAX_AGE_MIN", 30.0)
    min_buyers = int(_env_float("SUNPARK_MIN_BUYERS", 5))
    min_ratio = _env_float("SUNPARK_MIN_BUY_RATIO", 1.0)
    min_liq = _env_float("SUNPARK_MIN_INIT_LIQ_SOL", 0.0)

    age = stats.get("age_seconds")
    if max_age and age is not None and age / 60 > max_age:
        reasons.append("old_token")
    elif age is None:
        reasons.append("no_age_data")
    if min_vol and window.get("vol_sol", 0) < min_vol:
        reasons.append("low_volume")
    if min_buyers and window.get("unique_buyers", 0) < min_buyers:
        reasons.append("few_buyers")
    if min_ratio and window.get("sell_sol", 0) > 0:
        if window.get("buy_sol", 0) / window.get("sell_sol", 0) < min_ratio:
            reasons.append("bad_buy_ratio")
    if min_liq:
        liq = stats.get("initial_liquidity_sol")
        if liq is None or liq < min_liq:
            reasons.append("thin_liquidity")

    if reasons:
        return False, reasons
    return True, []


def rules_decision(event, card=None):
    """Track A: intentionally conservative deterministic comparison baseline."""
    allowed, flags = light_filter(event)
    if not allowed:
        return {"signal": "IGNORE", "confidence": 1.0, "reason": ", ".join(flags)}

    event_type = (event.get("event_type") or "").upper()
    category = event.get("event_category") or "other"
    if category == "token_creation":
        return {"signal": "IGNORE", "confidence": 1.0, "reason": "token creation is watch-only"}
    if category == "liquidity":
        card = card or build_analysis_card(event)
        if not card.get("token_mints"):
            return {"signal": "IGNORE", "confidence": 1.0, "reason": "liquidity event has no mint"}
        if card.get("native_sol", 0) < 1:
            return {"signal": "IGNORE", "confidence": 1.0, "reason": "liquidity event below 1 SOL"}
        return {"signal": "BUY", "confidence": 0.5, "reason": "tradable liquidity event passed basic facts"}
    if "SELL" in event_type:
        return {"signal": "SELL", "confidence": 1.0, "reason": "event type indicates sell"}
    if "BUY" in event_type and event.get("token_transfers"):
        return {"signal": "BUY", "confidence": 1.0, "reason": "event type indicates token buy"}
    return {
        "signal": "IGNORE",
        "confidence": 1.0,
        "reason": "no deterministic trade signal",
    }
