#!/usr/bin/env python3
"""Kiểm tra rewrite SQL SQLite → PostgreSQL."""
from __future__ import annotations

import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, BASE)
os.environ['SME_DB_BACKEND'] = 'postgres'

from db.sql_compat import rewrite_sql_for_postgres  # noqa: E402

CASES = [
    ("PRAGMA table_info(products)", 'column_name'),
    ("PRAGMA foreign_keys=OFF", '_pragma_ok'),
    ("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", 'information_schema'),
    ("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", 'information_schema'),
    ("SELECT name FROM sqlite_master WHERE type='table' AND name='supplier_invoice'", 'information_schema'),
    ("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", 'ON CONFLICT'),
    ("INSERT OR IGNORE INTO inventory (product_id, quantity, avg_cost) VALUES (?, 0, 0)", 'DO NOTHING'),
    ("INSERT OR IGNORE INTO voucher_seq (type, seq) VALUES ('PT', 0), ('PC', 0), ('PN', 0), ('PX', 0)", 'ON CONFLICT'),
    ("SELECT last_insert_rowid()", 'lastval'),
    ("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, d DATETIME)", 'TIMESTAMP'),
    ("UPDATE sale_items SET id = rowid WHERE id IS NULL", '_rowid_ok'),
    ("SELECT value FROM settings WHERE key = ?", '%s'),
    ("DELETE FROM sqlite_sequence WHERE name=?", '_seq_ok'),
    ("SELECT IFNULL(MAX(id),0) FROM sale", 'COALESCE'),
    ("COALESCE(s.sale_no, 'DH' || printf('%06d', s.id))", 'lpad'),
    ("ORDER BY s.date DESC, si.rowid", 'si.id'),
    (
        "SELECT rowid, product_name, unit FROM sale_items "
        "WHERE sale_id = ? AND product_id = ? ORDER BY rowid DESC LIMIT 1",
        "SELECT id AS rowid",
    ),
    ("UPDATE sale_items SET unit = ? WHERE rowid = ?", "WHERE id = %s"),
    (
        "SELECT COALESCE(id, rowid) AS sale_item_id FROM sale_items "
        "WHERE COALESCE(id, rowid) = ?",
        "COALESCE(id, id)",
    ),
    ("ORDER BY fullname COLLATE NOCASE", 'fullname'),
    ("WHERE date(v.punched_at) = date('now', 'localtime')", "TO_CHAR"),
    ("date('now', 'localtime', '-30 day')", 'INTERVAL'),
    ("AND date(v.punched_at) = date(?)", 'substr(CAST'),
    ("GROUP_CONCAT(x.account_code, ', ')", 'string_agg'),
    ("INSERT OR IGNORE INTO crm_assign_state (id, last_owner_index, owners_csv) VALUES (1, -1, '')", 'ON CONFLICT'),
    ("PRAGMA database_list", 'main'),
    ('ORDER BY CASE o.stage WHEN "won" THEN 2', "WHEN 'won'"),
    ("WHERE code GLOB 'KHO_[0-9]*'", ' ~ '),
    ("substr(entry_no, 3) GLOB '[0-9]*'", ' ~ '),
    ("date(?, '-1 day')", 'INTERVAL'),
    ("CAST(COALESCE(status, 1) AS TEXT)", "CAST(status AS text)"),
    ("date(COALESCE(NULLIF(TRIM(si.invoice_date), ''), si.date))", "CAST((si.date) AS text)"),
    ("julianday('now') - julianday(COALESCE(opened_at, created_at))", 'COALESCE(opened_at, created_at)'),
    ("date('now', 'localtime', ?)", 'CAST(%s AS interval)'),
    ("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? COLLATE NOCASE LIMIT 1", 'information_schema'),
    ("NOT LIKE 'DV%' AND name = ?", "DV%%"),
    ("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", "%s"),
    ("UPDATE sqlite_sequence SET seq = ? WHERE name = 'sale'", "_seq_ok"),
]

failed = 0
for sql, needle in CASES:
    out = rewrite_sql_for_postgres(sql, schema='t_demo')
    if needle not in out:
        print(f'FAIL: {sql!r}\n  -> {out!r}')
        failed += 1
    else:
        print(f'OK: {sql[:50]}...')

# Regression: bare rowid trong sale_items
_rowid_select = rewrite_sql_for_postgres(
    "SELECT rowid, product_name, unit FROM sale_items "
    "WHERE sale_id = ? AND product_id = ? ORDER BY rowid DESC LIMIT 1",
    schema='t_demo',
)
if (
    'SELECT id AS rowid' not in _rowid_select
    or 'ORDER BY id DESC' not in _rowid_select
    or _rowid_select.count('%s') != 2
):
    print(f'FAIL sale_items bare rowid SELECT:\n  -> {_rowid_select!r}')
    failed += 1
else:
    print('OK: sale_items bare rowid SELECT -> id AS rowid...')

_rowid_where = rewrite_sql_for_postgres(
    "UPDATE sale_items SET product_name = ? WHERE rowid = ?",
    schema='t_demo',
)
if 'WHERE id = %s' not in _rowid_where or 'rowid' in _rowid_where.lower():
    print(f'FAIL sale_items bare rowid WHERE:\n  -> {_rowid_where!r}')
    failed += 1
else:
    print('OK: sale_items bare rowid WHERE -> id...')

_rowid_coalesce = rewrite_sql_for_postgres(
    "SELECT COALESCE(id, rowid) AS sale_item_id FROM sale_items "
    "WHERE COALESCE(id, rowid) = ?",
    schema='t_demo',
)
if 'COALESCE(id, id)' not in _rowid_coalesce or 'rowid' in _rowid_coalesce.lower():
    print(f'FAIL sale_items COALESCE(id,rowid):\n  -> {_rowid_coalesce!r}')
    failed += 1
else:
    print('OK: sale_items COALESCE(id,rowid) -> COALESCE(id,id)...')

_legacy_rowid = rewrite_sql_for_postgres("SELECT rowid FROM legacy_table", schema='t_demo')
if 'rowid' not in _legacy_rowid.lower():
    print(f'FAIL rowid scope guard:\n  -> {_legacy_rowid!r}')
    failed += 1
else:
    print('OK: bare rowid ngoài sale_items không bị rewrite...')

# Idempotent: rewrite lần 2 không biến %s → %%s (0 placeholders)
once = rewrite_sql_for_postgres(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
    schema='t_demo',
)
twice = rewrite_sql_for_postgres(once, schema='t_demo')
if twice.count('%s') != once.count('%s') or '%%s' in twice:
    print(f'FAIL idempotent adapt:\n  once={once!r}\n  twice={twice!r}')
    failed += 1
else:
    print('OK: idempotent rewrite (no %%s)...')

# DECIMAL(18,2) AS generated
from db.sql_compat import convert_sqlite_ddl
ddl_out = convert_sqlite_ddl(
    "remaining_amount DECIMAL(18, 2) AS (unpaid_amount - paid_amount) VIRTUAL"
)
if 'GENERATED ALWAYS AS' not in ddl_out.upper():
    print(f'FAIL DECIMAL AS: {ddl_out!r}')
    failed += 1
else:
    print('OK: DECIMAL(18,2) AS -> GENERATED...')

if failed:
    print(f'\n{failed} failed')
    sys.exit(1)
print('\nAll SQL compat checks passed.')
