import sqlite3
from Services.sme.general_ledger import accounts_with_activity, account_ledger, _level1_account

conn = sqlite3.connect("tenants/sme_demo.db")
conn.row_factory = sqlite3.Row
out = []
rows = conn.execute(
    "SELECT code, level, parent_code FROM sme_chart_of_accounts WHERE level>1 LIMIT 3"
).fetchall()
for row in rows:
    l1 = _level1_account(conn, row["code"])
    out.append(
        f"{row['code']} -> {l1['code'] if l1 else None} L{l1['level'] if l1 else None}"
    )
accs = accounts_with_activity(conn)
out.append("L1 count=" + str(len(accs)) + " sample=" + str([a["code"] for a in accs[:10]]))
assert all(a["level"] == 1 for a in accs)
if accs:
    code = accs[0]["code"]
    child = conn.execute(
        "SELECT code FROM sme_chart_of_accounts WHERE parent_code=? LIMIT 1", (code,)
    ).fetchone()
    q = child["code"] if child else code
    led = account_ledger(conn, q, date_from="2020-01-01", date_to="2030-12-31")
    out.append(
        f"ledger q={q} resolved={led['account']['code']} lines={led['line_count']} "
        f"kids={led['account'].get('includes_children')}"
    )
conn.close()
text = "\n".join(out)
open("_smoke.txt", "w", encoding="utf-8").write(text)
print(text)
