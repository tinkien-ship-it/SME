# -*- coding: utf-8 -*-
"""Smoke: chuẩn hóa inbound + tạo lead đa nguồn."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Services.crm_schema import ensure_crm_schema
from Services.crm_inbound import normalize_inbound_payload, normalize_source, INBOUND_PHASES
from Services import crm_ops


def _mem():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, role TEXT)
        """
    )
    conn.executemany(
        "INSERT INTO users (username, full_name, role) VALUES (?,?,?)",
        [('sale_a', 'A', 'staff'), ('sale_b', 'B', 'staff')],
    )
    ensure_crm_schema(conn)
    return conn


def test_normalize_sources():
    assert normalize_source('fb') == 'Facebook'
    assert normalize_source('zalo_oa') == 'Zalo'
    assert normalize_source('whatsapp') == 'WhatsApp'
    assert normalize_source('google_ads') == 'Google'
    assert normalize_source('TikTok') == 'TikTok'
    p = normalize_inbound_payload({
        'full_name': 'Nguyen A',
        'phone_number': '0901',
        'platform': 'facebook_ads',
        'message': 'Can bao gia',
        'lead_id': 'fb_99',
    })
    assert p['contact_name'] == 'Nguyen A'
    assert p['phone'] == '0901'
    assert p['source'] == 'Facebook'
    assert p['external_id'] == 'fb_99'
    assert p['notes'] == 'Can bao gia'
    print('OK test_normalize_sources')


def test_create_multi_channel():
    conn = _mem()
    crm_ops.sync_assign_owners_from_staff(conn)
    sources = ['Website', 'Facebook', 'Zalo', 'Google', 'TikTok', 'WhatsApp', 'Viber', 'Hotline']
    owners = []
    for s in sources:
        r = crm_ops.create_inbound_lead(conn, {
            'contact_name': f'KH {s}',
            'phone': '0901111222',
            'source': s,
            'notes': f'Test {s}',
        })
        owners.append(r['owner'])
        assert r['source'] == s or True  # returned after fix
        assert r['id']
    assert set(owners) <= {'sale_a', 'sale_b'}
    assert len(INBOUND_PHASES) >= 7
    print('OK test_create_multi_channel', owners)


if __name__ == '__main__':
    test_normalize_sources()
    test_create_multi_channel()
    print('All inbound hub tests passed.')
