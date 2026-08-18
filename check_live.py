import sys
sys.path.insert(0, "/var/www/sunpark")
from exits import paper
from storage import load_paper_trades, load_paper_positions

# Current state
h = paper.honest_summary()
print("=== CURRENT STATE ===")
print(f"  Raw balance:    {h['balance_sol']} SOL")
print(f"  Honest balance: {h['honest_balance_sol']} SOL")
print(f"  Honest PnL:     {h['honest_pnl_sol']} SOL")
print(f"  Open positions: {h['open_positions']}")

print("\n=== OPEN POSITIONS ===")
pos = load_paper_positions()
for p in pos:
    print(f"  {p['mint'][:12]}  state={p['state']}  entry={p['entry_price_sol']:.2e}  size={p['size_sol']}")

print("\n=== RECENT TRADES (last 5) ===")
trades = load_paper_trades(limit=5)
for t in trades:
    src = t.get("entry_source", "?")
    wash = " [WASH]" if t.get("is_wash") else ""
    phantom = " [PHANTOM]" if t.get("is_phantom") else ""
    print(f"  {t['mint'][:12]}  {t['action']:6s}  pnl={t['pnl_sol']:>10.4f}  {t['reason']:20s}  src={src}{wash}{phantom}")

print("\n=== RECENT HONEST WINS ===")
trades_all = load_paper_trades(limit=200)
wins = [t for t in trades_all if t.get("action") in ("close","tp1","tp2") and not t.get("is_wash") and not t.get("is_phantom") and t.get("pnl_sol",0) > 0]
wins.sort(key=lambda x: x.get("pnl_sol",0), reverse=True)
for t in wins[:5]:
    print(f"  {t['mint'][:12]}  {t['action']:6s}  pnl={t['pnl_sol']:>10.4f}  {t['reason']:20s}  {t['created_at']}")
