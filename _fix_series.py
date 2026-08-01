import sqlite3

c = sqlite3.connect("tenants/sme_demo.db")
print(c.execute("PRAGMA table_info(invoice_settings)").fetchall())
r = c.execute(
    "SELECT invoice_series, invoice_type FROM invoice_settings WHERE is_active=1"
).fetchone()
print("before", repr(r[0]), repr(r[1]))
c.execute(
    "UPDATE invoice_settings SET invoice_series=TRIM(invoice_series), invoice_type=TRIM(invoice_type) WHERE is_active=1"
)
c.commit()
r = c.execute(
    "SELECT invoice_series, invoice_type FROM invoice_settings WHERE is_active=1"
).fetchone()
print("after", repr(r[0]), repr(r[1]))
c.close()
