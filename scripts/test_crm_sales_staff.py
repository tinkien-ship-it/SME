# -*- coding: utf-8 -*-
"""Smoke test: CRM sales staff round-robin — staff_field trước, staff quầy sau."""
from __future__ import annotations

import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Services.crm_schema import ensure_crm_schema
from Services import crm_ops
from Services.login_service import login_redirect_target
from Services.hrm.ess_access import (
    can_ess_customer_visits,
    ess_portal_path_allowed,
    is_ess_portal_only_user,
)


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
            (2, 'field_a', 'Field A', 'staff_field'),
            (3, 'admin1', 'Quan tri', 'admin'),
            (4, 'ess1', 'NV ESS', 'employee'),
            (5, 'fb1', 'NV F&B', 'staff**'),
            (6, 'field_b', 'Field B', 'staff_field'),
        ],
    )
    ensure_crm_schema(conn)
    return conn


def test_list_field_then_staff():
    conn = _mem_db()
    staff = crm_ops.list_crm_sales_staff(conn)
    names = [u['username'] for u in staff]
    assert names == ['field_a', 'field_b', 'sale_a'], names
    assert staff[0]['role'] == 'staff_field'
    assert staff[-1]['role'] == 'staff'
    print('OK test_list_field_then_staff')


def test_sync_and_round_robin():
    conn = _mem_db()
    crm_ops.set_setting(conn, 'assign_owners', 'sale_a,admin1,old_user')
    owners = crm_ops.sync_assign_owners_from_staff(conn)
    assert owners == ['sale_a', 'field_a', 'field_b'], owners

    first = crm_ops.next_assignee(conn)
    second = crm_ops.next_assignee(conn)
    assert first == 'sale_a' and second == 'field_a', (first, second)
    print('OK test_sync_and_round_robin')


def test_role_redirects_and_paths():
    assert login_redirect_target('employee', 't1') == 'hrm_ess_portal'
    assert login_redirect_target('staff_field', 't1') == 'hrm_ess_portal'
    assert login_redirect_target('staff', 't1') == 'sale'
    assert is_ess_portal_only_user('employee')
    assert is_ess_portal_only_user('staff_field')
    assert not is_ess_portal_only_user('staff')
    assert not can_ess_customer_visits('employee')
    assert can_ess_customer_visits('staff_field')
    assert can_ess_customer_visits('staff')
    assert ess_portal_path_allowed('/hrm/ess', 'employee')
    assert not ess_portal_path_allowed('/crm/leads', 'employee')
    assert ess_portal_path_allowed('/crm/leads', 'staff_field')
    assert not ess_portal_path_allowed('/sale', 'staff_field')
    print('OK test_role_redirects_and_paths')


if __name__ == '__main__':
    test_list_field_then_staff()
    test_sync_and_round_robin()
    test_role_redirects_and_paths()
    print('All CRM sales staff tests passed.')
