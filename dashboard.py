import json
import time

from filters import compact_stats, selection_gate
from stats import rollup
from storage import get_db, load_picks

_STATUS_CACHE = {"heavy": (0.0, {}), "recent": (0.0, []), "migrations": (0.0, {})}
_STATUS_CACHE_TTL = 60
_RECENT_CACHE_TTL = 20
_MIGRATIONS_CACHE_TTL = 120


def _cached(name, fn):
    ts, value = _STATUS_CACHE[name]
    if time.time() - ts > _STATUS_CACHE_TTL:
        value = fn()
        _STATUS_CACHE[name] = (time.time(), value)
    return value


def _cached_recent(fn):
    ts, value = _STATUS_CACHE["recent"]
    if time.time() - ts > _RECENT_CACHE_TTL:
        value = fn()
        _STATUS_CACHE["recent"] = (time.time(), value)
    return value


def _heavy_stats(connection):
    """Expensive aggregates over the big tables; refreshed once per minute."""
    return {
        "pending": connection.execute(
            "SELECT COUNT(*) FROM candidates WHERE processed_at IS NULL"
        ).fetchone()[0],
        "candidates_24h": connection.execute(
            "SELECT COUNT(*) FROM candidates WHERE datetime(received_at) >= datetime('now', '-1 day')"
        ).fetchone()[0],
        "ai_errors": connection.execute(
            "SELECT COUNT(*) FROM track_decisions WHERE track = 'ai' AND status = 'error'"
        ).fetchone()[0],
        "ai_latency": connection.execute(
            "SELECT AVG(latency_ms) FROM track_decisions WHERE track = 'ai' AND status = 'ok'"
        ).fetchone()[0] or 0,
        "rejection": rejection_counts(connection),
        "funnel": {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT category, COUNT(*) FROM ("
                "  SELECT json_extract(analysis_json, '$.event_category') AS category "
                "  FROM candidates WHERE analysis_json IS NOT NULL "
                "  AND datetime(received_at) >= datetime('now', '-3 hours')"
                ") WHERE category IS NOT NULL GROUP BY category"
            )
        },
    }


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SunPark Monitor</title>
<style>
body{margin:0;background:#10151b;color:#e7edf3;font:15px system-ui,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:22px 0}
.card{background:#18212a;border:1px solid #2a3945;border-radius:10px;padding:16px}
.value{font-size:25px;font-weight:700;margin-top:8px}.ok{color:#53d69a}.warn{color:#f4c95d}.bad{color:#ff7e7e}
table{width:100%;border-collapse:collapse;background:#18212a;border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:10px;border-bottom:1px solid #2a3945;font-size:13px} th{color:#91a0ad}
code{color:#a9d6ff} .signal-BUY{color:#53d69a}.signal-SELL{color:#ff9f7e}.signal-IGNORE{color:#91a0ad}
@media(max-width:650px){main{padding:14px}th,td{padding:8px 5px;font-size:11px}.hide-mobile{display:none}}
</style>
</head>
<body><main>
<h1><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#f4c95d" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;margin-right:6px"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>SunPark Monitor</h1><div class="muted"><a href="/sunpark/logs">Activity logs</a> | Paper-trading pipeline. Refreshes every 10 seconds.</div>
 <section class="grid" id="cards"></section>
 <h2>Paper Account (dry-run)</h2>
  <div class="muted">Paper PnL. Honest excludes wash trades, applies 30% slippage + fees.</div>
  <section class="grid" id="paperCards"></section>
   <table><thead><tr><th>Position</th><th>State</th><th>Entry</th><th>Peak</th></tr></thead>
   <tbody id="paper"><tr><td colspan="4">Loading...</td></tr></tbody></table>
 <h2>Live Picks (Top-10)</h2>
 <div class="muted">Gate-clean mints ranked by momentum score.</div>
 <table><thead><tr><th>#</th><th>Token</th><th>Score</th><th>Why</th><th>AI verdict</th><th>AI reason</th></tr></thead>
 <tbody id="picks"><tr><td colspan="6">Loading...</td></tr></tbody></table>

  <h2>RPC Enrichment Health</h2>
  <div class="muted">Background RPC caches. Green = live, red = rate-limited (self-heals).</div>
  <section class="grid" id="enrich"></section>
  <h2>Edge (forward outcomes)</h2>
  <div class="muted">Forward +5m/+30m returns from live prices. No hindsight.</div>
  <section class="grid" id="edgeCards"></section>
   <h3>By entry mode</h3>
   <table><thead><tr><th>Mode</th><th>Samples</th><th>Resolved</th><th>Win %</th><th>Avg +30m</th><th>Median +30m</th></tr></thead><tbody id="edge-mode"><tr><td colspan="6">Loading...</td></tr></tbody></table>
   <h3>By exit reason</h3>
  <table><thead><tr><th>Exit</th><th>Samples</th><th>Resolved</th><th>Win %</th><th>Avg +30m</th><th>Median +30m</th></tr></thead><tbody id="edge-exit"><tr><td colspan="6">Loading...</td></tr></tbody></table>
   <h2>Why Tokens Get Rejected (24h)</h2>
 <table><thead><tr><th>Reason</th><th>Count</th></tr></thead>
 <tbody id="reject"><tr><td colspan="2">Loading...</td></tr></tbody></table>

</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function card(label,value,kind=''){return `<div class="card"><div class="muted">${esc(label)}</div><div class="value ${kind}">${esc(value)}</div></div>`}
async function refresh(){
 const r=await fetch('/sunpark/api/status'); if(!r.ok) throw Error(r.status); const d=await r.json();
 const stream=d.ingress.stream||{}; const services=d.services||{};
 document.querySelector('#cards').innerHTML=[
   card('Webhook',services.webhook?'ONLINE':'CHECK',services.webhook?'ok':'warn'),
   card('Stream',services.stream?'ONLINE':'STALE',services.stream?'ok':'bad'),
   card('Pending queue',d.pending), card('RPC failures',stream.rpc_failures||0,stream.rpc_failures?'warn':'ok')].join('');
   document.querySelector('#reject').innerHTML=(d.rejection?Object.entries(d.rejection).slice(0,12).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join(''):'')||'<tr><td colspan="2">No rejections yet</td></tr>';
  const g=await fetch('/sunpark/api/enrichment');const en=await g.json();
  document.querySelector('#enrich').innerHTML=Object.entries(en||{}).map(([k,v])=>card(k,`${v.ok}/${v.total}`,v.status==='ok'?'ok':(v.status==='degraded'?'warn':'bad'))).join('')||card('RPC enrichment','unavailable','bad');
  document.querySelector('#picks').innerHTML=(d.picks||[]).map(x=>`<tr><td>${esc(x.rank)}</td><td><strong>${esc(x.symbol||x.name||'Unknown')}</strong><br><code>${esc((x.mint||'').slice(0,10))}...</code></td><td>${esc(x.score)}</td><td>${esc((x.reasons||[]).join(', '))}</td><td class="signal-${esc(x.ai_signal||'IGNORE')}">${esc(x.ai_signal||'…')}${x.ai_confidence?' ('+esc(x.ai_confidence)+')':''}</td><td>${esc(x.ai_reason||'')}</td></tr>`).join('')||'<tr><td colspan="6">No picks yet</td></tr>';
    const p=await fetch('/sunpark/api/paper');const paper=await p.json();
    let solUsd=0;
    try{const sp=await fetch('https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd');const sj=await sp.json();solUsd=sj.solana?.usd||0;}catch(e){}
    const $=v=>solUsd?(v*solUsd).toFixed(0):'';
    document.querySelector('#paperCards').innerHTML=paper.open_positions!==undefined?[
     card('Honest Balance',paper.honest_balance_sol+' SOL'+(solUsd?' ($'+$(paper.honest_balance_sol)+')':''),(paper.honest_balance_sol>10?'ok':'warn')),
     card('Honest PnL',paper.honest_pnl_sol+' SOL'+(solUsd?' ($'+$(paper.honest_pnl_sol)+')':''),(paper.honest_pnl_sol>0?'ok':'bad')),
     card('Win rate',paper.honest_closed_count?Math.round(paper.honest_win_count/paper.honest_closed_count*100)+'%':'-',paper.honest_win_count>paper.honest_closed_count/2?'ok':'warn'),
     card('Open',paper.open_positions||0,paper.open_positions>5?'warn':''),
     card('Closed',paper.honest_closed_count||paper.closed_count||0),
     card('Wins',paper.honest_win_count||paper.win_count||0,(paper.honest_win_count||0)>0?'ok':'')
    ].join(''):'';
    document.querySelector('#paper').innerHTML=(paper.positions&&paper.positions.length?(paper.positions||[]).map(x=>`<tr><td><code>${esc((x.mint||'').slice(0,10))}...</code></td><td>${esc(x.state)}</td><td>${esc(x.entry_price_sol)}</td><td>${esc(x.peak_price_sol)}</td></tr>`).join(''):'<tr><td colspan="4">No open positions</td></tr>'):'<tr><td colspan="4">No paper trades yet</td></tr>');
  const e=await fetch('/sunpark/api/edge');const edge=await e.json();
  const s=edge.summary||{};
  document.querySelector('#edgeCards').innerHTML=[
   card('Outcome samples',s.samples||0), card('Resolved',s.resolved||0),
   card('Win rate (30m)',s.win_rate_pct==null?'-':s.win_rate_pct+'%',s.win_rate_pct>50?'ok':(s.win_rate_pct==null?'':'warn')),
   card('Avg +30m',s.avg_return_30m_pct==null?'-':s.avg_return_30m_pct+'%',(s.avg_return_30m_pct||0)>0?'ok':'warn'),
   card('Median +30m',s.median_return_30m_pct==null?'-':s.median_return_30m_pct+'%')].join('');
  function groupRows(obj){return Object.entries(obj||{}).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(v.samples??0)}</td><td>${esc(v.resolved??0)}</td><td>${v.win_rate_pct==null?'-':esc(v.win_rate_pct+'%')}</td><td>${v.avg_return_30m_pct==null?'-':esc(v.avg_return_30m_pct+'%')}</td><td>${v.median_return_30m_pct==null?'-':esc(v.median_return_30m_pct+'%')}</td></tr>`).join('')}
  ['mode','exit'].forEach(k=>document.querySelector('#edge-'+k).innerHTML=groupRows(edge['by_'+k])||'<tr><td colspan="6">No outcomes yet</td></tr>');
}
refresh().catch(e=>document.querySelector('#cards').innerHTML=card('Dashboard error',e.message,'bad')); setInterval(()=>refresh().catch(()=>{}),10000);
</script></body></html>"""


LOGS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SunPark Activity Logs</title>
<style>
body{margin:0;background:#10151b;color:#e7edf3;font:15px system-ui,sans-serif}main{max-width:1200px;margin:0 auto;padding:24px}
a{color:#a9d6ff}.muted{color:#91a0ad}table{width:100%;border-collapse:collapse;background:#18212a;margin-top:18px}
th,td{text-align:left;padding:10px;border-bottom:1px solid #2a3945;font-size:13px}th{color:#91a0ad}
.info{color:#b9c8d4}.warn{color:#f4c95d}.error{color:#ff7e7e}code{color:#a9d6ff}
.filters{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0;align-items:end}
.filters label{font-size:12px;color:#91a0ad;display:flex;flex-direction:column;gap:3px}
.filters input,.filters select{background:#1e293b;border:1px solid #2a3945;color:#e7edf3;padding:6px 8px;border-radius:4px;font-size:13px}
.filters button{background:#1e6c2f;border:none;color:#e7edf3;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px}
.filters button:hover{background:#24803a}
@media(max-width:650px){main{padding:14px}th,td{padding:8px 5px;font-size:11px}}
</style></head><body><main>
<h1>Activity Logs</h1><div class="muted"><a href="/sunpark/">Back to monitor</a> | Refreshes every 10 seconds.</div>
<div class="filters">
<label>Time range<select id="f-time"><option value="">All</option><option value="1">Last 1h</option><option value="6">Last 6h</option><option value="24" selected>Last 24h</option><option value="168">Last 7d</option></select></label>
<label>Level<select id="f-level"><option value="">All</option><option value="info">info</option><option value="warn">warn</option><option value="error">error</option></select></label>
<label>Source<select id="f-source"><option value="">All</option><option value="stream">stream</option><option value="worker">worker</option><option value="exits">exits</option><option value="metadata">metadata</option><option value="safety">safety</option><option value="holders">holders</option><option value="webhook">webhook</option></select></label>
<label>Search<input id="f-q" type="text" placeholder="message or details..." style="min-width:200px"></label>
<button onclick="refresh()">Filter</button>
</div>
<table><thead><tr><th>Time</th><th>Level</th><th>Source</th><th>Message</th><th>Details</th></tr></thead>
<tbody id="logs"><tr><td colspan="5">Loading...</td></tr></tbody></table>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function buildUrl(){const p=new URLSearchParams();const h=document.getElementById('f-time').value;if(h){const d=new Date(Date.now()-h*3600000).toISOString();p.set('since',d)}const lv=document.getElementById('f-level').value;if(lv)p.set('level',lv);const src=document.getElementById('f-source').value;if(src)p.set('source',src);const q=document.getElementById('f-q').value.trim();if(q)p.set('q',q);p.set('limit','300');return '/sunpark/api/logs?'+p.toString()}
async function refresh(){const r=await fetch(buildUrl());if(!r.ok)throw Error(r.status);const rows=await r.json();document.querySelector('#logs').innerHTML=rows.map(x=>`<tr><td>${esc(x.created_at)}</td><td class="${esc(x.level)}">${esc(x.level)}</td><td>${esc(x.source)}</td><td>${esc(x.message)}</td><td><code>${esc(x.meta||'')}</code></td></tr>`).join('')||'<tr><td colspan="5">No matching logs</td></tr>'}
document.getElementById('f-q').addEventListener('keydown',e=>{if(e.key==='Enter')refresh()});
refresh().catch(e=>document.querySelector('#logs').innerHTML=`<tr><td colspan="5">${esc(e.message)}</td></tr>`);setInterval(()=>refresh().catch(()=>{}),10000);
</script></main></body></html>"""


def status_payload():
    connection = get_db()
    try:
        stream = connection.execute(
            "SELECT stats_json, updated_at FROM ingress_stats WHERE source = 'stream'"
        ).fetchone()
        ingress = {}
        if stream:
            ingress["stream"] = json.loads(stream[0])
            ingress["stream"]["updated_at"] = stream[1]

        heavy = _cached("heavy", lambda: _heavy_stats(connection))
        pending = heavy["pending"]
        candidates_24h = heavy["candidates_24h"]
        ai_errors = heavy["ai_errors"]
        ai_latency = heavy["ai_latency"]
        rejection = heavy["rejection"]
        funnel = heavy["funnel"]
        recent_rows = _cached_recent(
            lambda: connection.execute(
                "SELECT signature, event_type, received_at, payload_json, analysis_json "
                "FROM candidates ORDER BY received_at DESC LIMIT 20"
            ).fetchall()
        )
        recent = []
        for signature, event_type, received_at, payload_json, analysis_json in recent_rows:
            payload = json.loads(payload_json)
            card = json.loads(analysis_json) if analysis_json else {}
            category = card.get("event_category", payload.get("event_category", event_type or "other"))
            decisions = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT track, signal, latency_ms FROM track_decisions WHERE signature = ?",
                    (signature,),
                )
            }
            rules = decisions.get("rules", ("-", None))
            ai = decisions.get("ai", ("-", None))
            gate_reasons = []
            try:
                allowed, gate_reasons = selection_gate(payload, card)
                if allowed:
                    gate_reasons = []
            except Exception:
                gate_reasons = []
            recent.append(
                {
                    "signature": signature,
                    "category": category,
                    "name": card.get("name"),
                    "symbol": card.get("symbol"),
                    "primary_mint": card.get("primary_mint"),
                    "received_at": received_at,
                    "rules_signal": rules[0],
                    "ai_signal": ai[0],
                    "ai_latency_ms": round(ai[1], 1) if ai[1] is not None else None,
                    "flags": ",".join(gate_reasons) if gate_reasons else None,
                }
            )
        return {
            "services": {
                "webhook": True,
                "stream": bool(stream),
            },
            "ingress": ingress,
            "pending": pending,
            "candidates_24h": candidates_24h,
            "ai_errors": ai_errors,
            "ai_avg_latency_ms": round(ai_latency, 1),
            "recent": recent,
            "rejection": rejection,
            "funnel": funnel,
            "picks": picks_payload(),
        }
    finally:
        connection.close()


def enrichment_payload():
    """Background RPC cache health: holders, safety, metadata, price coverage."""
    connection = get_db()
    try:
        def status_counts(table):
            total = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            ok_rows = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE status = 'ok'"
            ).fetchone()[0]
            status = "ok" if ok_rows else ("degraded" if total else "idle")
            return {"total": total, "ok": ok_rows, "status": status}

        holders = status_counts("token_holders")
        safety = status_counts("token_safety")
        metadata = status_counts("token_meta")
        rows = connection.execute("SELECT stats_json FROM mint_stats").fetchall()
        with_price = sum(
            1
            for (payload,) in rows
            if (json.loads(payload).get("price_sol") or 0) > 0
        )
        prices = {
            "total": len(rows),
            "ok": with_price,
            "status": "ok" if with_price else ("idle" if not rows else "degraded"),
        }
        return {"holders": holders, "safety": safety, "metadata": metadata, "prices": prices}
    finally:
        connection.close()


def top_mints_payload(limit=12):
    try:
        limit = max(1, min(int(limit), 30))
    except (TypeError, ValueError):
        limit = 12
    rows = rollup.top_mints(300, limit=limit)
    names = {}
    mints = [row["mint"] for row in rows]
    if mints:
        connection = get_db()
        try:
            names = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    "SELECT mint, name, symbol FROM token_meta WHERE mint IN (%s)"
                    % ",".join("?" * len(mints)),
                    mints,
                )
            }
        finally:
            connection.close()
    result = []
    for row in rows:
        name, symbol = names.get(row["mint"], (None, None))
        result.append(
            {
                "mint": row["mint"],
                "name": name,
                "symbol": symbol,
                "vol_5m_sol": row.get("vol_sol"),
                "buy_ratio_5m": (
                    round(row.get("buy_sol", 0) / row.get("sell_sol", 0), 2)
                    if row.get("sell_sol") and row.get("sell_sol") > 0
                    else None
                ),
                "net_5m_sol": row.get("net_sol"),
                "unique_buyers_5m": row.get("unique_buyers"),
                "unique_sellers_5m": row.get("unique_sellers"),
                "age_minutes": (
                    round((row.get("age_seconds") or 0) / 60, 1)
                    if row.get("age_seconds") is not None
                    else None
                ),
                "initial_liquidity_sol": row.get("initial_liquidity_sol"),
            }
        )
    return result


def rejection_counts(connection, hours=24):
    """Selection-gate rejection reasons over the last window, counted."""
    counts = {}
    rows = connection.execute(
        "SELECT reason FROM track_decisions "
        "WHERE track = 'ai' AND status = 'filtered' "
        "AND datetime(created_at) >= datetime('now', ?)",
        (f"-{int(hours)} hours",),
    ).fetchall()
    for (reason,) in rows:
        for item in str(reason).split(","):
            item = item.strip()
            if item:
                counts[item] = counts.get(item, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: -pair[1]))


def migrations_payload():
    """DEX migration statistics: graduated tokens, forward returns, dev filter analysis."""
    cached = _STATUS_CACHE["migrations"]
    if time.time() - cached[0] < _MIGRATIONS_CACHE_TTL and cached[1]:
        return cached[1]

    connection = get_db()
    try:
        now = time.time()
        rows = connection.execute("""
            SELECT
                r.mint, r.creator, r.graduated_at, r.status,
                o.entry_time, o.entry_price_sol,
                o.price_5m, o.price_30m,
                o.return_5m_pct, o.return_30m_pct,
                o.peak_price_sol, o.peak_pct,
                o.exit_reason
            FROM token_registry r
            LEFT JOIN outcomes o ON r.mint = o.mint AND o.kind = 'pick'
            WHERE r.status = 'migrated'
            ORDER BY r.graduated_at DESC
            LIMIT 200
        """).fetchall()

        total_grad = len(rows)
        with_outcome = [r for r in rows if r["entry_time"] is not None]

        peak_2x = sum(1 for r in with_outcome if r["peak_pct"] and r["peak_pct"] >= 100)
        ret30_2x = sum(1 for r in with_outcome if r["return_30m_pct"] and r["return_30m_pct"] >= 100)
        ret30_3x = sum(1 for r in with_outcome if r["return_30m_pct"] and r["return_30m_pct"] >= 200)
        returns_30m = [r["return_30m_pct"] for r in with_outcome if r["return_30m_pct"] is not None]

        avg_ret = round(sum(returns_30m) / len(returns_30m), 2) if returns_30m else None
        med_ret = round(sorted(returns_30m)[len(returns_30m) // 2], 2) if returns_30m else None

        dev_pass_count = 0
        dev_pass_wins = 0
        dev_fail_count = 0
        dev_fail_wins = 0

        for r in with_outcome:
            creator = r["creator"]
            if not creator:
                continue
            grad_row = connection.execute("""
                SELECT COUNT(*) as gc, MAX(graduated_at) as lg
                FROM token_registry WHERE creator = ? AND status = 'migrated'
            """, (creator,)).fetchone()
            gc = grad_row["gc"] or 0
            lg = grad_row["lg"]
            quiet = (now - lg) / 86400 if lg else None
            passes = gc >= 1 and quiet is not None and quiet >= 7
            if passes:
                dev_pass_count += 1
                if r["return_30m_pct"] and r["return_30m_pct"] >= 100:
                    dev_pass_wins += 1
            elif gc >= 1:
                dev_fail_count += 1
                if r["return_30m_pct"] and r["return_30m_pct"] >= 100:
                    dev_fail_wins += 1

        recent = []
        for r in rows[:15]:
            grad_ts = r["graduated_at"]
            age_min = round((now - grad_ts) / 60, 0) if grad_ts else None
            recent.append({
                "mint": r["mint"],
                "creator": r["creator"],
                "graduated_ago_min": age_min,
                "return_5m_pct": r["return_5m_pct"],
                "return_30m_pct": r["return_30m_pct"],
                "peak_pct": r["peak_pct"],
                "exit_reason": r["exit_reason"],
            })

        result = {
            "total_graduations": total_grad,
            "with_outcome": len(with_outcome),
            "peak_2x_count": peak_2x,
            "ret30_2x_count": ret30_2x,
            "ret30_3x_count": ret30_3x,
            "peak_2x_pct": round(peak_2x / len(with_outcome) * 100, 1) if with_outcome else None,
            "ret30_2x_pct": round(ret30_2x / len(with_outcome) * 100, 1) if with_outcome else None,
            "avg_return_30m": avg_ret,
            "median_return_30m": med_ret,
            "dev_pass_count": dev_pass_count,
            "dev_pass_wins": dev_pass_wins,
            "dev_pass_win_rate": round(dev_pass_wins / dev_pass_count * 100, 1) if dev_pass_count else None,
            "dev_fail_count": dev_fail_count,
            "dev_fail_wins": dev_fail_wins,
            "dev_fail_win_rate": round(dev_fail_wins / dev_fail_count * 100, 1) if dev_fail_count else None,
            "recent": recent,
        }
        _STATUS_CACHE["migrations"] = (time.time(), result)
        return result
    except Exception:
        return {"total_graduations": 0, "with_outcome": 0, "recent": []}
    finally:
        connection.close()


def picks_payload():
    picks = load_picks(10)
    if not picks:
        return []
    names = {}
    mints = [pick["mint"] for pick in picks]
    connection = get_db()
    try:
        names = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                "SELECT mint, name, symbol FROM token_meta WHERE mint IN (%s)"
                % ",".join("?" * len(mints)),
                mints,
            )
        }
    finally:
        connection.close()
    result = []
    for pick in picks:
        name, symbol = names.get(pick["mint"], (None, None))
        result.append(
            {
                "mint": pick["mint"],
                "name": name,
                "symbol": symbol,
                "score": pick["score"],
                "rank": pick["rank"],
                "reasons": pick["reasons"],
                "ai_signal": pick["ai_signal"],
                "ai_confidence": pick["ai_confidence"],
                "ai_reason": pick["ai_reason"],
                "ai_latency_ms": pick["ai_latency_ms"],
                "updated_at": pick["updated_at"],
            }
        )
    return result


def paper_payload():
    try:
        from exits import paper

        summary = paper.honest_summary()
        positions = []
        for mint, position in paper.positions.items():
            snapshot = rollup.stats_for(mint) or {}
            positions.append(
                {
                    "mint": mint,
                    "state": position.state,
                    "entry_price_sol": position.entry_price_sol,
                    "current_price_sol": snapshot.get("price_sol"),
                    "size_sol": round(position.size_sol, 4),
                    "peak_price_sol": position.peak_price_sol,
                }
            )
        summary["positions"] = positions
        return summary
    except Exception:
        return {}


def edge_payload():
    try:
        from outcomes import outcomes_summary

        return outcomes_summary()
    except Exception:
        return {}


def logs_payload(limit=150, level=None, source=None, since=None, q=None):
    try:
        limit = max(1, min(int(limit), 300))
    except (TypeError, ValueError):
        limit = 150
    query = "SELECT created_at, level, source, message, meta_json FROM activity_logs"
    clauses = []
    values = []
    if level:
        clauses.append("level = ?")
        values.append(level[:20])
    if source:
        clauses.append("source = ?")
        values.append(source[:40])
    if since:
        clauses.append("created_at >= ?")
        values.append(since)
    if q:
        like = f"%{q}%"
        clauses.append("(message LIKE ? OR meta_json LIKE ?)")
        values.extend([like, like])
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    values.append(limit)

    connection = get_db()
    try:
        rows = connection.execute(query, values).fetchall()
        return [
            {
                "created_at": row[0],
                "level": row[1],
                "source": row[2],
                "message": row[3],
                "meta": row[4] or "",
            }
            for row in rows
        ]
    finally:
        connection.close()
