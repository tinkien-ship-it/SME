# -*- coding: utf-8 -*-
"""CRM ops: campaigns, targets, contracts, tickets, loyalty, inbound, reminders."""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
from typing import Any

from Services.crm import (
    CAMPAIGN_STATUSES,
    CONTRACT_STATUSES,
    MEMBER_TIERS,
    TICKET_PRIORITIES,
    TICKET_STATUSES,
    ready,
    upsert_lead,
)
from Services.crm_schema import ensure_crm_schema


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


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


def get_setting(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    ready(conn)
    row = conn.execute('SELECT value FROM crm_settings WHERE key = ?', (key,)).fetchone()
    return str(_row(row).get('value') or default)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    ready(conn)
    conn.execute(
        """
        INSERT INTO crm_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def ensure_inbound_token(conn: sqlite3.Connection) -> str:
    tok = get_setting(conn, 'inbound_token')
    if not tok:
        tok = secrets.token_urlsafe(24)
        set_setting(conn, 'inbound_token', tok)
    return tok


# ── Assignment ─────────────────────────────────────────────────────────

def get_assign_owners(conn: sqlite3.Connection) -> list[str]:
    ready(conn)
    raw = get_setting(conn, 'assign_owners')
    if raw.strip():
        return [x.strip() for x in raw.split(',') if x.strip()]
    row = conn.execute('SELECT owners_csv FROM crm_assign_state WHERE id = 1').fetchone()
    csv = (_row(row).get('owners_csv') or '').strip()
    return [x.strip() for x in csv.split(',') if x.strip()]


def set_assign_owners(conn: sqlite3.Connection, owners: list[str]) -> None:
    owners = [o.strip() for o in owners if o and str(o).strip()]
    csv = ','.join(owners)
    set_setting(conn, 'assign_owners', csv)
    conn.execute(
        """
        UPDATE crm_assign_state SET owners_csv = ?, updated_at = ? WHERE id = 1
        """,
        (csv, _now()),
    )


def next_assignee(conn: sqlite3.Connection) -> str | None:
    owners = get_assign_owners(conn)
    if not owners:
        return None
    row = conn.execute('SELECT last_owner_index FROM crm_assign_state WHERE id = 1').fetchone()
    idx = int(_row(row).get('last_owner_index') or -1)
    idx = (idx + 1) % len(owners)
    conn.execute(
        'UPDATE crm_assign_state SET last_owner_index = ?, updated_at = ? WHERE id = 1',
        (idx, _now()),
    )
    return owners[idx]


def assign_lead(conn: sqlite3.Connection, lead_id: int, owner: str | None = None) -> str | None:
    ready(conn)
    owner = (owner or '').strip() or next_assignee(conn)
    if not owner:
        return None
    conn.execute(
        """
        UPDATE crm_leads SET owner = ?, assigned_at = ?, updated_at = ? WHERE id = ?
        """,
        (owner, _now(), _now(), lead_id),
    )
    return owner


# ── Inbound lead ───────────────────────────────────────────────────────

def create_inbound_lead(conn: sqlite3.Connection, data: dict, auto_assign: bool = True) -> dict:
    ready(conn)
    payload = {
        'title': data.get('title') or data.get('contact_name') or 'Lead inbound',
        'contact_name': (data.get('contact_name') or data.get('name') or '').strip() or 'Khách mới',
        'company_name': data.get('company_name') or data.get('company'),
        'phone': data.get('phone'),
        'email': data.get('email'),
        'source': data.get('source') or data.get('utm_source') or data.get('channel') or 'Website',
        'status': 'new',
        'expected_value': data.get('expected_value') or 0,
        'notes': data.get('notes') or data.get('message'),
        'owner': data.get('owner'),
    }
    lid = upsert_lead(conn, payload)
    # extra fields
    conn.execute(
        """
        UPDATE crm_leads SET
            campaign_id = COALESCE(?, campaign_id),
            utm_source = COALESCE(?, utm_source),
            utm_medium = COALESCE(?, utm_medium),
            utm_campaign = COALESCE(?, utm_campaign),
            channel = COALESCE(?, channel),
            external_id = COALESCE(?, external_id),
            score = COALESCE(?, score)
        WHERE id = ?
        """,
        (
            data.get('campaign_id'),
            data.get('utm_source'),
            data.get('utm_medium'),
            data.get('utm_campaign'),
            data.get('channel') or data.get('source'),
            data.get('external_id'),
            data.get('score'),
            lid,
        ),
    )
    owner = payload.get('owner')
    if auto_assign and not owner:
        owner = assign_lead(conn, lid)
    elif owner:
        assign_lead(conn, lid, owner)
    return {'id': lid, 'owner': owner}


# ── Campaigns ──────────────────────────────────────────────────────────

def list_campaigns(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    ready(conn)
    if status:
        return _rows(conn.execute(
            'SELECT * FROM crm_campaigns WHERE status = ? ORDER BY id DESC', (status,)
        ))
    return _rows(conn.execute('SELECT * FROM crm_campaigns ORDER BY id DESC'))


def upsert_campaign(conn: sqlite3.Connection, data: dict, cid: int | None = None) -> int:
    ready(conn)
    name = (data.get('name') or '').strip()
    if not name:
        raise ValueError('Tên chiến dịch không được trống')
    status = (data.get('status') or 'active').strip()
    if status not in CAMPAIGN_STATUSES:
        status = 'active'
    vals = (
        name,
        (data.get('channel') or '').strip() or None,
        status,
        (data.get('start_date') or '').strip() or None,
        (data.get('end_date') or '').strip() or None,
        _f(data.get('budget')),
        _f(data.get('spend')),
        (data.get('notes') or '').strip() or None,
        _now(),
    )
    if cid:
        conn.execute(
            """
            UPDATE crm_campaigns SET
                name=?, channel=?, status=?, start_date=?, end_date=?,
                budget=?, spend=?, notes=?, updated_at=?
            WHERE id=?
            """,
            vals + (cid,),
        )
        return int(cid)
    cur = conn.execute(
        """
        INSERT INTO crm_campaigns (
            name, channel, status, start_date, end_date, budget, spend, notes,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        vals[:-1] + (_now(), _now()),
    )
    return int(cur.lastrowid)


def delete_campaign(conn: sqlite3.Connection, cid: int) -> None:
    ready(conn)
    conn.execute('DELETE FROM crm_campaigns WHERE id = ?', (cid,))


# ── Targets ────────────────────────────────────────────────────────────

def list_targets(conn: sqlite3.Connection, period_key: str | None = None) -> list[dict]:
    ready(conn)
    if period_key:
        return _rows(conn.execute(
            'SELECT * FROM crm_targets WHERE period_key = ? ORDER BY owner', (period_key,)
        ))
    return _rows(conn.execute('SELECT * FROM crm_targets ORDER BY period_key DESC, owner'))


def upsert_target(conn: sqlite3.Connection, data: dict) -> int:
    ready(conn)
    period_type = (data.get('period_type') or 'month').strip()
    period_key = (data.get('period_key') or '').strip()
    if not period_key:
        raise ValueError('Thiếu period_key (VD: 2026-08 hoặc 2026-Q3)')
    owner = (data.get('owner') or '').strip()
    conn.execute(
        """
        INSERT INTO crm_targets (period_type, period_key, owner, target_amount, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(period_type, period_key, owner) DO UPDATE SET
            target_amount = excluded.target_amount,
            notes = excluded.notes
        """,
        (
            period_type,
            period_key,
            owner,
            _f(data.get('target_amount')),
            (data.get('notes') or '').strip() or None,
            _now(),
        ),
    )
    row = conn.execute(
        """
        SELECT id FROM crm_targets
        WHERE period_type=? AND period_key=? AND owner=?
        """,
        (period_type, period_key, owner),
    ).fetchone()
    return int(_row(row).get('id') or 0)


def delete_target(conn: sqlite3.Connection, tid: int) -> None:
    ready(conn)
    conn.execute('DELETE FROM crm_targets WHERE id = ?', (tid,))


# ── Contracts ──────────────────────────────────────────────────────────

def _next_contract_no(conn: sqlite3.Connection) -> str:
    y = datetime.now().strftime('%Y')
    prefix = f'HD{y}'
    row = conn.execute(
        "SELECT contract_no FROM crm_contracts WHERE contract_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    seq = 1
    if row:
        raw = str(_row(row).get('contract_no') or '')
        try:
            seq = int(raw.replace(prefix, '')) + 1
        except ValueError:
            seq = 1
    return f'{prefix}{seq:04d}'


def list_contracts(conn: sqlite3.Connection, customer_id: int | None = None) -> list[dict]:
    ready(conn)
    sql = """
        SELECT ct.*, COALESCE(c.company_name, c.name) AS customer_name
        FROM crm_contracts ct
        LEFT JOIN customers c ON c.id = ct.customer_id
        WHERE 1=1
    """
    params: list[Any] = []
    if customer_id:
        sql += ' AND ct.customer_id = ?'
        params.append(customer_id)
    sql += ' ORDER BY ct.id DESC'
    return _rows(conn.execute(sql, params))


def upsert_contract(conn: sqlite3.Connection, data: dict, cid: int | None = None) -> int:
    ready(conn)
    status = (data.get('status') or 'draft').strip()
    if status not in CONTRACT_STATUSES:
        status = 'draft'
    contract_no = (data.get('contract_no') or '').strip()
    if cid:
        existing = conn.execute('SELECT contract_no FROM crm_contracts WHERE id=?', (cid,)).fetchone()
        if not contract_no:
            contract_no = _row(existing).get('contract_no')
    if not contract_no:
        contract_no = _next_contract_no(conn)
    vals = (
        contract_no,
        data.get('customer_id') or None,
        data.get('quote_id') or None,
        data.get('opportunity_id') or None,
        data.get('sale_id') or None,
        (data.get('title') or '').strip() or contract_no,
        (data.get('signed_date') or '').strip() or None,
        (data.get('start_date') or '').strip() or None,
        (data.get('end_date') or '').strip() or None,
        _f(data.get('amount')),
        status,
        (data.get('file_path') or '').strip() or None,
        (data.get('notes') or '').strip() or None,
        (data.get('owner') or '').strip() or None,
        _now(),
    )
    if cid:
        conn.execute(
            """
            UPDATE crm_contracts SET
                contract_no=?, customer_id=?, quote_id=?, opportunity_id=?, sale_id=?,
                title=?, signed_date=?, start_date=?, end_date=?, amount=?, status=?,
                file_path=?, notes=?, owner=?, updated_at=?
            WHERE id=?
            """,
            vals + (cid,),
        )
        return int(cid)
    cur = conn.execute(
        """
        INSERT INTO crm_contracts (
            contract_no, customer_id, quote_id, opportunity_id, sale_id, title,
            signed_date, start_date, end_date, amount, status, file_path, notes,
            owner, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        vals[:-1] + (_now(), _now()),
    )
    return int(cur.lastrowid)


def delete_contract(conn: sqlite3.Connection, cid: int) -> None:
    ready(conn)
    conn.execute('DELETE FROM crm_contracts WHERE id = ?', (cid,))


# ── Tickets ────────────────────────────────────────────────────────────

def _next_ticket_no(conn: sqlite3.Connection) -> str:
    y = datetime.now().strftime('%Y')
    prefix = f'TK{y}'
    row = conn.execute(
        "SELECT ticket_no FROM crm_tickets WHERE ticket_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    seq = 1
    if row:
        raw = str(_row(row).get('ticket_no') or '')
        try:
            seq = int(raw.replace(prefix, '')) + 1
        except ValueError:
            seq = 1
    return f'{prefix}{seq:04d}'


def list_tickets(conn: sqlite3.Connection, status: str | None = None, customer_id: int | None = None) -> list[dict]:
    ready(conn)
    sql = """
        SELECT t.*, COALESCE(c.company_name, c.name) AS customer_name
        FROM crm_tickets t
        LEFT JOIN customers c ON c.id = t.customer_id
        WHERE 1=1
    """
    params: list[Any] = []
    if status:
        sql += ' AND t.status = ?'
        params.append(status)
    if customer_id:
        sql += ' AND t.customer_id = ?'
        params.append(customer_id)
    sql += ' ORDER BY CASE t.status WHEN "closed" THEN 2 WHEN "resolved" THEN 1 ELSE 0 END, t.id DESC'
    return _rows(conn.execute(sql, params))


def get_ticket(conn: sqlite3.Connection, tid: int) -> dict | None:
    ready(conn)
    row = conn.execute(
        """
        SELECT t.*, COALESCE(c.company_name, c.name) AS customer_name
        FROM crm_tickets t LEFT JOIN customers c ON c.id = t.customer_id
        WHERE t.id = ?
        """,
        (tid,),
    ).fetchone()
    if not row:
        return None
    t = _row(row)
    t['events'] = _rows(conn.execute(
        'SELECT * FROM crm_ticket_events WHERE ticket_id = ? ORDER BY id', (tid,)
    ))
    return t


def upsert_ticket(conn: sqlite3.Connection, data: dict, tid: int | None = None) -> int:
    ready(conn)
    subject = (data.get('subject') or '').strip()
    if not subject:
        raise ValueError('Tiêu đề ticket không được trống')
    status = (data.get('status') or 'open').strip()
    if status not in TICKET_STATUSES:
        status = 'open'
    priority = (data.get('priority') or 'normal').strip()
    if priority not in TICKET_PRIORITIES:
        priority = 'normal'
    now = _now()
    if tid:
        old = get_ticket(conn, tid) or {}
        resolved_at = old.get('resolved_at')
        closed_at = old.get('closed_at')
        first_response_at = old.get('first_response_at')
        if status == 'in_progress' and not first_response_at:
            first_response_at = now
        if status == 'resolved' and not resolved_at:
            resolved_at = now
        if status == 'closed' and not closed_at:
            closed_at = now
            if not resolved_at:
                resolved_at = now
        conn.execute(
            """
            UPDATE crm_tickets SET
                customer_id=?, subject=?, description=?, category=?, priority=?,
                status=?, assignee=?, first_response_at=?, resolved_at=?, closed_at=?,
                csat_score=?, notes=?, updated_at=?
            WHERE id=?
            """,
            (
                data.get('customer_id') or old.get('customer_id'),
                subject,
                (data.get('description') or '').strip() or None,
                (data.get('category') or 'general').strip(),
                priority,
                status,
                (data.get('assignee') or '').strip() or None,
                first_response_at,
                resolved_at,
                closed_at,
                data.get('csat_score'),
                (data.get('notes') or '').strip() or None,
                now,
                tid,
            ),
        )
        return int(tid)
    cur = conn.execute(
        """
        INSERT INTO crm_tickets (
            ticket_no, customer_id, subject, description, category, priority,
            status, assignee, opened_at, notes, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _next_ticket_no(conn),
            data.get('customer_id') or None,
            subject,
            (data.get('description') or '').strip() or None,
            (data.get('category') or 'general').strip(),
            priority,
            status,
            (data.get('assignee') or '').strip() or None,
            now,
            (data.get('notes') or '').strip() or None,
            (data.get('created_by') or '').strip() or None,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def add_ticket_event(conn: sqlite3.Connection, ticket_id: int, content: str, created_by: str = '', event_type: str = 'note') -> int:
    ready(conn)
    cur = conn.execute(
        """
        INSERT INTO crm_ticket_events (ticket_id, event_type, content, created_by, created_at)
        VALUES (?,?,?,?,?)
        """,
        (ticket_id, event_type, content, created_by or None, _now()),
    )
    t = get_ticket(conn, ticket_id) or {}
    if t.get('status') == 'open':
        conn.execute(
            """
            UPDATE crm_tickets SET status='in_progress',
                first_response_at=COALESCE(first_response_at, ?), updated_at=?
            WHERE id=?
            """,
            (_now(), _now(), ticket_id),
        )
    return int(cur.lastrowid)


def delete_ticket(conn: sqlite3.Connection, tid: int) -> None:
    ready(conn)
    conn.execute('DELETE FROM crm_ticket_events WHERE ticket_id = ?', (tid,))
    conn.execute('DELETE FROM crm_tickets WHERE id = ?', (tid,))


# ── Loyalty / surveys ──────────────────────────────────────────────────

def update_loyalty(conn: sqlite3.Connection, customer_id: int, data: dict) -> None:
    ready(conn)
    tier = (data.get('crm_member_tier') or '').strip() or None
    if tier and tier not in MEMBER_TIERS:
        tier = 'standard'
    conn.execute(
        """
        UPDATE customers SET
            crm_birthday = COALESCE(?, crm_birthday),
            crm_member_code = COALESCE(?, crm_member_code),
            crm_member_tier = COALESCE(?, crm_member_tier),
            crm_loyalty_points = COALESCE(?, crm_loyalty_points),
            crm_updated_at = ?
        WHERE id = ?
        """,
        (
            (data.get('crm_birthday') or '').strip() or None,
            (data.get('crm_member_code') or '').strip() or None,
            tier,
            data.get('crm_loyalty_points') if data.get('crm_loyalty_points') is not None else None,
            _now(),
            customer_id,
        ),
    )


def add_survey(conn: sqlite3.Connection, data: dict) -> int:
    ready(conn)
    stype = (data.get('survey_type') or 'csat').strip()
    if stype not in ('csat', 'nps'):
        stype = 'csat'
    score = _f(data.get('score'))
    cur = conn.execute(
        """
        INSERT INTO crm_surveys (
            customer_id, survey_type, score, comment, channel,
            related_ticket_id, related_sale_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            data.get('customer_id') or None,
            stype,
            score,
            (data.get('comment') or '').strip() or None,
            (data.get('channel') or '').strip() or None,
            data.get('related_ticket_id') or None,
            data.get('related_sale_id') or None,
            _now(),
        ),
    )
    cid = data.get('customer_id')
    if cid:
        if stype == 'csat':
            conn.execute(
                'UPDATE customers SET crm_csat_score=?, crm_last_survey_at=?, crm_updated_at=? WHERE id=?',
                (score, _now(), _now(), cid),
            )
        else:
            conn.execute(
                'UPDATE customers SET crm_nps_score=?, crm_last_survey_at=?, crm_updated_at=? WHERE id=?',
                (score, _now(), _now(), cid),
            )
    return int(cur.lastrowid)


def list_surveys(conn: sqlite3.Connection, customer_id: int | None = None, limit: int = 50) -> list[dict]:
    ready(conn)
    if customer_id:
        return _rows(conn.execute(
            'SELECT * FROM crm_surveys WHERE customer_id=? ORDER BY id DESC LIMIT ?',
            (customer_id, int(limit)),
        ))
    return _rows(conn.execute(
        'SELECT * FROM crm_surveys ORDER BY id DESC LIMIT ?', (int(limit),)
    ))


# ── Notifications / reminders ──────────────────────────────────────────

def add_notification(conn: sqlite3.Connection, data: dict) -> int:
    ready(conn)
    cur = conn.execute(
        """
        INSERT INTO crm_notifications (
            notif_type, title, body, owner, customer_id, lead_id,
            opportunity_id, ticket_id, due_at, is_read, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,0,?)
        """,
        (
            data.get('notif_type') or 'reminder',
            (data.get('title') or '').strip() or 'Nhắc CRM',
            (data.get('body') or '').strip() or None,
            (data.get('owner') or '').strip() or None,
            data.get('customer_id') or None,
            data.get('lead_id') or None,
            data.get('opportunity_id') or None,
            data.get('ticket_id') or None,
            (data.get('due_at') or '').strip() or None,
            _now(),
        ),
    )
    return int(cur.lastrowid)


def list_notifications(conn: sqlite3.Connection, owner: str | None = None, unread_only: bool = False, limit: int = 50) -> list[dict]:
    ready(conn)
    sql = 'SELECT * FROM crm_notifications WHERE 1=1'
    params: list[Any] = []
    if owner:
        sql += ' AND (owner = ? OR owner IS NULL OR owner = "")'
        params.append(owner)
    if unread_only:
        sql += ' AND is_read = 0'
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(int(limit))
    return _rows(conn.execute(sql, params))


def mark_notification_read(conn: sqlite3.Connection, nid: int) -> None:
    ready(conn)
    conn.execute('UPDATE crm_notifications SET is_read = 1 WHERE id = ?', (nid,))


def scan_reminders(conn: sqlite3.Connection) -> dict:
    """Tạo notification cho hẹn liên hệ đến hạn + sinh nhật hôm nay."""
    ensure_crm_schema(conn, commit=False)
    created = 0
    today = _today()
    # customer follow-ups due today/overdue
    rows = _rows(conn.execute(
        """
        SELECT id, name, company_name, crm_owner, crm_next_contact_at
        FROM customers
        WHERE crm_next_contact_at IS NOT NULL AND TRIM(crm_next_contact_at) != ''
          AND date(crm_next_contact_at) <= date('now', 'localtime')
        """
    ))
    for r in rows:
        exists = conn.execute(
            """
            SELECT id FROM crm_notifications
            WHERE notif_type='reminder' AND customer_id=?
              AND date(created_at)=date('now','localtime')
            LIMIT 1
            """,
            (r['id'],),
        ).fetchone()
        if exists:
            continue
        add_notification(conn, {
            'notif_type': 'reminder',
            'title': f"Nhắc liên hệ: {r.get('company_name') or r.get('name')}",
            'body': f"Hẹn lúc {r.get('crm_next_contact_at')}",
            'owner': r.get('crm_owner'),
            'customer_id': r['id'],
            'due_at': r.get('crm_next_contact_at'),
        })
        created += 1

    # birthdays today (mm-dd)
    bdays = _rows(conn.execute(
        """
        SELECT id, name, company_name, crm_owner, crm_birthday
        FROM customers
        WHERE crm_birthday IS NOT NULL AND TRIM(crm_birthday) != ''
          AND substr(crm_birthday, 6, 5) = substr(date('now','localtime'), 6, 5)
        """
    ))
    for r in bdays:
        exists = conn.execute(
            """
            SELECT id FROM crm_notifications
            WHERE notif_type='birthday' AND customer_id=?
              AND date(created_at)=date('now','localtime')
            LIMIT 1
            """,
            (r['id'],),
        ).fetchone()
        if exists:
            continue
        name = r.get('company_name') or r.get('name')
        add_notification(conn, {
            'notif_type': 'birthday',
            'title': f'Sinh nhật KH: {name}',
            'body': 'Gửi lời chúc / ưu đãi trong ngày.',
            'owner': r.get('crm_owner'),
            'customer_id': r['id'],
            'due_at': today,
        })
        # planned activity
        from Services.crm import add_activity
        add_activity(conn, {
            'customer_id': r['id'],
            'activity_type': 'birthday',
            'subject': f'Chúc mừng sinh nhật {name}',
            'status': 'planned',
            'owner': r.get('crm_owner'),
            'activity_at': _now(),
        })
        created += 1

    # open tickets aging > 2 days without resolve
    aged = _rows(conn.execute(
        """
        SELECT id, ticket_no, subject, assignee, customer_id
        FROM crm_tickets
        WHERE status IN ('open', 'in_progress', 'waiting')
          AND julianday('now','localtime') - julianday(COALESCE(opened_at, created_at)) >= 2
        """
    ))
    for r in aged:
        exists = conn.execute(
            """
            SELECT id FROM crm_notifications
            WHERE notif_type='ticket_sla' AND ticket_id=?
              AND date(created_at)=date('now','localtime')
            LIMIT 1
            """,
            (r['id'],),
        ).fetchone()
        if exists:
            continue
        add_notification(conn, {
            'notif_type': 'ticket_sla',
            'title': f'Ticket quá hạn: {r.get("ticket_no")}',
            'body': r.get('subject'),
            'owner': r.get('assignee'),
            'customer_id': r.get('customer_id'),
            'ticket_id': r['id'],
            'due_at': today,
        })
        created += 1

    return {'created': created, 'at': _now()}


def run_reminders_all_tenants() -> dict:
    """Quét mọi tenant DB — gọi từ scheduler."""
    from db_utils import BASE_DIR, open_sqlite
    import os

    created_total = 0
    scanned = 0
    tenants_dir = os.path.join(BASE_DIR, 'tenants')
    paths = []
    if os.path.isdir(tenants_dir):
        for fn in os.listdir(tenants_dir):
            if fn.endswith('.db') and fn.lower() != 'registry.db':
                paths.append(os.path.join(tenants_dir, fn))
    for path in paths:
        try:
            conn = open_sqlite(path, timeout=2)
            try:
                r = scan_reminders(conn)
                conn.commit()
                created_total += int(r.get('created') or 0)
                scanned += 1
            finally:
                conn.close()
        except Exception:
            continue
    return {'scanned': scanned, 'created': created_total}
