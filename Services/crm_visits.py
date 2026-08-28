# -*- coding: utf-8 -*-
"""CRM field visits — check-in/out gặp khách (ESS)."""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
from typing import Any

from Services.crm import add_activity
from Services.crm_schema import ensure_crm_schema

MIN_CHECKOUT_NOTE_LEN = 3


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _row(r) -> dict:
    if r is None:
        return {}
    if isinstance(r, dict):
        return dict(r)
    if hasattr(r, 'keys'):
        return dict(r)
    return {}


def _rows(cur) -> list[dict]:
    return [_row(r) for r in cur.fetchall()]


def _customer_label(c: dict) -> str:
    company = str(c.get('company_name') or '').strip()
    name = str(c.get('name') or '').strip()
    if company and name and company.lower() != name.lower():
        return company
    return company or name or f"KH #{c.get('id')}"


def _customer_summary(c: dict) -> str:
    lines = [f'Khách: {_customer_label(c)}']
    phone = str(c.get('phone') or '').strip()
    if phone:
        lines.append(f'SĐT: {phone}')
    addr = str(c.get('address') or '').strip()
    if addr:
        lines.append(f'Địa chỉ: {addr}')
    return '\n'.join(lines)


def _verify_customer_owner(conn: sqlite3.Connection, customer_id: int, owner: str) -> dict:
    row = conn.execute(
        'SELECT * FROM customers WHERE id = ?',
        (int(customer_id),),
    ).fetchone()
    cust = _row(row)
    if not cust:
        raise ValueError('Không tìm thấy khách hàng')
    assigned = str(cust.get('crm_owner') or '').strip()
    if assigned and assigned.lower() != str(owner or '').strip().lower():
        raise ValueError('Khách hàng không thuộc phụ trách của bạn')
    return cust


def _open_session(
    conn: sqlite3.Connection,
    *,
    customer_id: int,
    owner: str,
) -> dict | None:
    rows = _rows(conn.execute(
        """
        SELECT v.*
        FROM crm_visit_checkins v
        WHERE v.customer_id = ?
          AND LOWER(TRIM(v.owner)) = LOWER(TRIM(?))
          AND date(v.punched_at) = date('now', 'localtime')
          AND v.check_type = 'in'
          AND NOT EXISTS (
            SELECT 1 FROM crm_visit_checkins o
            WHERE o.visit_session_id = v.visit_session_id
              AND o.check_type = 'out'
          )
        ORDER BY v.punched_at DESC
        LIMIT 1
        """,
        (int(customer_id), owner),
    ))
    return rows[0] if rows else None


def list_visit_customers(
    conn: sqlite3.Connection,
    owner: str,
    *,
    include_all: bool = True,
) -> list[dict]:
    """KH phụ trách cho ESS — ưu tiên hẹn hôm nay."""
    ensure_crm_schema(conn)
    owner = str(owner or '').strip()
    if not owner:
        return []

    sql = """
        SELECT c.id, c.name, c.company_name, c.phone, c.address,
               c.crm_owner, c.crm_next_contact_at
        FROM customers c
        WHERE LOWER(TRIM(COALESCE(c.crm_owner, ''))) = LOWER(TRIM(?))
    """
    params: list[Any] = [owner]
    if not include_all:
        sql += """
          AND (
            date(c.crm_next_contact_at) = date('now', 'localtime')
            OR c.id IN (
              SELECT DISTINCT customer_id FROM crm_visit_checkins
              WHERE LOWER(TRIM(owner)) = LOWER(TRIM(?))
                AND date(punched_at) = date('now', 'localtime')
                AND check_type = 'in'
            )
          )
        """
        params.append(owner)
    sql += """
        ORDER BY
          CASE WHEN date(c.crm_next_contact_at) = date('now', 'localtime') THEN 0 ELSE 1 END,
          (c.crm_next_contact_at IS NULL OR TRIM(c.crm_next_contact_at) = ''),
          c.crm_next_contact_at ASC,
          COALESCE(NULLIF(TRIM(c.company_name), ''), c.name),
          c.id
        LIMIT 120
    """
    items: list[dict] = []
    for c in _rows(conn.execute(sql, params)):
        cid = int(c['id'])
        open_sess = _open_session(conn, customer_id=cid, owner=owner)
        visit_status = 'open' if open_sess else 'idle'
        if not open_sess:
            done = conn.execute(
                """
                SELECT 1 FROM crm_visit_checkins
                WHERE customer_id = ? AND LOWER(TRIM(owner)) = LOWER(TRIM(?))
                  AND date(punched_at) = date('now', 'localtime')
                  AND check_type = 'out'
                LIMIT 1
                """,
                (cid, owner),
            ).fetchone()
            if done:
                visit_status = 'done_today'
        items.append({
            'id': cid,
            'label': _customer_label(c),
            'name': c.get('name'),
            'company_name': c.get('company_name'),
            'phone': c.get('phone'),
            'address': c.get('address'),
            'crm_next_contact_at': c.get('crm_next_contact_at'),
            'visit_status': visit_status,
            'open_session_id': (open_sess or {}).get('visit_session_id'),
            'checked_in_at': (open_sess or {}).get('punched_at'),
        })
    return items


def visit_checkin(
    conn: sqlite3.Connection,
    data: dict,
    *,
    owner: str,
    employee_id: int | None = None,
) -> dict:
    ensure_crm_schema(conn)
    owner = str(owner or '').strip()
    if not owner:
        raise ValueError('Thiếu thông tin NV phụ trách')

    customer_id = int(data.get('customer_id') or 0)
    if not customer_id:
        raise ValueError('Thiếu customer_id')

    check_type = str(data.get('check_type') or 'in').strip().lower()
    if check_type not in ('in', 'out'):
        raise ValueError('check_type phải là in hoặc out')

    cust = _verify_customer_owner(conn, customer_id, owner)
    lat = _f(data.get('lat'))
    lng = _f(data.get('lng'))
    accuracy = _f(data.get('accuracy')) if data.get('accuracy') is not None else None
    note = str(data.get('note') or '').strip() or None
    device_info = str(data.get('device_info') or '')[:200] or None

    now = _now()
    activity_id = None

    if check_type == 'in':
        if _open_session(conn, customer_id=customer_id, owner=owner):
            raise ValueError('Đang check-in tại khách này — hãy check-out trước')
        session_id = secrets.token_urlsafe(12)
    else:
        if not note or len(note) < MIN_CHECKOUT_NOTE_LEN:
            raise ValueError(
                f'Check-out cần ghi rõ nội dung gặp khách (tối thiểu {MIN_CHECKOUT_NOTE_LEN} ký tự)'
            )
        session_id = str(data.get('visit_session_id') or '').strip()
        open_sess = _open_session(conn, customer_id=customer_id, owner=owner)
        if not session_id and open_sess:
            session_id = str(open_sess.get('visit_session_id') or '')
        if not session_id:
            raise ValueError('Chưa check-in — không thể check-out')
        in_row = _row(conn.execute(
            """
            SELECT * FROM crm_visit_checkins
            WHERE visit_session_id = ? AND check_type = 'in'
            ORDER BY id DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone())
        if not in_row:
            raise ValueError('Phiên thăm không hợp lệ')

        content_parts = [
            _customer_summary(cust),
            '',
            f'Check-in: {in_row.get("punched_at") or ""}',
            f'Check-out: {now}',
            '',
            f'Nội dung gặp khách: {note}',
        ]
        next_at = str(data.get('next_contact_at') or '').strip() or None
        activity_id = add_activity(conn, {
            'customer_id': customer_id,
            'activity_type': 'meeting',
            'subject': f'Gặp khách — {_customer_label(cust)}',
            'content': '\n'.join(content_parts),
            'activity_at': now,
            'status': 'done',
            'owner': owner,
            'created_by': owner,
            'next_contact_at': next_at,
        })

    cur = conn.execute(
        """
        INSERT INTO crm_visit_checkins (
            visit_session_id, customer_id, employee_id, owner, check_type,
            lat, lng, accuracy, ward, district, province, formatted_address,
            note, crm_activity_id, device_info, punched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            session_id,
            customer_id,
            int(employee_id) if employee_id else None,
            owner,
            check_type,
            lat,
            lng,
            accuracy,
            None,
            None,
            None,
            None,
            note,
            activity_id,
            device_info,
            now,
        ),
    )
    row = _row(conn.execute(
        'SELECT * FROM crm_visit_checkins WHERE id = ?',
        (cur.lastrowid,),
    ).fetchone())
    row['customer_label'] = _customer_label(cust)
    return row


def _f(v) -> float | None:
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def list_visits(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    customer_id: int | None = None,
    visit_date: str | None = None,
    limit: int = 100,
) -> list[dict]:
    ensure_crm_schema(conn)
    sql = """
        SELECT v.*,
               c.name AS customer_name,
               c.company_name AS customer_company,
               c.phone AS customer_phone
        FROM crm_visit_checkins v
        LEFT JOIN customers c ON c.id = v.customer_id
        WHERE 1=1
    """
    params: list[Any] = []
    if owner:
        sql += ' AND LOWER(TRIM(v.owner)) = LOWER(TRIM(?))'
        params.append(owner.strip())
    if customer_id:
        sql += ' AND v.customer_id = ?'
        params.append(int(customer_id))
    if visit_date:
        sql += " AND date(v.punched_at) = date(?)"
        params.append(visit_date.strip())
    else:
        sql += " AND date(v.punched_at) >= date('now', 'localtime', '-30 day')"
    sql += ' ORDER BY v.punched_at DESC LIMIT ?'
    params.append(int(limit))

    out: list[dict] = []
    for r in _rows(conn.execute(sql, params)):
        r['customer_label'] = _customer_label(r)
        out.append(r)
    return out


def list_visit_sessions_today(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Gom phiên in+out trong ngày cho dashboard."""
    ensure_crm_schema(conn)
    sql = """
        SELECT visit_session_id,
               MAX(customer_id) AS customer_id,
               MAX(owner) AS owner,
               MIN(CASE WHEN check_type = 'in' THEN punched_at END) AS check_in_at,
               MAX(CASE WHEN check_type = 'out' THEN punched_at END) AS check_out_at,
               MAX(CASE WHEN check_type = 'out' THEN note END) AS meeting_note
        FROM crm_visit_checkins
        WHERE date(punched_at) = date('now', 'localtime')
    """
    params: list[Any] = []
    if owner:
        sql += ' AND LOWER(TRIM(owner)) = LOWER(TRIM(?))'
        params.append(owner.strip())
    sql += """
        GROUP BY visit_session_id
        ORDER BY check_in_at DESC
        LIMIT ?
    """
    params.append(int(limit))

    sessions: list[dict] = []
    for s in _rows(conn.execute(sql, params)):
        cid = int(s.get('customer_id') or 0)
        cust = _row(conn.execute(
            'SELECT id, name, company_name, phone FROM customers WHERE id = ?',
            (cid,),
        ).fetchone()) if cid else {}
        s['customer_label'] = _customer_label(cust) if cust else f'KH #{cid}'
        s['customer_phone'] = (cust.get('phone') if cust else '') or ''
        s['meeting_note'] = str(s.get('meeting_note') or '').strip()
        s['status'] = 'done' if s.get('check_out_at') else 'open'
        sessions.append(s)
    return sessions
