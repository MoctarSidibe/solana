import sqlite3, json

conn = sqlite3.connect("/var/www/sunpark/data/events.sqlite")
conn.row_factory = sqlite3.Row

print("=== paper_state ===")
for r in conn.execute("SELECT * FROM paper_state").fetchall():
    print(json.dumps(dict(r), default=str))

print()
print("=== paper_trades count ===")
for r in conn.execute("SELECT action, COUNT(*) as c FROM paper_trades GROUP BY action").fetchall():
    print(r["action"], r["c"])

print()
print("=== last 5 closes ===")
for r in conn.execute('SELECT * FROM paper_trades WHERE action="close" ORDER BY id DESC LIMIT 5').fetchall():
    print(json.dumps(dict(r), default=str))

print()
print("=== paper_positions ===")
for r in conn.execute("SELECT * FROM paper_positions").fetchall():
    print(json.dumps(dict(r), default=str))

print()
print("=== last 5 opens ===")
for r in conn.execute('SELECT * FROM paper_trades WHERE action="open" ORDER BY id DESC LIMIT 5').fetchall():
    print(json.dumps(dict(r), default=str))

print()
print("=== last 5 partials (tp1/tp2) ===")
for r in conn.execute('SELECT * FROM paper_trades WHERE action IN ("tp1","tp2") ORDER BY id DESC LIMIT 5').fetchall():
    print(json.dumps(dict(r), default=str))

conn.close()
