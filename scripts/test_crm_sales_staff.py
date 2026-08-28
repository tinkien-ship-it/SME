# -*- coding: utf-8 -*-
"""Smoke test: CRM sales staff round-robin chỉ lấy role staff từ Settings."""
from __future__ import annotations

import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Services.crm_schema import ensure_crm_schema
from Services import crm_ops


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            role TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO users (id, username, full_name, role) VALUES (?, ?, ?, ?)",
        [
            (1, 'sale_a', 'Nguyen A', 'staff'),
            (2, 'sale_b', 'Tran B', 'staff'),
            (3, 'admin1', 'Quan tri', 'admin'),
            (4, 'ess1', 'NV ESS', 'employee'),
            (5, 'fb1', 'NV F&B', 'staff**'),
        ],
    )
    ensure_crm_schema(conn)
    return conn


def test_list_only_staff():
    conn = _mem_db()
    staff = crm_ops.list_crm_sales_staff(conn)
    names = [u['username'] for u in staff]
    assert names == ['sale_a', 'sale_b'], names
    assert all(u['role_label'] == 'NV Bán hàng' for u in staff)
    print('OK test_list_only_staff')


def test_sync_and_round_robin():
    conn = _mem_db()
    crm_ops.set_setting(conn, 'assign_owners', 'sale_b,admin1,old_user')
    owners = crm_ops.sync_assign_owners_from_staff(conn)
    assert owners == ['sale_b', 'sale_a'], owners

    first = crm_ops.next_assignee(conn)
    second = crm_ops.next_assignee(conn)
    third = crm_ops.next_assignee(conn)
    assert (first, second, third) == ('sale_b', 'sale_a', 'sale_b'), (first, second, third)
    print('OK test_sync_and_round_robin')


def test_inbound_lead_owner():
    conn = _mem_db()
    crm_ops.sync_assign_owners_from_staff(conn)
    result = crm_ops.create_inbound_lead(
        conn,
        {'contact_name': 'Khach', 'phone': '090', 'source': 'Test'},
        auto_assign=True,
    )
    assert result['owner'] in ('sale_a', 'sale_b'), result
    print('OK test_inbound_lead_owner', result)


if __name__ == '__main__':
    test_list_only_staff()
    test_sync_and_round_robin()
    test_inbound_lead_owner()
    print('All CRM sales staff tests passed.')
