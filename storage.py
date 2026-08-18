import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("SUNPARK_DB_PATH", "data/events.sqlite"))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


_schema_checked = False
_schema_lock = threading.Lock()


def get_db():
    """Create a new SQLite connection. Schema is initialized exactly once."""
    global _schema_checked
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    with _schema_lock:
        if not _schema_checked:
            initialize_schema(connection)
            _schema_checked = True
    return connection


def initialize_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS processed_events (
            signature TEXT PRIMARY KEY,
            slot INTEGER,
            event_type TEXT,
            received_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
            signature TEXT PRIMARY KEY,
            slot INTEGER,
            event_type TEXT,
            payload_json TEXT NOT NULL,
            analysis_json TEXT,
            received_at TEXT NOT NULL,
            processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS track_decisions (
            signature TEXT NOT NULL,
            track TEXT NOT NULL,
            signal TEXT NOT NULL,
            confidence REAL,
            reason TEXT,
            latency_ms REAL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (signature, track)
        );
        CREATE TABLE IF NOT EXISTS ingress_stats (
            source TEXT PRIMARY KEY,
            stats_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            source TEXT NOT NULL,
            message TEXT NOT NULL,
            meta_json TEXT
        );
        CREATE TABLE IF NOT EXISTS token_meta (
            mint TEXT PRIMARY KEY,
            name TEXT,
            symbol TEXT,
            uri TEXT,
            decimals INTEGER,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mint_stats (
            mint TEXT PRIMARY KEY,
            stats_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS token_registry (
            mint TEXT PRIMARY KEY,
            creator TEXT,
            created_at REAL,
            source TEXT,
            decimals INTEGER,
            graduated_at REAL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS token_safety (
            mint TEXT PRIMARY KEY,
            mint_authority TEXT,
            mint_authority_is_pda INTEGER,
            decimals INTEGER,
            initialized INTEGER,
            freeze_authority TEXT,
            freeze_authority_is_pda INTEGER,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS token_holders (
            mint TEXT PRIMARY KEY,
            supply REAL,
            total_assets INTEGER,
            top1_share REAL,
            top3_share REAL,
            whale_share REAL,
            top20_share REAL,
            owners_json TEXT,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            kind TEXT NOT NULL,
            entry_time REAL NOT NULL,
            entry_price_sol REAL NOT NULL,
            entry_category TEXT,
            price_5m REAL,
            price_30m REAL,
            return_5m_pct REAL,
            return_30m_pct REAL,
            peak_price_sol REAL,
            peak_pct REAL,
            exit_reason TEXT,
            ai_signal TEXT,
            ai_confidence REAL,
            mode TEXT,
            score REAL,
            rank INTEGER,
            resolved TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_outcomes_mint ON outcomes(mint, entry_time);
        CREATE INDEX IF NOT EXISTS idx_outcomes_resolved ON outcomes(resolved, entry_time);
        CREATE TABLE IF NOT EXISTS picks (
            mint TEXT PRIMARY KEY,
            score REAL NOT NULL,
            rank INTEGER NOT NULL,
            reasons_json TEXT NOT NULL,
            event_category TEXT,
            ai_signal TEXT,
            ai_confidence REAL,
            ai_reason TEXT,
            ai_latency_ms REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_positions (
            mint TEXT PRIMARY KEY,
            entry_price_sol REAL,
            entry_time REAL,
            size_sol REAL,
            initial_size_sol REAL,
            peak_price_sol REAL,
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            action TEXT NOT NULL,
            price_sol REAL,
            sol_value REAL,
            pnl_sol REAL,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_state (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_activity_created
            ON activity_logs(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_candidates_received
            ON candidates(received_at);
        CREATE INDEX IF NOT EXISTS idx_track_decisions_sig
            ON track_decisions(signature);
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(candidates)")
    }
    if "analysis_json" not in columns:
        connection.execute("ALTER TABLE candidates ADD COLUMN analysis_json TEXT")
    outcome_cols = {
        row[1] for row in connection.execute("PRAGMA table_info(outcomes)")
    }
    if "entry_category" not in outcome_cols:
        connection.execute("ALTER TABLE outcomes ADD COLUMN entry_category TEXT")
    picks_cols = {
        row[1] for row in connection.execute("PRAGMA table_info(picks)")
    }
    if "event_category" not in picks_cols:
        connection.execute("ALTER TABLE picks ADD COLUMN event_category TEXT")
    trade_cols = {
        row[1] for row in connection.execute("PRAGMA table_info(paper_trades)")
    }
    if "is_wash" not in trade_cols:
        connection.execute("ALTER TABLE paper_trades ADD COLUMN is_wash INTEGER DEFAULT 0")
        connection.execute(
            "UPDATE paper_trades SET is_wash = 1 WHERE mint IN "
            "(SELECT mint FROM paper_trades GROUP BY mint HAVING COUNT(*) > 8)"
        )
    if "is_phantom" not in trade_cols:
        connection.execute("ALTER TABLE paper_trades ADD COLUMN is_phantom INTEGER DEFAULT 0")
        connection.execute(
            "UPDATE paper_trades SET is_phantom = 1 WHERE id IN ("
            "  SELECT t2.id FROM paper_trades t2"
            "  JOIN paper_trades t1 ON t1.mint = t2.mint"
            "  WHERE t2.action = 'tp2' AND t1.action = 'tp2' AND t1.id < t2.id"
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM paper_trades t3"
            "    WHERE t3.mint = t2.mint AND t3.action = 'open'"
            "    AND t3.id > t1.id AND t3.id < t2.id"
            "  )"
            ")"
        )
    if "entry_source" not in trade_cols:
        connection.execute("ALTER TABLE paper_trades ADD COLUMN entry_source TEXT DEFAULT 'rollup'")
    connection.commit()


def enqueue_candidate(event):
    signature = event.get("signature")
    if not signature:
        return False

    connection = get_db()
    try:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO candidates "
            "(signature, slot, event_type, payload_json, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                signature,
                event.get("slot"),
                event.get("event_type"),
                json.dumps(event),
                utc_now(),
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def update_ingress_stats(source, stats):
    connection = get_db()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO ingress_stats (source, stats_json, updated_at) "
            "VALUES (?, ?, ?)",
            (source, json.dumps(stats), utc_now()),
        )
        connection.commit()
    finally:
        connection.close()


def get_token_meta(mint):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT mint, name, symbol, uri, decimals, status, updated_at "
            "FROM token_meta WHERE mint = ?",
            (mint,),
        ).fetchone()
        if not row:
            return None
        return dict(zip(("mint", "name", "symbol", "uri", "decimals", "status", "updated_at"), row))
    finally:
        connection.close()


def save_token_meta(meta):
    connection = get_db()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO token_meta "
            "(mint, name, symbol, uri, decimals, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                meta["mint"], meta.get("name"), meta.get("symbol"), meta.get("uri"),
                meta.get("decimals"), meta["status"], utc_now(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_token_safety(mint):
    if not mint:
        return None
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT mint, mint_authority, mint_authority_is_pda, decimals, initialized, "
            "freeze_authority, freeze_authority_is_pda, status, updated_at "
            "FROM token_safety WHERE mint = ?",
            (mint,),
        ).fetchone()
        if not row:
            return None
        return dict(
            zip(
                (
                    "mint", "mint_authority", "mint_authority_is_pda", "decimals", "initialized",
                    "freeze_authority", "freeze_authority_is_pda", "status", "updated_at",
                ),
                row,
            )
        )
    finally:
        connection.close()


def save_token_safety(safety):
    if not safety.get("mint"):
        return
    connection = get_db()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO token_safety "
            "(mint, mint_authority, mint_authority_is_pda, decimals, initialized, "
            "freeze_authority, freeze_authority_is_pda, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                safety["mint"],
                safety.get("mint_authority"),
                1 if safety.get("mint_authority_is_pda") else 0,
                safety.get("decimals"),
                1 if safety.get("initialized") else 0,
                safety.get("freeze_authority"),
                1 if safety.get("freeze_authority_is_pda") else 0,
                safety.get("status", "missing"),
                utc_now(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_token_holders(mint):
    if not mint:
        return None
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT mint, supply, total_assets, top1_share, top3_share, whale_share, "
            "top20_share, owners_json, status, updated_at "
            "FROM token_holders WHERE mint = ?",
            (mint,),
        ).fetchone()
        if not row:
            return None
        result = dict(
            zip(
                (
                    "mint", "supply", "total_assets", "top1_share", "top3_share",
                    "whale_share", "top20_share", "owners_json", "status", "updated_at",
                ),
                row,
            )
        )
        try:
            result["owners"] = json.loads(result.pop("owners_json") or "[]")
        except (TypeError, ValueError):
            result["owners"] = []
        return result
    finally:
        connection.close()


def save_token_holders(holders):
    if not holders.get("mint"):
        return
    connection = get_db()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO token_holders "
            "(mint, supply, total_assets, top1_share, top3_share, whale_share, "
            "top20_share, owners_json, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                holders["mint"],
                holders.get("supply"),
                holders.get("total_assets"),
                holders.get("top1_share"),
                holders.get("top3_share"),
                holders.get("whale_share"),
                holders.get("top20_share"),
                json.dumps(holders.get("owners") or []),
                holders.get("status", "missing"),
                utc_now(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def insert_outcome(row):
    connection = get_db()
    try:
        cursor = connection.execute(
            "INSERT INTO outcomes "
            "(mint, kind, entry_time, entry_price_sol, entry_category, exit_reason, ai_signal, "
            "ai_confidence, mode, score, rank, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("mint"),
                row.get("kind"),
                row.get("entry_time"),
                row.get("entry_price_sol"),
                row.get("entry_category"),
                row.get("exit_reason"),
                row.get("ai_signal"),
                row.get("ai_confidence"),
                row.get("mode"),
                row.get("score"),
                row.get("rank"),
                utc_now(),
            ),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def find_outcome(mint, kind, entry_time):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT id FROM outcomes WHERE mint = ? AND kind = ? AND entry_time = ? LIMIT 1",
            (mint, kind, entry_time),
        ).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


def find_open_pick_outcome(mint):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT id FROM outcomes WHERE mint = ? AND kind = 'pick' AND resolved IS NULL LIMIT 1",
            (mint,),
        ).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


def update_outcome(outcome_id, **fields):
    if not fields:
        return
    allowed = {
        "price_5m", "price_30m", "return_5m_pct", "return_30m_pct",
        "peak_price_sol", "peak_pct", "exit_reason", "resolved", "resolved_at",
    }
    assignments = {key: value for key, value in fields.items() if key in allowed}
    if not assignments:
        return
    clause = ", ".join(f"{key} = ?" for key in assignments)
    connection = get_db()
    try:
        connection.execute(
            f"UPDATE outcomes SET {clause} WHERE id = ?",
            (*assignments.values(), outcome_id),
        )
        connection.commit()
    finally:
        connection.close()


def list_outcomes(limit=2000):
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT mint, kind, entry_time, entry_price_sol, price_5m, price_30m, "
            "return_5m_pct, return_30m_pct, peak_price_sol, peak_pct, exit_reason, "
            "ai_signal, ai_confidence, mode, score, rank, resolved "
            "FROM outcomes ORDER BY entry_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            dict(
                zip(
                    (
                        "mint", "kind", "entry_time", "entry_price_sol", "price_5m",
                        "price_30m", "return_5m_pct", "return_30m_pct", "peak_price_sol",
                        "peak_pct", "exit_reason", "ai_signal", "ai_confidence", "mode",
                        "score", "rank", "resolved",
                    ),
                    row,
                )
            )
            for row in rows
        ]
    finally:
        connection.close()


def set_paper_exit_reason(mint, reason):
    connection = get_db()
    try:
        connection.execute(
            "UPDATE outcomes SET exit_reason = ? WHERE id = ("
            "  SELECT id FROM outcomes WHERE mint = ? AND kind = 'paper' "
            "  AND resolved IS NULL AND exit_reason IS NULL "
            "  ORDER BY entry_time DESC LIMIT 1"
            ")",
            (reason, mint),
        )
        connection.commit()
    finally:
        connection.close()


def list_unresolved_outcomes(limit=1000):
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT id, mint, kind, entry_time, entry_price_sol, price_5m, price_30m, peak_price_sol "
            "FROM outcomes WHERE resolved IS NULL ORDER BY entry_time ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            dict(zip(("id", "mint", "kind", "entry_time", "entry_price_sol", "price_5m", "price_30m", "peak_price_sol"), row))
            for row in rows
        ]
    finally:
        connection.close()


def save_mint_stats(stats_rows):
    if not stats_rows:
        return
    connection = get_db()
    try:
        connection.executemany(
            "INSERT OR REPLACE INTO mint_stats (mint, stats_json, updated_at) "
            "VALUES (?, ?, ?)",
            [(row.get("mint"), json.dumps(row), utc_now()) for row in stats_rows],
        )
        connection.commit()
    finally:
        connection.close()


def load_mint_stats():
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT mint, stats_json FROM mint_stats"
        ).fetchall()
        return [
            {"mint": row[0], **json.loads(row[1])}
            for row in rows
        ]
    finally:
        connection.close()


def prune_mint_stats(keep_mints, max_rows=5000):
    if not keep_mints:
        return
    connection = get_db()
    try:
        connection.execute(
            "DELETE FROM mint_stats WHERE mint NOT IN (%s)"
            % ",".join("?" * len(keep_mints)),
            list(keep_mints),
        )
        excess = connection.execute(
            "SELECT mint FROM mint_stats ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
            (max_rows,),
        ).fetchall()
        for (mint,) in excess:
            connection.execute("DELETE FROM mint_stats WHERE mint = ?", (mint,))
        connection.commit()
    finally:
        connection.close()


def save_analysis_card(signature, card):
    connection = get_db()
    try:
        connection.execute(
            "UPDATE candidates SET analysis_json = ? WHERE signature = ?",
            (json.dumps(card), signature),
        )
        connection.commit()
    finally:
        connection.close()


def save_token_registry(record):
    if not record.get("mint"):
        return
    connection = get_db()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO token_registry "
            "(mint, creator, created_at, source, decimals, graduated_at, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["mint"],
                record.get("creator"),
                record.get("created_at"),
                record.get("source"),
                record.get("decimals"),
                record.get("graduated_at"),
                record.get("status", "created"),
                utc_now(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_token_registry(mint):
    if not mint:
        return None
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT mint, creator, created_at, source, decimals, graduated_at, status, updated_at "
            "FROM token_registry WHERE mint = ?",
            (mint,),
        ).fetchone()
        if not row:
            return None
        return dict(
            zip(
                ("mint", "creator", "created_at", "source", "decimals", "graduated_at", "status", "updated_at"),
                row,
            )
        )
    finally:
        connection.close()


def creator_tokens(creator):
    if not creator:
        return []
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT mint, creator, created_at, source, decimals, graduated_at, status, updated_at "
            "FROM token_registry WHERE creator = ? ORDER BY created_at DESC LIMIT 200",
            (creator,),
        ).fetchall()
        return [
            dict(
                zip(
                    ("mint", "creator", "created_at", "source", "decimals", "graduated_at", "status", "updated_at"),
                    row,
                )
            )
            for row in rows
        ]
    finally:
        connection.close()


def save_picks(rows):
    if not rows:
        return
    connection = get_db()
    try:
        for row in rows:
            connection.execute(
                "INSERT OR REPLACE INTO picks "
                "(mint, score, rank, reasons_json, event_category, ai_signal, ai_confidence, ai_reason, ai_latency_ms, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["mint"],
                    row.get("score", 0),
                    row.get("rank", 0),
                    json.dumps(row.get("reasons", [])),
                    row.get("event_category"),
                    row.get("ai_signal"),
                    row.get("ai_confidence"),
                    row.get("ai_reason"),
                    row.get("ai_latency_ms"),
                    row.get("created_at") or utc_now(),
                    utc_now(),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def load_picks(limit=10):
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT mint, score, rank, reasons_json, ai_signal, ai_confidence, ai_reason, ai_latency_ms, created_at, updated_at "
            "FROM picks ORDER BY rank ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "mint": row[0],
                "score": row[1],
                "rank": row[2],
                "reasons": json.loads(row[3] or "[]"),
                "ai_signal": row[4],
                "ai_confidence": row[5],
                "ai_reason": row[6],
                "ai_latency_ms": row[7],
                "created_at": row[8],
                "updated_at": row[9],
            }
            for row in rows
        ]
    finally:
        connection.close()


def clear_stale_picks(keep_mints, limit=50):
    connection = get_db()
    try:
        if keep_mints:
            connection.execute(
                "DELETE FROM picks WHERE mint NOT IN (%s)"
                % ",".join("?" * len(keep_mints)),
                list(keep_mints),
            )
        excess = connection.execute(
            "SELECT mint FROM picks ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
            (limit,),
        ).fetchall()
        for (mint,) in excess:
            connection.execute("DELETE FROM picks WHERE mint = ?", (mint,))
        connection.commit()
    finally:
        connection.close()


def load_paper_positions():
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT mint, entry_price_sol, entry_time, size_sol, initial_size_sol, peak_price_sol, state "
            "FROM paper_positions WHERE state != 'closed' ORDER BY updated_at DESC"
        ).fetchall()
        return [
            {
                "mint": row[0],
                "entry_price_sol": row[1],
                "entry_time": row[2],
                "size_sol": row[3],
                "initial_size_sol": row[4],
                "peak_price_sol": row[5],
                "state": row[6],
            }
            for row in rows
        ]
    finally:
        connection.close()


def save_paper_position(position, closed=False):
    if not position.get("mint"):
        return
    connection = get_db()
    try:
        if closed:
            connection.execute("DELETE FROM paper_positions WHERE mint = ?", (position["mint"],))
        else:
            connection.execute(
                "INSERT OR REPLACE INTO paper_positions "
                "(mint, entry_price_sol, entry_time, size_sol, initial_size_sol, peak_price_sol, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    position["mint"],
                    position.get("entry_price_sol"),
                    position.get("entry_time"),
                    position.get("size_sol"),
                    position.get("initial_size_sol"),
                    position.get("peak_price_sol"),
                    position.get("state", "open"),
                    utc_now(),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def save_paper_trade(trade):
    connection = get_db()
    try:
        connection.execute(
            "INSERT INTO paper_trades (mint, action, price_sol, sol_value, pnl_sol, reason, created_at, is_wash, is_phantom, entry_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade.get("mint"),
                trade.get("action"),
                trade.get("price_sol"),
                trade.get("sol_value"),
                trade.get("pnl_sol"),
                trade.get("reason"),
                trade.get("created_at") or utc_now(),
                trade.get("is_wash", 0),
                trade.get("is_phantom", 0),
                trade.get("entry_source", "rollup"),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def load_paper_trades(limit=500):
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT mint, action, price_sol, sol_value, pnl_sol, reason, created_at, is_wash, is_phantom, entry_source "
            "FROM paper_trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "mint": row[0],
                "action": row[1],
                "price_sol": row[2],
                "sol_value": row[3],
                "pnl_sol": row[4],
                "reason": row[5],
                "created_at": row[6],
                "is_wash": row[7] or 0,
                "is_phantom": row[8] or 0,
                "entry_source": row[9] or "rollup",
            }
            for row in rows
        ]
    finally:
        connection.close()


def save_paper_state(key, value):
    connection = get_db()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO paper_state (key, value_json, updated_at) "
            "VALUES (?, ?, ?)",
            (key, json.dumps(value), utc_now()),
        )
        connection.commit()
    finally:
        connection.close()


def load_paper_state(key, default=None):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT value_json FROM paper_state WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default
    finally:
        connection.close()


def clear_paper_trades():
    connection = get_db()
    try:
        connection.execute("DELETE FROM paper_trades")
        connection.execute("DELETE FROM paper_positions")
        connection.commit()
    finally:
        connection.close()


def prune_old_candidates(keep_hours=24, keep_recent=500):
    """Delete processed candidates older than keep_hours to prevent DB bloat."""
    connection = get_db()
    try:
        connection.execute(
            "DELETE FROM candidates WHERE processed_at IS NOT NULL "
            "AND signature NOT IN ("
            "  SELECT signature FROM candidates "
            "  WHERE processed_at IS NOT NULL "
            "  ORDER BY received_at DESC LIMIT ?"
            ")",
            (keep_recent,),
        )
        connection.execute(
            "DELETE FROM candidates WHERE processed_at IS NOT NULL "
            "AND received_at < datetime('now', ?)",
            (f"-{keep_hours} hours",),
        )
        connection.commit()
    finally:
        connection.close()


def _safe_meta_json(meta):
    if meta is None:
        return None
    s = json.dumps(meta)
    if len(s) <= 1000:
        return s
    trimmed = {}
    for k, v in meta.items():
        sv = str(v)
        trimmed[k] = sv[:200] if len(sv) > 200 else sv
    return json.dumps(trimmed)[:1000]


def append_activity(level, source, message, meta=None):
    connection = get_db()
    try:
        connection.execute(
            "INSERT INTO activity_logs "
            "(created_at, level, source, message, meta_json) VALUES (?, ?, ?, ?, ?)",
            (
                utc_now(),
                level[:20],
                source[:40],
                str(message)[:500],
                _safe_meta_json(meta),
            ),
        )
        connection.execute(
            "DELETE FROM activity_logs WHERE id NOT IN "
            "(SELECT id FROM activity_logs ORDER BY id DESC LIMIT 5000)"
        )
        connection.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()
