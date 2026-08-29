# -*- coding: utf-8 -*-
"""Kiểm tra: sửa lead / convert không tạo trùng KH + cơ hội."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Services.crm_schema import ensure_crm_schema
from Services import crm as crm_svc


def _db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_crm_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, company_name TEXT, phone TEXT, email TEXT,
            crm_source TEXT, crm_owner TEXT, crm_lifecycle TEXT,
            crm_segment TEXT, crm_notes TEXT, crm_next_contact_at TEXT,
            crm_created_at TEXT, crm_updated_at TEXT
        )
        """
    )
    # ensure_crm may have created customers — add missing cols if needed
    return conn


def test_edit_preserves_customer_link():
    conn = _db()
    lid = crm_svc.upsert_lead(conn, {
        'contact_name': 'Nguyen A', 'phone': '0901111222', 'status': 'new', 'owner': 'sale1',
    })
    r1 = crm_svc.convert_lead(conn, lid, owner='sale1')
    cid = r1['customer_id']
    # Sửa như form UI (không gửi customer_id / owner)
    crm_svc.upsert_lead(conn, {
        'contact_name': 'Nguyen A Updated',
        'phone': '0901111222',
        'status': 'contacting',
        'notes': 'goi lai',
    }, lead_id=lid)
    lead = crm_svc.get_lead(conn, lid)
    assert lead['customer_id'] == cid, lead
    assert lead['owner'] == 'sale1', lead
    cust_n = conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
    assert cust_n == 1, cust_n
    print('OK test_edit_preserves_customer_link')


def test_convert_twice_no_dup():
    conn = _db()
    lid = crm_svc.upsert_lead(conn, {
        'contact_name': 'Tran B', 'phone': '0912345678', 'status': 'qualified',
    })
    r1 = crm_svc.convert_lead(conn, lid)
    r2 = crm_svc.convert_lead(conn, lid)
    assert r1['customer_id'] == r2['customer_id']
    assert r1['opportunity_id'] == r2['opportunity_id']
    assert conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM crm_opportunities').fetchone()[0] == 1
    print('OK test_convert_twice_no_dup', r2)


def test_reuse_customer_by_phone():
    conn = _db()
    conn.execute(
        "INSERT INTO customers (name, phone, crm_lifecycle) VALUES ('Cu A', '0909999888', 'active')"
    )
    existing_id = conn.execute('SELECT id FROM customers').fetchone()[0]
    lid = crm_svc.upsert_lead(conn, {
        'contact_name': 'Cu A lead', 'phone': '090-999-9888', 'status': 'new',
    })
    r = crm_svc.convert_lead(conn, lid)
    assert r['customer_id'] == existing_id, r
    assert r.get('reused_customer') is True
    assert conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM crm_opportunities').fetchone()[0] == 1
    print('OK test_reuse_customer_by_phone')


def test_status_converted_via_save():
    conn = _db()
    lid = crm_svc.upsert_lead(conn, {
        'contact_name': 'Le C', 'phone': '0888777666', 'status': 'new',
    })
    crm_svc.upsert_lead(conn, {
        'contact_name': 'Le C', 'phone': '0888777666', 'status': 'converted',
    }, lead_id=lid)
    lead = crm_svc.get_lead(conn, lid)
    assert lead['status'] == 'converted'
    assert lead['customer_id']
    assert conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM crm_opportunities').fetchone()[0] == 1
    # sửa lại không nhân bản
    crm_svc.upsert_lead(conn, {
        'contact_name': 'Le C moi', 'phone': '0888777666', 'status': 'converted',
    }, lead_id=lid)
    assert conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM crm_opportunities').fetchone()[0] == 1
    print('OK test_status_converted_via_save')


if __name__ == '__main__':
    test_edit_preserves_customer_link()
    test_convert_twice_no_dup()
    test_reuse_customer_by_phone()
    test_status_converted_via_save()
    print('All lead/customer dedup tests passed.')
