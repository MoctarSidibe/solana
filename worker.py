import json
import os
import queue
import threading
import time

import outcomes
from brain import judge_volume_spike
from exits import PAPER_ENTRY_SOL, paper
from filters import build_analysis_card, primary_mint, rules_decision, selection_gate
from holders import cached_token_holders, resolve_token_holders
from intel import wallet_book
from jupiter import quote_buy_fill, quote_sell_fill
from metadata import cached_token_metadata, resolve_token_metadata
from rank import compute_rankings
from safety import resolve_token_safety
from stats import rollup
from storage import (
    append_activity,
    clear_stale_picks,
    get_db,
    get_token_registry,
    load_mint_stats,
    load_paper_trades,
    load_picks,
    prune_mint_stats,
    save_analysis_card,
    save_mint_stats,
    save_picks,
    save_token_registry,
    utc_now,
)

metadata_jobs = queue.Queue(maxsize=2000)
metadata_seen = set()
metadata_seen_lock = threading.Lock()

safety_jobs = queue.Queue(maxsize=2000)
safety_seen = set()
safety_seen_lock = threading.Lock()

holders_jobs = queue.Queue(maxsize=2000)
holders_seen = set()
holders_seen_lock = threading.Lock()

picks_event = threading.Event()


def swap_direction(event):
    source = (event.get("source") or "").upper()
    event_type = (event.get("event_type") or "").upper()
    if "SELL" in event_type:
        return "sell"
    if "BUY" in event_type:
        return "buy"
    if source == "RAYDIUM":
        return "sell" if "SELL" in event_type else "buy"
    return "buy"


QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _transfer_ui(transfer):
    raw = transfer.get("tokenAmount")
    decimals = transfer.get("decimals")
    if raw is None or decimals is None:
        return None
    try:
        return int(raw) / (10 ** int(decimals))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def token_price_sol(event, mint):
    """Derive a SOL/token price from the event's token + SOL legs.

    The stream emits raw tokenAmount/decimals (no amount_ui). The SOL side is
    the WSOL leg that shares an account with the primary token leg (that is the
    AMM pair); multi-hop routes fall back to the largest WSOL leg, and native
    SOL is only used when no WSOL/USDC leg exists. Returns None when no usable
    pair is present so a price is never guessed.
    """
    transfers = [t for t in event.get("token_transfers") or [] if isinstance(t, dict)]
    target = next((t for t in transfers if t.get("mint") == mint), None)
    if not target:
        return None
    token_ui = _transfer_ui(target)
    if not token_ui or token_ui <= 0:
        return None
    target_accounts = {target.get("fromUserAccount"), target.get("toUserAccount")}
    wsol_legs = [
        (t.get("fromUserAccount"), t.get("toUserAccount"), _transfer_ui(t))
        for t in transfers
        if t.get("mint") == WSOL_MINT
    ]
    sol_ui = None
    for frm, to, amount in wsol_legs:
        if not amount:
            continue
        if frm in target_accounts or to in target_accounts:
            sol_ui = amount if sol_ui is None else max(sol_ui, amount)
    if sol_ui is None:
        candidates = [amount for _, _, amount in wsol_legs if amount]
        if candidates:
            sol_ui = max(candidates)
    if not sol_ui and not any(t.get("mint") == USDC_MINT for t in transfers):
        native_amounts = [
            item.get("amount")
            for item in event.get("native_transfers") or []
            if isinstance(item, dict) and item.get("amount")
        ]
        if native_amounts:
            sol_ui = max(native_amounts) / 1e9
    if not sol_ui or sol_ui <= 0:
        return None
    return sol_ui / token_ui


def apply_event_to_rollup(event, card):
    mint = card.get("primary_mint")
    if not mint or mint in QUOTE_MINTS:
        return
    timestamp = event.get("timestamp") or time.time()
    category = event.get("event_category") or card.get("event_category")
    native_sol = card.get("native_sol") or 0
    trader = event.get("fee_payer")
    if category == "liquidity":
        rollup.record_liquidity(mint, timestamp, max(native_sol, 0.0))
    elif category == "swap":
        side = swap_direction(event)
        rollup.record_swap(mint, timestamp, side, native_sol, trader)
        wallet_book.record_swap(trader, mint, side, native_sol, timestamp)
    price = token_price_sol(event, mint)
    if price:
        rollup.record_price(mint, timestamp, price)


def stats_loop():
    snapshot_seconds = int(os.getenv("SUNPARK_STATS_SNAPSHOT_SECONDS", "60"))
    prune_counter = 0
    while True:
        time.sleep(snapshot_seconds)
        try:
            rows = rollup.snapshot_all()
            save_mint_stats(rows)
            active_mints = {row["mint"] for row in rows}
            prune_mint_stats(active_mints)
        except Exception as error:
            append_activity(
                "warn", "worker", "stats snapshot failed",
                {"error": str(error)[:300]},
            )
        prune_counter += 1
        if prune_counter >= 30:
            prune_counter = 0
            try:
                from storage import prune_old_candidates
                prune_old_candidates()
            except Exception:
                pass


def update_registry(event, card):
    mint = card.get("primary_mint")
    if not mint:
        return
    timestamp = event.get("timestamp") or time.time()
    source = event.get("source") or card.get("source")
    category = event.get("event_category") or card.get("event_category")
    existing = get_token_registry(mint)
    if category == "token_creation" and not existing:
        save_token_registry(
            {
                "mint": mint,
                "creator": event.get("fee_payer"),
                "created_at": timestamp,
                "source": source,
                "decimals": card.get("decimals"),
                "status": "created",
            }
        )
    elif category == "liquidity":
        is_migration = event.get("event_type") == "PUMP_MIGRATE"
        record = {
            "mint": mint,
            "creator": (existing or {}).get("creator") or event.get("fee_payer"),
            "created_at": (existing or {}).get("created_at") or timestamp,
            "source": (existing or {}).get("source") or source,
            "decimals": card.get("decimals"),
            "graduated_at": (existing or {}).get("graduated_at") or (timestamp if is_migration else None),
            "status": "migrated" if is_migration else (existing or {}).get("status", "created"),
        }
        save_token_registry(record)


def pick_payload(pick):
    mint = pick["mint"]
    snapshot = rollup.stats_for(mint) or {}
    meta = cached_token_metadata(mint) or {}
    from filters import compact_stats

    card = {"primary_mint": mint, "stats": snapshot, "name": meta.get("name"), "symbol": meta.get("symbol")}
    return {
        "mint": mint,
        "name": meta.get("name"),
        "symbol": meta.get("symbol"),
        "price_sol": snapshot.get("price_sol"),
        "age_seconds": snapshot.get("age_seconds"),
        "stats": compact_stats(card),
        "intel_quality": pick.get("intel_quality"),
    }


def paper_entry_fill(mint):
    """Effective paper entry price: Jupiter quote if enabled, else rollup.

    Returns (price, source). Never blocks forever; on any quote failure the
    rollup price is used and the source says so, so the paper account keeps
    collecting samples instead of freezing.
    """
    snapshot = rollup.stats_for(mint) or {}
    price = snapshot.get("price_sol")
    if not price or price <= 0:
        return None, None
    if os.getenv("SUNPARK_JUPITER_QUOTE", "1") == "1":
        try:
            quote = quote_buy_fill(mint, PAPER_ENTRY_SOL)
            if quote and quote.get("price_sol"):
                return quote["price_sol"], "jupiter"
        except Exception:
            pass
        return price, "rollup"
    return price, "rollup"


def try_paper_entry(mint, reason):
    """Enter one paper position at the honest fill price. Returns True/False."""
    price, source = paper_entry_fill(mint)
    if not price:
        return False
    if paper.halt_trading():
        append_activity(
            "warn", "exits", "paper entry blocked by circuit breaker",
            {"mint": mint[:12]},
        )
        return False
    opened = paper.open_position(mint, price, entry_reason=reason, entry_source=source or "rollup")
    if opened:
        append_activity(
            "info", "exits", "paper entry opened",
            {"mint": mint[:12], "source": source, "reason": reason},
        )
    return opened


def picks_loop():
    cooldown = int(os.getenv("SUNPARK_PICK_AI_COOLDOWN", "600"))
    auto_mode = os.getenv("SUNPARK_AUTO_PAPER_MODE", "mechanical").lower()
    auto_max = int(os.getenv("SUNPARK_AUTO_PAPER_MAX", "2"))
    min_interval = float(os.getenv("SUNPARK_PICKS_MIN_INTERVAL", "5"))
    ai_enabled = os.getenv("SUNPARK_AI_ENABLED", "0") == "1"
    ai_last_run = {}
    last_picks_time = 0.0
    while True:
        picks_event.wait(timeout=min_interval)
        picks_event.clear()
        elapsed = time.time() - last_picks_time
        if elapsed < min_interval:
            picks_event.set()
            time.sleep(min_interval - elapsed)
            continue
        last_picks_time = time.time()
        try:
            enqueue_top_holder_jobs()
            picks = compute_rankings()
            if not picks:
                clear_stale_picks([], 10)
                continue
            mints = [pick["mint"] for pick in picks]
            existing = {item["mint"]: item for item in load_picks(50)}
            now = time.time()
            rows = []
            for pick in picks:
                row = {
                    "mint": pick["mint"],
                    "score": pick["score"],
                    "rank": pick["rank"],
                    "reasons": pick["reasons"],
                    "event_category": pick.get("event_category"),
                    "created_at": (existing.get(pick["mint"]) or {}).get("created_at"),
                    "ai_signal": (existing.get(pick["mint"]) or {}).get("ai_signal"),
                    "ai_confidence": (existing.get(pick["mint"]) or {}).get("ai_confidence"),
                    "ai_reason": (existing.get(pick["mint"]) or {}).get("ai_reason"),
                    "ai_latency_ms": (existing.get(pick["mint"]) or {}).get("ai_latency_ms"),
                }
                rows.append(row)
            save_picks(rows)
            clear_stale_picks(mints, 10)

            if auto_mode == "mechanical":
                for pick in picks[:auto_max]:
                    try:
                        try_paper_entry(pick["mint"], f"entry_mech_top{pick['rank']}")
                    except Exception as error:
                        append_activity(
                            "warn", "exits", "mechanical paper entry failed",
                            {"mint": pick["mint"][:12], "error": str(error)[:300]},
                        )

            if ai_enabled:
                for pick in picks:
                    mint = pick["mint"]
                    if now - ai_last_run.get(mint, 0) < cooldown:
                        continue
                    try:
                        started = time.perf_counter()
                        result = normalize_ai_result(judge_volume_spike(pick_payload(pick)))
                        latency = (time.perf_counter() - started) * 1000
                        ai_last_run[mint] = now
                        if result["signal"] == "IGNORE" and result["reason"] == "AI returned no result":
                            append_activity(
                                "warn", "ranker", "AI last-pass returned nothing",
                                {"mint": mint[:12], "latency_ms": round(latency, 1)},
                            )
                        for row in rows:
                            if row["mint"] == mint:
                                row["ai_signal"] = result["signal"]
                                row["ai_confidence"] = result.get("confidence")
                                row["ai_reason"] = result.get("reason")
                                row["ai_latency_ms"] = round(latency, 1)
                        append_activity(
                            "info", "ranker", "AI last-pass",
                            {"mint": mint[:12], "signal": result["signal"], "latency_ms": round(latency, 1)},
                        )
                        if auto_mode == "ai" and result["signal"] == "BUY":
                            try_paper_entry(mint, "entry_ai_buy")
                    except Exception as error:
                        append_activity(
                            "warn", "ranker", "AI last-pass failed",
                            {"mint": mint[:12], "error": str(error)[:300]},
                        )
                save_picks(rows)
        except Exception as error:
            append_activity(
                "warn", "ranker", "picks loop failed",
                {"error": str(error)[:300]},
            )


def outcomes_loop():
    interval = float(os.getenv("SUNPARK_OUTCOMES_INTERVAL", "60"))
    while True:
        time.sleep(interval)
        try:
            for pick in load_picks(50):
                outcomes.record_pick_outcome(pick)
            for trade in load_paper_trades(500):
                outcomes.record_paper_outcome(trade)
            outcomes.resolve_maturities()
        except Exception as error:
            append_activity(
                "warn", "outcomes", "outcome labeling failed",
                {"error": str(error)[:300]},
            )


def exits_loop():
    interval = float(os.getenv("SUNPARK_EXIT_INTERVAL", "10"))
    while True:
        time.sleep(interval)
        try:
            snapshots = rollup.snapshot_all()
            paper.evaluate({row["mint"]: row for row in snapshots})
        except Exception as error:
            append_activity(
                "warn", "exits", "exit evaluation failed",
                {"error": str(error)[:300]},
            )


def queue_metadata_job(signature, event, mint):
    if not mint:
        return
    with metadata_seen_lock:
        if mint in metadata_seen:
            return
        metadata_seen.add(mint)
    try:
        metadata_jobs.put_nowait((signature, event, mint))
    except queue.Full:
        append_activity("warn", "worker", "metadata queue full", {"mint": mint[:16], "depth": metadata_jobs.qsize()})


def queue_safety_job(mint):
    if not mint:
        return
    with safety_seen_lock:
        if mint in safety_seen:
            return
        safety_seen.add(mint)
    try:
        safety_jobs.put_nowait(mint)
    except queue.Full:
        append_activity("warn", "worker", "safety queue full", {"mint": mint[:16], "depth": safety_jobs.qsize()})


def safety_loop():
    while True:
        mint = safety_jobs.get()
        try:
            resolve_token_safety(mint)
        except Exception as error:
            append_activity(
                "warn", "worker", "safety enrichment failed",
                {"mint": mint[:16], "error": str(error)[:300]},
            )
        finally:
            safety_jobs.task_done()


def queue_holders_job(mint):
    if not mint:
        return
    with holders_seen_lock:
        if mint in holders_seen:
            return
        holders_seen.add(mint)
    if cached_token_holders(mint):
        return
    try:
        holders_jobs.put_nowait(mint)
    except queue.Full:
        append_activity("warn", "worker", "holders queue full", {"mint": mint[:16], "depth": holders_jobs.qsize()})


def holders_loop():
    while True:
        mint = holders_jobs.get()
        try:
            result = resolve_token_holders(mint)
            if result and result.get("transient"):
                with holders_seen_lock:
                    holders_seen.discard(mint)
        except Exception as error:
            append_activity(
                "warn", "worker", "holders enrichment failed",
                {"mint": mint[:16], "error": str(error)[:300]},
            )
        finally:
            holders_jobs.task_done()


def enqueue_top_holder_jobs(limit=20):
    try:
        mints = [row["mint"] for row in rollup.top_mints(300, limit=limit)]
    except Exception:
        return
    for mint in mints:
        queue_holders_job(mint)


def metadata_loop():
    while True:
        signature, event, mint = metadata_jobs.get()
        try:
            metadata = resolve_token_metadata(mint)
            save_analysis_card(signature, build_analysis_card(event, metadata))
        except Exception as error:
            append_activity(
                "warn", "worker", "metadata enrichment failed",
                {"mint": mint[:16], "error": str(error)[:300]},
            )
        finally:
            metadata_jobs.task_done()


def normalize_ai_result(result):
    if not isinstance(result, dict):
        return {"signal": "IGNORE", "confidence": None, "reason": "AI returned no result"}

    signal = result.get("signal")
    if signal not in {"BUY", "SELL", "IGNORE"}:
        signal = "BUY" if result.get("buy") is True else "IGNORE"
    return {
        "signal": signal,
        "confidence": result.get("confidence"),
        "reason": result.get("reason", "No reason given"),
    }


def save_decision(connection, signature, track, result, latency_ms, status="ok", error=None):
    connection.execute(
        "INSERT OR REPLACE INTO track_decisions "
        "(signature, track, signal, confidence, reason, latency_ms, status, error, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            signature,
            track,
            result.get("signal", "IGNORE"),
            result.get("confidence"),
            result.get("reason"),
            latency_ms,
            status,
            error,
            utc_now(),
        ),
    )


def process_candidate(candidate, connection, run_ai=True):
    event = json.loads(candidate["payload_json"])
    signature = candidate["signature"]
    mint = primary_mint(event)
    metadata = None
    if mint:
        if event.get("event_category") in {"liquidity", "token_creation"}:
            metadata = resolve_token_metadata(mint)
        else:
            metadata = cached_token_metadata(mint) or {"mint": mint, "status": "pending"}
    card = build_analysis_card(event, metadata)
    apply_event_to_rollup(event, card)
    update_registry(event, card)
    picks_event.set()
    queue_safety_job(mint)
    queue_holders_job(mint)
    if metadata and metadata.get("status") == "pending":
        queue_metadata_job(signature, event, mint)
    connection.execute(
        "UPDATE candidates SET analysis_json = ? WHERE signature = ?",
        (json.dumps(card), signature),
    )

    started = time.perf_counter()
    rules_result = rules_decision(event, card)
    save_decision(
        connection,
        signature,
        "rules",
        rules_result,
        (time.perf_counter() - started) * 1000,
    )

    if run_ai:
        gated, reasons = selection_gate(event, card)
        if not gated:
            ai_result = {
                "signal": "IGNORE",
                "confidence": 1.0,
                "reason": ", ".join(reasons),
            }
            save_decision(connection, signature, "ai", ai_result, 0, status="filtered")
        else:
            save_decision(
                connection,
                signature,
                "ai",
                {"signal": "IGNORE", "confidence": None, "reason": "passed gate, awaiting picks ranking"},
                0,
                status="pending",
            )

    connection.execute(
        "UPDATE candidates SET processed_at = ? WHERE signature = ?",
        (utc_now(), signature),
    )


def process_pending_once(limit=10, run_ai=True):
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT signature, payload_json FROM candidates "
            "WHERE processed_at IS NULL ORDER BY received_at LIMIT ?",
            (limit,),
        ).fetchall()
        candidates = [{"signature": row[0], "payload_json": row[1]} for row in rows]
        for candidate in candidates:
            try:
                process_candidate(candidate, connection, run_ai=run_ai)
            except Exception as error:
                connection.rollback()
                connection.execute(
                    "INSERT OR REPLACE INTO track_decisions "
                    "(signature, track, signal, confidence, reason, latency_ms, status, error, created_at) "
                    "VALUES (?, 'ai', 'IGNORE', NULL, 'worker error', 0, 'error', ?, ?)",
                    (candidate["signature"], str(error)[:500], utc_now()),
                )
                connection.execute(
                    "UPDATE candidates SET processed_at = ? WHERE signature = ?",
                    (utc_now(), candidate["signature"]),
                )
                connection.commit()
                append_activity(
                    "error", "worker", "candidate processing failed",
                    {"signature": candidate["signature"][:16], "error": str(error)[:300]},
                )
        connection.commit()
        return len(candidates)
    finally:
        connection.close()


def worker_loop(stop_event=None):
    stop_event = stop_event or threading.Event()
    poll_seconds = float(os.getenv("SUNPARK_WORKER_POLL_SECONDS", "0.5"))
    while not stop_event.is_set():
        processed = process_pending_once()
        if not processed:
            stop_event.wait(poll_seconds)


def start_worker():
    try:
        rollup.restore(load_mint_stats())
    except Exception as error:
        append_activity("warn", "worker", "stats restore failed", {"error": str(error)[:300]})
    if os.getenv("SUNPARK_JUPITER_QUOTE", "1") == "1":
        def sell_hook(mint, size_sol, price_sol):
            try:
                quote = quote_sell_fill(mint, size_sol, price_sol)
            except Exception:
                return None
            return (quote or {}).get("price_sol")

        paper.sell_price_hook = sell_hook
    threading.Thread(target=metadata_loop, name="sunpark-metadata", daemon=True).start()
    threading.Thread(target=safety_loop, name="sunpark-safety", daemon=True).start()
    threading.Thread(target=holders_loop, name="sunpark-holders", daemon=True).start()
    threading.Thread(target=stats_loop, name="sunpark-stats", daemon=True).start()
    threading.Thread(target=picks_loop, name="sunpark-picks", daemon=True).start()
    threading.Thread(target=exits_loop, name="sunpark-exits", daemon=True).start()
    threading.Thread(target=outcomes_loop, name="sunpark-outcomes", daemon=True).start()
    thread = threading.Thread(target=worker_loop, name="sunpark-worker", daemon=True)
    thread.start()
    return thread
