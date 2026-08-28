# -*- coding: utf-8 -*-
"""Smoke test: CRM visit check-in/out (ESS flow)."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Services.crm_schema import ensure_crm_schema
from Services import crm_visits


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT,
            company_name TEXT,
            phone TEXT,
            address TEXT,
            crm_owner TEXT,
            crm_next_contact_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO customers (id, name, company_name, phone, address, crm_owner, crm_next_contact_at)
        VALUES (1, 'An', 'Cty ABC', '090111', '123 Nguyen Trai, Q1, HCM', 'sale_a', datetime('now'))
        """
    )
    ensure_crm_schema(conn)
    return conn


def test_customer_display_labels():
    conn = _mem_db()
    items = crm_visits.list_visit_customers(conn, 'sale_a')
    assert items[0]['label'] == 'Cty ABC'
    assert items[0]['representative'] == 'An'
    print('OK test_customer_display_labels')


def test_checkin_checkout_flow():
    conn = _mem_db()
    row_in = crm_visits.visit_checkin(
        conn,
        {'customer_id': 1, 'check_type': 'in', 'lat': 10.77, 'lng': 106.69, 'accuracy': 12},
        owner='sale_a',
        employee_id=5,
    )
    assert row_in['check_type'] == 'in'
    assert row_in['visit_session_id']
    sid = row_in['visit_session_id']

    items = crm_visits.list_visit_customers(conn, 'sale_a')
    assert items[0]['visit_status'] == 'open'

    row_out = crm_visits.visit_checkin(
        conn,
        {
            'customer_id': 1,
            'check_type': 'out',
            'visit_session_id': sid,
            'note': 'Da bao gia san pham A, khach hen goi lai',
        },
        owner='sale_a',
        employee_id=5,
    )
    assert row_out['check_type'] == 'out'
    assert row_out.get('crm_activity_id')

    sessions = crm_visits.list_visit_sessions_today(conn)
    assert len(sessions) == 1
    assert sessions[0]['status'] == 'done'
    print('OK test_checkin_checkout_flow')


def test_owner_mismatch():
    conn = _mem_db()
    try:
        crm_visits.visit_checkin(
            conn,
            {'customer_id': 1, 'check_type': 'in', 'lat': 1, 'lng': 2},
            owner='other_user',
        )
        assert False, 'expected ValueError'
    except ValueError as e:
        assert 'phụ trách' in str(e).lower() or 'Khách' in str(e)
    print('OK test_owner_mismatch')


def test_checkout_requires_note():
    conn = _mem_db()
    row_in = crm_visits.visit_checkin(
        conn,
        {'customer_id': 1, 'check_type': 'in'},
        owner='sale_a',
    )
    sid = row_in['visit_session_id']
    try:
        crm_visits.visit_checkin(
            conn,
            {'customer_id': 1, 'check_type': 'out', 'visit_session_id': sid, 'note': 'ab'},
            owner='sale_a',
        )
        assert False, 'expected ValueError'
    except ValueError as e:
        assert 'gặp khách' in str(e).lower() or 'ký tự' in str(e).lower()
    print('OK test_checkout_requires_note')


def test_crm_visit_schema_twice():
    from Services.crm_schema import ensure_crm_schema
    conn = _mem_db()
    ensure_crm_schema(conn)
    ensure_crm_schema(conn)
    print('OK test_crm_visit_schema_twice')


def test_double_checkin_blocked():
    conn = _mem_db()
    crm_visits.visit_checkin(conn, {'customer_id': 1, 'check_type': 'in'}, owner='sale_a')
    try:
        crm_visits.visit_checkin(conn, {'customer_id': 1, 'check_type': 'in'}, owner='sale_a')
        assert False, 'expected ValueError'
    except ValueError as e:
        assert 'check-out' in str(e).lower() or 'check-in' in str(e).lower()
    print('OK test_double_checkin_blocked')


if __name__ == '__main__':
    test_customer_display_labels()
    test_checkin_checkout_flow()
    test_checkout_requires_note()
    test_owner_mismatch()
    test_crm_visit_schema_twice()
    test_double_checkin_blocked()
    print('All CRM visit tests passed.')
