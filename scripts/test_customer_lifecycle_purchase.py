# -*- coding: utf-8 -*-
"""Xóa KH + trạng thái: chỉ mua hàng mới chặn xóa / mới là Đang giao dịch."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Services.crm_schema import ensure_crm_schema
from Services import crm as crm_svc
from routes.customers import _customer_transaction_reason, _enrich_customer_row


def _db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_crm_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY, name TEXT, phone TEXT,
            crm_lifecycle TEXT DEFAULT 'prospect'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sale (
            id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT
        )
        """
    )
    return conn


def test_no_purchase_can_delete_and_prospect():
    conn = _db()
    conn.execute("INSERT INTO customers (id, name, crm_lifecycle) VALUES (1, 'A', 'active')")
    row = _enrich_customer_row(conn, conn.execute('SELECT * FROM customers WHERE id=1').fetchone())
    assert row['can_delete'] is True
    assert row['crm_lifecycle'] == 'prospect'
    assert row['has_purchase'] is False
    assert _customer_transaction_reason(conn, 1) is None
    print('OK no purchase')


def test_purchase_blocks_and_active():
    conn = _db()
    conn.execute("INSERT INTO customers (id, name, crm_lifecycle) VALUES (1, 'A', 'prospect')")
    conn.execute("INSERT INTO sale (customer_id, status) VALUES (1, 'completed')")
    row = _enrich_customer_row(conn, conn.execute('SELECT * FROM customers WHERE id=1').fetchone())
    assert row['can_delete'] is False
    assert row['crm_lifecycle'] == 'active'
    assert 'mua hàng' in (row['delete_block_reason'] or '')
    print('OK has purchase')


def test_cannot_force_active_without_purchase():
    conn = _db()
    # đủ cột cho update_customer_crm
    for col, typ in (
        ('crm_source', 'TEXT'), ('crm_owner', 'TEXT'), ('crm_segment', 'TEXT'),
        ('crm_notes', 'TEXT'), ('crm_next_contact_at', 'TEXT'), ('crm_tags', 'TEXT'),
        ('crm_updated_at', 'TEXT'),
    ):
        try:
            conn.execute(f'ALTER TABLE customers ADD COLUMN {col} {typ}')
        except sqlite3.Error:
            pass
    conn.execute("INSERT INTO customers (id, name, crm_lifecycle) VALUES (1, 'A', 'prospect')")
    crm_svc.update_customer_crm(conn, 1, {'crm_lifecycle': 'active'})
    stored = conn.execute('SELECT crm_lifecycle FROM customers WHERE id=1').fetchone()[0]
    assert stored == 'prospect'
    print('OK coerce active to prospect')


if __name__ == '__main__':
    test_no_purchase_can_delete_and_prospect()
    test_purchase_blocks_and_active()
    test_cannot_force_active_without_purchase()
    print('All lifecycle/delete tests passed.')
