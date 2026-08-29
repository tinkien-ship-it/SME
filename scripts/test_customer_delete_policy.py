# -*- coding: utf-8 -*-
"""Xóa KH: cho phép khi chỉ tiếp cận; chặn khi đã có giao dịch."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.customers import (
    _cleanup_customer_crm_links,
    _customer_transaction_reason,
)


def _db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE sale (
            id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT
        );
        CREATE TABLE crm_activities (
            id INTEGER PRIMARY KEY, customer_id INTEGER
        );
        CREATE TABLE crm_opportunities (
            id INTEGER PRIMARY KEY, customer_id INTEGER
        );
        CREATE TABLE crm_quotes (
            id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT, sale_id INTEGER
        );
        CREATE TABLE crm_contracts (
            id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT
        );
        CREATE TABLE crm_leads (
            id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT, updated_at TEXT
        );
        CREATE TABLE crm_visit_checkins (
            id INTEGER PRIMARY KEY, customer_id INTEGER
        );
        """
    )
    return conn


def test_allow_approached_only():
    conn = _db()
    conn.execute('INSERT INTO customers (id, name) VALUES (1, "A")')
    conn.execute('INSERT INTO crm_activities (customer_id) VALUES (1)')
    conn.execute('INSERT INTO crm_opportunities (customer_id) VALUES (1)')
    conn.execute('INSERT INTO crm_visit_checkins (customer_id) VALUES (1)')
    conn.execute('INSERT INTO crm_quotes (customer_id, status) VALUES (1, "draft")')
    conn.execute('INSERT INTO crm_contracts (customer_id, status) VALUES (1, "draft")')
    assert _customer_transaction_reason(conn, 1) is None
    print('OK allow approached')


def test_block_sale():
    conn = _db()
    conn.execute('INSERT INTO customers (id, name) VALUES (1, "A")')
    conn.execute('INSERT INTO sale (customer_id, status) VALUES (1, "completed")')
    assert 'mua hàng' in (_customer_transaction_reason(conn, 1) or '')
    print('OK block sale')


def test_block_signed_contract():
    # Hợp đồng không còn chặn xóa — chỉ mua hàng mới chặn
    conn = _db()
    conn.execute('INSERT INTO customers (id, name) VALUES (1, "A")')
    conn.execute('INSERT INTO crm_contracts (customer_id, status) VALUES (1, "signed")')
    assert _customer_transaction_reason(conn, 1) is None
    print('OK contract alone does not block')


def test_cleanup_on_delete():
    conn = _db()
    conn.execute('INSERT INTO customers (id, name) VALUES (1, "A")')
    conn.execute('INSERT INTO crm_activities (customer_id) VALUES (1)')
    conn.execute('INSERT INTO crm_leads (customer_id, status) VALUES (1, "converted")')
    _cleanup_customer_crm_links(conn, 1)
    assert conn.execute('SELECT COUNT(*) FROM crm_activities').fetchone()[0] == 0
    lead = conn.execute('SELECT customer_id, status FROM crm_leads').fetchone()
    assert lead['customer_id'] is None
    assert lead['status'] == 'qualified'
    print('OK cleanup')


if __name__ == '__main__':
    test_allow_approached_only()
    test_block_sale()
    test_block_signed_contract()
    test_cleanup_on_delete()
    print('All customer delete policy tests passed.')
