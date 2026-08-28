# -*- coding: utf-8 -*-
"""Smoke: adapters đa kênh + process_channel_inbound + phase auto + dedup/notify."""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Services.crm_schema import ensure_crm_schema
from Services.crm_inbound_adapters import adapt_channel
from Services.crm_inbound import (
    get_phase_status,
    list_inbound_logs,
    process_channel_inbound,
)
from Services import crm_ops


def _mem():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        'CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, role TEXT)'
    )
    conn.executemany(
        'INSERT INTO users (username, full_name, role) VALUES (?,?,?)',
        [('sale_a', 'A', 'staff'), ('sale_b', 'B', 'staff')],
    )
    ensure_crm_schema(conn)
    crm_ops.sync_assign_owners_from_staff(conn)
    return conn


def test_adapt_facebook_field_data():
    a = adapt_channel('facebook', {
        'field_data': [
            {'name': 'full_name', 'values': ['Nguyen FB']},
            {'name': 'phone_number', 'values': ['0911111111']},
        ],
        'leadgen_id': 'lg_1',
    })
    assert a['contact_name'] == 'Nguyen FB'
    assert a['phone'] == '0911111111'
    assert a['source'] == 'Facebook'
    assert a['external_id'] == 'lg_1'
    print('OK test_adapt_facebook_field_data')


def test_adapt_tiktok_batch():
    a = adapt_channel('tiktok', {
        'data': [{'leads': [{'lead_id': 'tt9', 'name': 'Tik User', 'phone_number': '092222'}]}],
    })
    assert a['contact_name'] == 'Tik User'
    assert a['phone'] == '092222'
    assert a['external_id'] == 'tt9'
    print('OK test_adapt_tiktok_batch')


def test_adapt_zalo_whatsapp_viber_google():
    z = adapt_channel('zalo', {
        'sender': {'id': 'z1', 'name': 'Zalo Nam'},
        'message': {'text': 'Can bao gia'},
        'phone': '093333',
    })
    assert z['source'] == 'Zalo' and z['phone'] == '093333'
    w = adapt_channel('whatsapp', {
        'entry': [{'changes': [{'value': {
            'contacts': [{'wa_id': '84901234567', 'profile': {'name': 'WA User'}}],
            'messages': [{'id': 'wamid', 'from': '84901234567', 'text': {'body': 'Hi'}}],
        }}]}],
    })
    assert w['source'] == 'WhatsApp' and '8490' in w['phone']
    v = adapt_channel('viber', {'sender': {'name': 'Vi'}, 'message': {'text': 'hello'}, 'phone': '094'})
    assert v['source'] == 'Viber'
    g = adapt_channel('google', {
        'user_column_data': [
            {'column_id': 'FULL_NAME', 'string_value': 'Google Lead'},
            {'column_id': 'PHONE_NUMBER', 'string_value': '095555'},
        ],
    })
    assert g['source'] == 'Google'
    assert g['contact_name'] == 'Google Lead'
    h = adapt_channel('hotline', {'contact_name': 'Ref', 'phone': '096', 'source': 'Giới thiệu'})
    assert h['source'] == 'Giới thiệu'
    print('OK test_adapt_zalo_whatsapp_viber_google')


def test_process_all_channels_and_phases():
    conn = _mem()
    channels = [
        ('website', {'contact_name': 'W', 'phone': '0901', 'external_id': 'e_w'}),
        ('facebook', {'contact_name': 'F', 'phone': '0902', 'leadgen_id': 'f1'}),
        ('zalo', {'contact_name': 'Z', 'phone': '0903', 'external_id': 'e_z'}),
        ('google', {'contact_name': 'G', 'phone': '0904', 'external_id': 'e_g'}),
        ('tiktok', {'contact_name': 'T', 'phone': '0905', 'external_id': 'e_t'}),
        ('whatsapp', {'contact_name': 'WA', 'phone': '0906', 'external_id': 'e_wa'}),
        ('viber', {'contact_name': 'V', 'phone': '0907', 'external_id': 'e_v'}),
        ('hotline', {'contact_name': 'H', 'phone': '0908', 'external_id': 'e_h'}),
    ]
    for ch, payload in channels:
        r = process_channel_inbound(conn, ch, payload)
        assert r['id'] and r['channel'] == ch
        assert not r.get('deduped')
    phases = get_phase_status(conn)
    for pid in ('website', 'facebook', 'zalo', 'google', 'tiktok', 'whatsapp', 'viber', 'other'):
        assert phases.get(pid), pid
    logs = list_inbound_logs(conn, limit=20)
    assert len(logs) >= 8
    assert all(x['status'] in ('ok', 'dedup') for x in logs)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM crm_notifications WHERE notif_type='inbound_lead'"
    ).fetchone()['n']
    assert n >= 8, n
    acts = conn.execute(
        "SELECT COUNT(*) AS n FROM crm_activities WHERE created_by='inbound'"
    ).fetchone()['n']
    assert acts >= 8, acts
    print('OK test_process_all_channels_and_phases', phases)


def test_dedup_external_id():
    conn = _mem()
    p = {'contact_name': 'Dup', 'phone': '0999', 'external_id': 'same_x'}
    a = process_channel_inbound(conn, 'facebook', {**p, 'leadgen_id': 'same_x'})
    b = process_channel_inbound(conn, 'facebook', {**p, 'leadgen_id': 'same_x', 'phone': '0888'})
    assert a['id'] == b['id']
    assert b.get('deduped') is True
    cnt = conn.execute('SELECT COUNT(*) AS n FROM crm_leads').fetchone()['n']
    assert cnt == 1
    logs = list_inbound_logs(conn, limit=5)
    assert any(x['status'] == 'dedup' for x in logs)
    print('OK test_dedup_external_id')


if __name__ == '__main__':
    test_adapt_facebook_field_data()
    test_adapt_tiktok_batch()
    test_adapt_zalo_whatsapp_viber_google()
    test_process_all_channels_and_phases()
    test_dedup_external_id()
    print('All channel inbound tests passed.')
