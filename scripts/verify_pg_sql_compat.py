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
    ("ORDER BY fullname COLLATE NOCASE", 'fullname'),
    ("WHERE date(v.punched_at) = date('now', 'localtime')", "TO_CHAR"),
    ("date('now', 'localtime', '-30 day')", 'INTERVAL'),
    ("AND date(v.punched_at) = date(?)", 'LEFT(BTRIM'),
    ("GROUP_CONCAT(x.account_code, ', ')", 'string_agg'),
    ("INSERT OR IGNORE INTO crm_assign_state (id, last_owner_index, owners_csv) VALUES (1, -1, '')", 'ON CONFLICT'),
    ("PRAGMA database_list", 'main'),
]

failed = 0
for sql, needle in CASES:
    out = rewrite_sql_for_postgres(sql, schema='t_demo')
    if needle not in out:
        print(f'FAIL: {sql!r}\n  -> {out!r}')
        failed += 1
    else:
        print(f'OK: {sql[:50]}...')

if failed:
    print(f'\n{failed} failed')
    sys.exit(1)
print('\nAll SQL compat checks passed.')
