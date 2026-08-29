# -*- coding: utf-8 -*-
"""Logic nghiệp vụ CRM: leads, pipeline, activities, quotes, customer 360."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from Services.crm_schema import ensure_crm_schema

LEAD_STATUSES = ('new', 'contacting', 'qualified', 'converted', 'lost')
# Phễu chuẩn: Tiếp cận → Tư vấn → Báo giá → Đàm phán → Chốt/Thua
OPP_STAGES = ('approach', 'consulting', 'quoting', 'negotiate', 'won', 'lost')
OPP_STAGE_LABELS = {
    'approach': 'Tiếp cận',
    'consulting': 'Đang tư vấn',
    'quoting': 'Gửi báo giá',
    'negotiate': 'Đàm phán',
    'won': 'Chốt thành công',
    'lost': 'Thất bại',
    # alias cũ (tương thích)
    'lead': 'Tiếp cận',
    'consult': 'Đang tư vấn',
    'quote': 'Gửi báo giá',
}
LEAD_SOURCES = (
    'Website', 'Facebook', 'Zalo', 'Google', 'TikTok',
    'WhatsApp', 'Viber', 'Hotline',
    'Giới thiệu', 'Triển lãm', 'Khác',
)
ACTIVITY_TYPES = ('call', 'zalo', 'email', 'meeting', 'note', 'task', 'birthday', 'survey')
QUOTE_STATUSES = ('draft', 'sent', 'accepted', 'rejected', 'converted')
LIFECYCLES = ('prospect', 'active', 'inactive', 'churned')
SEGMENTS = ('standard', 'vip', 'wholesale', 'retail', 'agency')
MEMBER_TIERS = ('standard', 'silver', 'gold', 'platinum')
TICKET_STATUSES = ('open', 'in_progress', 'waiting', 'resolved', 'closed')
TICKET_PRIORITIES = ('low', 'normal', 'high', 'urgent')
CONTRACT_STATUSES = ('draft', 'sent', 'signed', 'active', 'expired', 'cancelled')
CAMPAIGN_STATUSES = ('draft', 'active', 'paused', 'ended')


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


def normalize_phone_digits(phone: str | None) -> str:
    """Chuẩn hóa SĐT để so khớp (bỏ ký tự; 84xxxxxxxxx → 0xxxxxxxxx)."""
    digits = ''.join(c for c in str(phone or '') if c.isdigit())
    if digits.startswith('84') and len(digits) >= 10:
        digits = '0' + digits[2:]
    if digits.startswith('840') and len(digits) >= 11:
        digits = '0' + digits[3:]
    return digits


def find_matching_customer(
    conn: sqlite3.Connection,
    *,
    phone: str | None = None,
    email: str | None = None,
    name: str | None = None,
) -> int | None:
    """Tìm KH đã có — ưu tiên SĐT, rồi email, rồi tên (khi không có SĐT/email)."""
    ready(conn)
    phone_n = normalize_phone_digits(phone)
    email_n = (email or '').strip().lower()
    name_n = (name or '').strip().lower()

    if phone_n and len(phone_n) >= 8:
        rows = conn.execute(
            """
            SELECT id, phone FROM customers
            WHERE phone IS NOT NULL AND TRIM(phone) != ''
            ORDER BY id DESC LIMIT 500
            """
        ).fetchall()
        for r in rows:
            d = _row(r)
            if normalize_phone_digits(d.get('phone')) == phone_n:
                return int(d['id'])

    if email_n:
        row = conn.execute(
            """
            SELECT id FROM customers
            WHERE lower(trim(COALESCE(email, ''))) = ?
            ORDER BY id DESC LIMIT 1
            """,
            (email_n,),
        ).fetchone()
        if row:
            return int(_row(row)['id'])

    # Chỉ khớp tên khi không có SĐT/email — tránh gộp nhầm hai KH trùng tên
    if name_n and not phone_n and not email_n:
        row = conn.execute(
            """
            SELECT id FROM customers
            WHERE lower(trim(COALESCE(company_name, name, ''))) = ?
               OR lower(trim(COALESCE(name, ''))) = ?
            ORDER BY id DESC LIMIT 1
            """,
            (name_n, name_n),
        ).fetchone()
        if row:
            return int(_row(row)['id'])
    return None


def find_opportunity_for_lead(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    include_closed: bool = False,
) -> dict | None:
    ready(conn)
    sql = """
        SELECT * FROM crm_opportunities
        WHERE lead_id = ?
    """
    if not include_closed:
        sql += " AND COALESCE(stage, '') NOT IN ('won', 'lost')"
    sql += ' ORDER BY id DESC LIMIT 1'
    row = conn.execute(sql, (int(lead_id),)).fetchone()
    return _row(row) if row else None


def sync_customer_from_lead(conn: sqlite3.Connection, customer_id: int, lead: dict) -> None:
    """Cập nhật thông tin KH từ lead đã liên kết — không tạo bản ghi mới."""
    if not customer_id:
        return
    name = (lead.get('company_name') or lead.get('contact_name') or '').strip()
    conn.execute(
        """
        UPDATE customers SET
            name = COALESCE(NULLIF(?, ''), name),
            company_name = COALESCE(NULLIF(?, ''), company_name),
            phone = COALESCE(NULLIF(?, ''), phone),
            email = COALESCE(NULLIF(?, ''), email),
            crm_notes = COALESCE(NULLIF(?, ''), crm_notes),
            crm_next_contact_at = COALESCE(NULLIF(?, ''), crm_next_contact_at),
            crm_updated_at = ?
        WHERE id = ?
        """,
        (
            name or None,
            (lead.get('company_name') or '').strip() or None,
            (lead.get('phone') or '').strip() or None,
            (lead.get('email') or '').strip() or None,
            (lead.get('notes') or '').strip() or None,
            (lead.get('next_contact_at') or '').strip() or None,
            _now(),
            int(customer_id),
        ),
    )


def ready(conn: sqlite3.Connection) -> None:
    from db_utils import _raw_sqlite_conn, is_postgres
    commit = True
    if not is_postgres():
        try:
            raw = _raw_sqlite_conn(conn)
            if getattr(raw, 'in_transaction', False):
                commit = False
        except Exception:
            pass
    ensure_crm_schema(conn, commit=commit)


def next_quote_no(conn: sqlite3.Connection) -> str:
    year = datetime.now().strftime('%Y')
    prefix = f'BG{year}'
    row = conn.execute(
        "SELECT quote_no FROM crm_quotes WHERE quote_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    seq = 1
    if row:
        raw = str(_row(row).get('quote_no') or '')
        try:
            seq = int(raw.replace(prefix, '')) + 1
        except ValueError:
            seq = 1
    return f'{prefix}{seq:04d}'


# ── Leads ──────────────────────────────────────────────────────────────

def list_leads(
    conn: sqlite3.Connection,
    status: str | None = None,
    q: str = '',
    source: str | None = None,
) -> list[dict]:
    ready(conn)
    sql = 'SELECT * FROM crm_leads WHERE 1=1'
    params: list[Any] = []
    if status:
        sql += ' AND status = ?'
        params.append(status)
    if source:
        sql += ' AND source = ?'
        params.append(source.strip())
    if q:
        like = f'%{q.strip()}%'
        sql += (
            ' AND (COALESCE(contact_name,"") LIKE ? OR COALESCE(company_name,"") LIKE ?'
            ' OR COALESCE(phone,"") LIKE ? OR COALESCE(title,"") LIKE ?'
            ' OR COALESCE(owner,"") LIKE ?)'
        )
        params.extend([like, like, like, like, like])
    sql += ' ORDER BY COALESCE(next_contact_at, created_at) ASC, id DESC'
    return _rows(conn.execute(sql, params))


def get_lead(conn: sqlite3.Connection, lead_id: int) -> dict | None:
    ready(conn)
    row = conn.execute('SELECT * FROM crm_leads WHERE id = ?', (lead_id,)).fetchone()
    return _row(row) if row else None


def upsert_lead(conn: sqlite3.Connection, data: dict, lead_id: int | None = None) -> int:
    ready(conn)
    contact = (data.get('contact_name') or '').strip()
    if not contact:
        raise ValueError('Tên liên hệ không được để trống')
    status = (data.get('status') or 'new').strip()
    if status not in LEAD_STATUSES:
        status = 'new'

    existing = get_lead(conn, lead_id) if lead_id else None

    # Giữ owner / customer_id khi form không gửi (tránh mất liên kết → tạo KH trùng lúc convert)
    if 'owner' in data:
        owner = str(data.get('owner') or '').strip() or None
    else:
        owner = (existing or {}).get('owner')

    if 'customer_id' in data and data.get('customer_id') not in (None, '', 0, '0'):
        try:
            customer_id = int(data.get('customer_id'))
        except (TypeError, ValueError):
            customer_id = (existing or {}).get('customer_id')
    else:
        customer_id = (existing or {}).get('customer_id')

    # Đặt status = converted trên form → chạy convert chuẩn (1 KH + 1 cơ hội)
    if status == 'converted' and lead_id:
        # Lưu thông tin trước, rồi convert (không INSERT customer rời)
        conn.execute(
            """
            UPDATE crm_leads SET
                title=?, contact_name=?, company_name=?, phone=?, email=?,
                source=?, owner=?, expected_value=?, notes=?, next_contact_at=?,
                updated_at=?
            WHERE id=?
            """,
            (
                (data.get('title') or '').strip() or contact,
                contact,
                (data.get('company_name') or '').strip() or None,
                (data.get('phone') or '').strip() or None,
                (data.get('email') or '').strip() or None,
                (data.get('source') or '').strip() or None,
                owner,
                _f(data.get('expected_value')),
                (data.get('notes') or '').strip() or None,
                (data.get('next_contact_at') or '').strip() or None,
                _now(),
                int(lead_id),
            ),
        )
        convert_lead(conn, int(lead_id), owner=owner or '')
        return int(lead_id)

    title = (data.get('title') or '').strip() or contact
    company = (data.get('company_name') or '').strip() or None
    phone = (data.get('phone') or '').strip() or None
    email = (data.get('email') or '').strip() or None
    source = (data.get('source') or '').strip() or None
    notes = (data.get('notes') or '').strip() or None
    next_at = (data.get('next_contact_at') or '').strip() or None
    expected = _f(data.get('expected_value'))
    now = _now()

    if lead_id:
        conn.execute(
            """
            UPDATE crm_leads SET
                title=?, contact_name=?, company_name=?, phone=?, email=?,
                source=?, status=?, owner=?, customer_id=?, expected_value=?,
                notes=?, next_contact_at=?, updated_at=?
            WHERE id=?
            """,
            (
                title, contact, company, phone, email, source, status,
                owner, customer_id, expected, notes, next_at, now, int(lead_id),
            ),
        )
        # Đồng bộ sang Danh mục KH nếu đã liên kết — không tạo dòng mới
        if customer_id:
            sync_customer_from_lead(
                conn,
                int(customer_id),
                {
                    'contact_name': contact,
                    'company_name': company,
                    'phone': phone,
                    'email': email,
                    'notes': notes,
                    'next_contact_at': next_at,
                },
            )
        return int(lead_id)

    cur = conn.execute(
        """
        INSERT INTO crm_leads (
            title, contact_name, company_name, phone, email, source, status,
            owner, customer_id, expected_value, notes, next_contact_at,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            title, contact, company, phone, email, source,
            'new' if status == 'converted' else status,
            owner, customer_id, expected, notes, next_at, now, now,
        ),
    )
    new_id = int(cur.lastrowid)
    if status == 'converted':
        convert_lead(conn, new_id, owner=owner or '')
    return new_id


def delete_lead(conn: sqlite3.Connection, lead_id: int) -> None:
    ready(conn)
    conn.execute('DELETE FROM crm_activities WHERE lead_id = ?', (lead_id,))
    conn.execute('DELETE FROM crm_leads WHERE id = ?', (lead_id,))


def convert_lead(conn: sqlite3.Connection, lead_id: int, owner: str = '') -> dict:
    """Chuyển lead → 1 customer + 1 opportunity (tái sử dụng nếu đã có)."""
    ready(conn)
    lead = get_lead(conn, lead_id)
    if not lead:
        raise ValueError('Không tìm thấy lead')

    owner_final = (owner or lead.get('owner') or '').strip() or None
    name = (lead.get('company_name') or lead.get('contact_name') or '').strip()

    customer_id = lead.get('customer_id')
    if customer_id:
        try:
            customer_id = int(customer_id)
        except (TypeError, ValueError):
            customer_id = None

    # Dedup: dùng lại KH theo SĐT / email / tên
    if not customer_id:
        customer_id = find_matching_customer(
            conn,
            phone=lead.get('phone'),
            email=lead.get('email'),
            name=name,
        )

    created_customer = False
    if not customer_id:
        cur = conn.execute(
            """
            INSERT INTO customers (
                name, company_name, phone, email, crm_source, crm_owner,
                crm_lifecycle, crm_segment, crm_notes, crm_next_contact_at,
                crm_created_at, crm_updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                name,
                (lead.get('company_name') or '').strip() or None,
                lead.get('phone'),
                lead.get('email'),
                lead.get('source'),
                owner_final,
                'prospect',
                'standard',
                lead.get('notes'),
                lead.get('next_contact_at'),
                _now(),
                _now(),
            ),
        )
        customer_id = int(cur.lastrowid)
        created_customer = True
    else:
        sync_customer_from_lead(conn, int(customer_id), lead)
        # Gắn owner CRM nếu trống
        conn.execute(
            """
            UPDATE customers SET
                crm_owner = COALESCE(NULLIF(crm_owner, ''), ?),
                crm_source = COALESCE(NULLIF(crm_source, ''), ?),
                crm_lifecycle = COALESCE(NULLIF(crm_lifecycle, ''), 'prospect'),
                crm_updated_at = ?
            WHERE id = ?
            """,
            (owner_final, lead.get('source'), _now(), int(customer_id)),
        )

    # 1 lead → tối đa 1 cơ hội mở
    existing_opp = find_opportunity_for_lead(conn, int(lead_id), include_closed=True)
    opp_payload = {
        'title': lead.get('title') or f"Cơ hội — {lead.get('contact_name')}",
        'customer_id': customer_id,
        'lead_id': lead_id,
        'stage': 'approach',
        'amount': lead.get('expected_value') or 0,
        'owner': owner_final,
        'notes': lead.get('notes'),
    }
    if existing_opp and existing_opp.get('id'):
        # Giữ stage hiện tại nếu đã đi xa hơn approach; chỉ cập nhật thông tin
        stage = existing_opp.get('stage') or 'approach'
        if stage in ('won', 'lost'):
            # Đã đóng — tạo lại chỉ khi convert lại sau khi mở lead
            opp_id = upsert_opportunity(conn, opp_payload)
        else:
            opp_payload['stage'] = stage
            opp_id = upsert_opportunity(conn, opp_payload, opp_id=int(existing_opp['id']))
        already_opp = True
    else:
        opp_id = upsert_opportunity(conn, opp_payload)
        already_opp = False

    conn.execute(
        """
        UPDATE crm_leads SET status='converted', customer_id=?, converted_at=?,
               owner=COALESCE(?, owner), updated_at=?
        WHERE id=?
        """,
        (customer_id, lead.get('converted_at') or _now(), owner_final, _now(), lead_id),
    )
    already = (
        lead.get('status') == 'converted'
        and bool(lead.get('customer_id'))
        and not created_customer
        and already_opp
    )
    return {
        'customer_id': customer_id,
        'opportunity_id': opp_id,
        'already': already,
        'reused_customer': not created_customer,
    }


# ── Opportunities ──────────────────────────────────────────────────────

def list_opportunities(
    conn: sqlite3.Connection,
    stage: str | None = None,
    customer_id: int | None = None,
) -> list[dict]:
    ready(conn)
    sql = """
        SELECT o.*,
               COALESCE(c.company_name, c.name) AS customer_name,
               c.phone AS customer_phone
        FROM crm_opportunities o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE 1=1
    """
    params: list[Any] = []
    if stage:
        sql += ' AND o.stage = ?'
        params.append(stage)
    if customer_id:
        sql += ' AND o.customer_id = ?'
        params.append(customer_id)
    sql += ' ORDER BY CASE o.stage WHEN "won" THEN 2 WHEN "lost" THEN 2 ELSE 0 END, o.id DESC'
    rows = _rows(conn.execute(sql, params))
    for r in rows:
        r['stage_label'] = OPP_STAGE_LABELS.get(r.get('stage') or '', r.get('stage'))
    return rows


def get_opportunity(conn: sqlite3.Connection, opp_id: int) -> dict | None:
    ready(conn)
    row = conn.execute(
        """
        SELECT o.*, COALESCE(c.company_name, c.name) AS customer_name
        FROM crm_opportunities o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.id = ?
        """,
        (opp_id,),
    ).fetchone()
    return _row(row) if row else None


def upsert_opportunity(conn: sqlite3.Connection, data: dict, opp_id: int | None = None) -> int:
    ready(conn)
    title = (data.get('title') or '').strip()
    if not title:
        raise ValueError('Tiêu đề cơ hội không được để trống')
    stage = (data.get('stage') or 'approach').strip()
    if stage in ('lead',):
        stage = 'approach'
    elif stage == 'consult':
        stage = 'consulting'
    elif stage == 'quote':
        stage = 'quoting'
    if stage not in OPP_STAGES:
        stage = 'approach'
    closed_at = data.get('closed_at')
    if stage in ('won', 'lost') and not closed_at:
        closed_at = _now()
    if stage not in ('won', 'lost'):
        closed_at = None
    fields = (
        title,
        data.get('customer_id') or None,
        data.get('lead_id') or None,
        stage,
        _f(data.get('amount')),
        _f(data.get('probability')),
        (data.get('owner') or '').strip() or None,
        (data.get('expected_close_date') or '').strip() or None,
        (data.get('notes') or '').strip() or None,
        (data.get('lost_reason') or '').strip() or None,
        data.get('sale_id') or None,
        data.get('quote_id') or None,
        _now(),
        closed_at,
    )
    if opp_id:
        conn.execute(
            """
            UPDATE crm_opportunities SET
                title=?, customer_id=?, lead_id=?, stage=?, amount=?, probability=?,
                owner=?, expected_close_date=?, notes=?, lost_reason=?, sale_id=?,
                quote_id=?, updated_at=?, closed_at=?
            WHERE id=?
            """,
            fields + (opp_id,),
        )
        return int(opp_id)
    cur = conn.execute(
        """
        INSERT INTO crm_opportunities (
            title, customer_id, lead_id, stage, amount, probability, owner,
            expected_close_date, notes, lost_reason, sale_id, quote_id,
            created_at, updated_at, closed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        fields[:-2] + (_now(), _now(), closed_at),
    )
    return int(cur.lastrowid)


def delete_opportunity(conn: sqlite3.Connection, opp_id: int) -> None:
    ready(conn)
    conn.execute('DELETE FROM crm_activities WHERE opportunity_id = ?', (opp_id,))
    conn.execute('DELETE FROM crm_opportunities WHERE id = ?', (opp_id,))


def pipeline_summary(conn: sqlite3.Connection) -> dict:
    ready(conn)
    by_stage = {s: {'count': 0, 'amount': 0.0, 'items': []} for s in OPP_STAGES}
    for row in list_opportunities(conn):
        st = row.get('stage') or 'lead'
        if st not in by_stage:
            by_stage[st] = {'count': 0, 'amount': 0.0, 'items': []}
        by_stage[st]['count'] += 1
        by_stage[st]['amount'] += _f(row.get('amount'))
        by_stage[st]['items'].append(row)
    return {'stages': OPP_STAGES, 'labels': OPP_STAGE_LABELS, 'by_stage': by_stage}


# ── Activities ─────────────────────────────────────────────────────────

def list_activities(
    conn: sqlite3.Connection,
    customer_id: int | None = None,
    lead_id: int | None = None,
    opportunity_id: int | None = None,
    upcoming_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    ready(conn)
    sql = 'SELECT * FROM crm_activities WHERE 1=1'
    params: list[Any] = []
    if customer_id:
        sql += ' AND customer_id = ?'
        params.append(customer_id)
    if lead_id:
        sql += ' AND lead_id = ?'
        params.append(lead_id)
    if opportunity_id:
        sql += ' AND opportunity_id = ?'
        params.append(opportunity_id)
    if upcoming_only:
        sql += " AND status = 'planned' AND COALESCE(next_contact_at, activity_at) >= date('now','localtime')"
        sql += ' ORDER BY COALESCE(next_contact_at, activity_at) ASC'
    else:
        sql += ' ORDER BY COALESCE(activity_at, created_at) DESC'
    sql += f' LIMIT {int(limit)}'
    return _rows(conn.execute(sql, params))


def add_activity(conn: sqlite3.Connection, data: dict) -> int:
    ready(conn)
    atype = (data.get('activity_type') or 'note').strip()
    if atype not in ACTIVITY_TYPES:
        atype = 'note'
    status = (data.get('status') or 'done').strip()
    if status not in ('done', 'planned'):
        status = 'done'
    next_at = (data.get('next_contact_at') or '').strip() or None
    customer_id = data.get('customer_id') or None
    cur = conn.execute(
        """
        INSERT INTO crm_activities (
            customer_id, lead_id, opportunity_id, activity_type, subject, content,
            activity_at, next_contact_at, status, owner, created_by, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            customer_id,
            data.get('lead_id') or None,
            data.get('opportunity_id') or None,
            atype,
            (data.get('subject') or '').strip() or None,
            (data.get('content') or '').strip() or None,
            (data.get('activity_at') or '').strip() or _now(),
            next_at,
            status,
            (data.get('owner') or '').strip() or None,
            (data.get('created_by') or '').strip() or None,
            _now(),
        ),
    )
    if customer_id and next_at:
        conn.execute(
            """
            UPDATE customers SET crm_next_contact_at = ?, crm_updated_at = ?
            WHERE id = ? AND (
                crm_next_contact_at IS NULL OR crm_next_contact_at = ''
                OR crm_next_contact_at > ?
            )
            """,
            (next_at, _now(), customer_id, next_at),
        )
    return int(cur.lastrowid)


def delete_activity(conn: sqlite3.Connection, activity_id: int) -> None:
    ready(conn)
    conn.execute('DELETE FROM crm_activities WHERE id = ?', (activity_id,))


# ── Quotes ─────────────────────────────────────────────────────────────

def list_quotes(
    conn: sqlite3.Connection,
    status: str | None = None,
    customer_id: int | None = None,
) -> list[dict]:
    ready(conn)
    sql = """
        SELECT q.*, COALESCE(c.company_name, c.name) AS customer_name
        FROM crm_quotes q
        LEFT JOIN customers c ON c.id = q.customer_id
        WHERE 1=1
    """
    params: list[Any] = []
    if status:
        sql += ' AND q.status = ?'
        params.append(status)
    if customer_id:
        sql += ' AND q.customer_id = ?'
        params.append(customer_id)
    sql += ' ORDER BY q.id DESC'
    return _rows(conn.execute(sql, params))


def get_quote(conn: sqlite3.Connection, quote_id: int) -> dict | None:
    ready(conn)
    row = conn.execute(
        """
        SELECT q.*, COALESCE(c.company_name, c.name) AS customer_name,
               c.phone AS customer_phone, c.address AS customer_address,
               c.tax_code AS customer_tax_code, c.email AS customer_email
        FROM crm_quotes q
        LEFT JOIN customers c ON c.id = q.customer_id
        WHERE q.id = ?
        """,
        (quote_id,),
    ).fetchone()
    if not row:
        return None
    q = _row(row)
    q['items'] = _rows(
        conn.execute(
            'SELECT * FROM crm_quote_items WHERE quote_id = ? ORDER BY id',
            (quote_id,),
        )
    )
    return q


def _calc_quote_totals(items: list[dict]) -> tuple[float, float, float]:
    subtotal = 0.0
    tax = 0.0
    for it in items:
        qty = _f(it.get('qty'))
        price = _f(it.get('unit_price'))
        rate = _f(it.get('tax_rate'))
        line = qty * price
        it['line_total'] = round(line * (1 + rate / 100.0), 2)
        subtotal += line
        tax += line * rate / 100.0
    return round(subtotal, 2), round(tax, 2), round(subtotal + tax, 2)


def upsert_quote(conn: sqlite3.Connection, data: dict, quote_id: int | None = None) -> int:
    ready(conn)
    items = data.get('items') or []
    if not isinstance(items, list):
        items = []
    subtotal, tax_amount, total = _calc_quote_totals(items)
    status = (data.get('status') or 'draft').strip()
    if status not in QUOTE_STATUSES:
        status = 'draft'
    quote_no = (data.get('quote_no') or '').strip()
    if quote_id:
        existing = get_quote(conn, quote_id)
        if not existing:
            raise ValueError('Không tìm thấy báo giá')
        if not quote_no:
            quote_no = existing.get('quote_no')
    if not quote_no:
        quote_no = next_quote_no(conn)

    vals = (
        quote_no,
        data.get('customer_id') or None,
        data.get('opportunity_id') or None,
        (data.get('quote_date') or '').strip() or _today(),
        (data.get('valid_until') or '').strip() or None,
        status,
        subtotal,
        tax_amount,
        total,
        (data.get('notes') or '').strip() or None,
        (data.get('owner') or '').strip() or None,
        _now(),
    )
    if quote_id:
        conn.execute(
            """
            UPDATE crm_quotes SET
                quote_no=?, customer_id=?, opportunity_id=?, quote_date=?, valid_until=?,
                status=?, subtotal=?, tax_amount=?, total=?, notes=?, owner=?, updated_at=?
            WHERE id=?
            """,
            vals + (quote_id,),
        )
        conn.execute('DELETE FROM crm_quote_items WHERE quote_id = ?', (quote_id,))
        qid = int(quote_id)
    else:
        cur = conn.execute(
            """
            INSERT INTO crm_quotes (
                quote_no, customer_id, opportunity_id, quote_date, valid_until,
                status, subtotal, tax_amount, total, notes, owner, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            vals[:-1] + (_now(), _now()),
        )
        qid = int(cur.lastrowid)

    for it in items:
        name = (it.get('product_name') or '').strip()
        if not name and not it.get('product_id'):
            continue
        conn.execute(
            """
            INSERT INTO crm_quote_items (
                quote_id, product_id, product_name, unit, qty, unit_price,
                tax_rate, line_total, notes
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                qid,
                it.get('product_id') or None,
                name or None,
                (it.get('unit') or '').strip() or None,
                _f(it.get('qty')) or 1,
                _f(it.get('unit_price')),
                _f(it.get('tax_rate')),
                _f(it.get('line_total')),
                (it.get('notes') or '').strip() or None,
            ),
        )

    opp_id = data.get('opportunity_id')
    if opp_id and status in ('sent', 'accepted', 'draft'):
        conn.execute(
            """
            UPDATE crm_opportunities SET quote_id=?, stage=CASE
                WHEN stage IN ('won','lost') THEN stage ELSE 'quoting' END,
                amount=?, updated_at=?
            WHERE id=?
            """,
            (qid, total, _now(), opp_id),
        )
    return qid


def delete_quote(conn: sqlite3.Connection, quote_id: int) -> None:
    ready(conn)
    q = get_quote(conn, quote_id)
    if q and q.get('status') == 'converted' and q.get('sale_id'):
        raise ValueError('Báo giá đã chuyển đơn hàng — không xóa được')
    conn.execute('DELETE FROM crm_quote_items WHERE quote_id = ?', (quote_id,))
    conn.execute('DELETE FROM crm_quotes WHERE id = ?', (quote_id,))


def convert_quote_to_sale(conn: sqlite3.Connection, quote_id: int) -> dict:
    """Tạo đơn bán (sale) từ báo giá đã chấp nhận / draft."""
    ready(conn)
    q = get_quote(conn, quote_id)
    if not q:
        raise ValueError('Không tìm thấy báo giá')
    if q.get('status') == 'converted' and q.get('sale_id'):
        return {'sale_id': q['sale_id'], 'already': True}
    if not q.get('customer_id'):
        raise ValueError('Báo giá chưa gắn khách hàng')
    if not q.get('items'):
        raise ValueError('Báo giá chưa có dòng hàng')

    cust = _row(
        conn.execute('SELECT * FROM customers WHERE id = ?', (q['customer_id'],)).fetchone()
    )
    if not cust:
        raise ValueError('Khách hàng không tồn tại')

    name = (cust.get('company_name') or cust.get('name') or '').strip()
    total = _f(q.get('total'))
    note = f"Từ báo giá {q.get('quote_no') or quote_id}"

    sale_cols = {r[1] for r in conn.execute('PRAGMA table_info(sale)').fetchall()}
    insert_cols = ['date', 'total_amount', 'customer_name', 'status']
    params: list[Any] = [total, name, 'pending']
    for col, val in (
        ('company_name', cust.get('company_name')),
        ('tax_code', cust.get('tax_code')),
        ('address', cust.get('address')),
        ('email', cust.get('email')),
        ('customer_phone', cust.get('phone')),
        ('customer_id', q['customer_id']),
        ('note', note),
        ('tax_amount', _f(q.get('tax_amount'))),
    ):
        if col in sale_cols:
            insert_cols.append(col)
            params.append(val)

    cols_sql = ', '.join(insert_cols)
    ph = ["datetime('now','localtime')"] + ['?'] * (len(insert_cols) - 1)
    cur = conn.execute(
        f"INSERT INTO sale ({cols_sql}) VALUES ({', '.join(ph)})",
        params,
    )
    sale_id = int(cur.lastrowid)

    item_cols = {r[1] for r in conn.execute('PRAGMA table_info(sale_items)').fetchall()}
    for it in q['items']:
        qty = _f(it.get('qty')) or 1
        price = _f(it.get('unit_price'))
        pid = it.get('product_id')
        pname = (it.get('product_name') or '').strip()
        line_total = _f(it.get('line_total')) or round(qty * price, 2)
        icols = ['sale_id', 'product_id', 'quantity', 'price']
        ivals: list[Any] = [sale_id, pid, qty, price]
        for col, val in (
            ('product_name', pname),
            ('item_name', pname),
            ('unit', it.get('unit')),
            ('line_total', line_total),
            ('UseSaleUnit', 0),
        ):
            if col in item_cols:
                icols.append(col)
                ivals.append(val)
        conn.execute(
            f"INSERT INTO sale_items ({', '.join(icols)}) VALUES ({', '.join('?' for _ in icols)})",
            ivals,
        )

    conn.execute(
        """
        UPDATE crm_quotes SET status='converted', sale_id=?, updated_at=? WHERE id=?
        """,
        (sale_id, _now(), quote_id),
    )
    if q.get('opportunity_id'):
        conn.execute(
            """
            UPDATE crm_opportunities SET stage='won', sale_id=?, closed_at=?, updated_at=?, amount=?
            WHERE id=?
            """,
            (sale_id, _now(), _now(), total, q['opportunity_id']),
        )
    conn.execute(
        """
        UPDATE customers SET crm_lifecycle='active', crm_updated_at=? WHERE id=?
        """,
        (_now(), q['customer_id']),
    )
    return {'sale_id': sale_id, 'already': False}


# ── Customer CRM profile / 360 ─────────────────────────────────────────

def update_customer_crm(conn: sqlite3.Connection, customer_id: int, data: dict) -> None:
    ready(conn)
    lifecycle = (data.get('crm_lifecycle') or '').strip() or None
    if lifecycle and lifecycle not in LIFECYCLES:
        lifecycle = 'active'
    segment = (data.get('crm_segment') or '').strip() or None
    if segment and segment not in SEGMENTS:
        segment = 'standard'
    conn.execute(
        """
        UPDATE customers SET
            crm_source = ?,
            crm_owner = ?,
            crm_segment = ?,
            crm_lifecycle = ?,
            crm_notes = ?,
            crm_next_contact_at = ?,
            crm_tags = ?,
            crm_updated_at = ?
        WHERE id = ?
        """,
        (
            (data.get('crm_source') or '').strip() or None,
            (data.get('crm_owner') or '').strip() or None,
            segment,
            lifecycle,
            data.get('crm_notes') if data.get('crm_notes') is not None else None,
            (data.get('crm_next_contact_at') or '').strip() or None,
            (data.get('crm_tags') or '').strip() or None,
            _now(),
            customer_id,
        ),
    )


def customer_360(conn: sqlite3.Connection, customer_id: int) -> dict:
    ready(conn)
    cust = _row(conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone())
    if not cust:
        raise ValueError('Không tìm thấy khách hàng')

    sales = []
    try:
        sales = _rows(
            conn.execute(
                """
                SELECT id, date, sale_no, total_amount, status, customer_name, payment_method
                FROM sale
                WHERE customer_id = ?
                   OR TRIM(COALESCE(customer_name,'')) = TRIM(COALESCE(?,''))
                   OR TRIM(COALESCE(company_name,'')) = TRIM(COALESCE(?,''))
                ORDER BY id DESC LIMIT 50
                """,
                (customer_id, cust.get('name'), cust.get('company_name') or cust.get('name')),
            )
        )
    except sqlite3.Error:
        sales = _rows(
            conn.execute(
                """
                SELECT id, date, total_amount, status, customer_name
                FROM sale WHERE customer_id = ? ORDER BY id DESC LIMIT 50
                """,
                (customer_id,),
            )
        )

    debt = {'balance': 0.0, 'summary': {}}
    party_name = (cust.get('company_name') or cust.get('name') or '').strip()
    try:
        from Services.sme.debt_ledger import ar_customer_detail

        detail = ar_customer_detail(conn, party_name)
        summary = detail.get('summary') or {}
        debt = {
            'balance': _f(summary.get('remaining') or summary.get('balance') or detail.get('balance')),
            'summary': summary,
        }
    except Exception:
        try:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(remaining, unpaid, debit - credit, 0)), 0) AS bal
                FROM cong_no
                WHERE TRIM(COALESCE(customer_name,'')) IN (?, ?)
                """,
                (
                    (cust.get('name') or '').strip(),
                    party_name,
                ),
            ).fetchone()
            debt = {'balance': _f(_row(row).get('bal')), 'summary': {}}
        except sqlite3.Error:
            pass

    invoices = []
    try:
        invoices = _rows(
            conn.execute(
                """
                SELECT id, invoice_number, invoice_date, total_amount, status, customer_name
                FROM outward_invoices
                WHERE customer_id = ?
                   OR TRIM(COALESCE(customer_name,'')) = TRIM(COALESCE(?,''))
                ORDER BY id DESC LIMIT 30
                """,
                (customer_id, cust.get('company_name') or cust.get('name')),
            )
        )
    except sqlite3.Error:
        pass

    tickets, contracts, surveys = [], [], []
    try:
        from Services import crm_ops
        tickets = crm_ops.list_tickets(conn, customer_id=customer_id)
        contracts = crm_ops.list_contracts(conn, customer_id=customer_id)
        surveys = crm_ops.list_surveys(conn, customer_id=customer_id, limit=20)
    except Exception:
        pass

    return {
        'customer': cust,
        'activities': list_activities(conn, customer_id=customer_id, limit=50),
        'opportunities': list_opportunities(conn, customer_id=customer_id),
        'quotes': list_quotes(conn, customer_id=customer_id),
        'sales': sales,
        'debt': debt,
        'invoices': invoices,
        'leads': _rows(
            conn.execute(
                'SELECT * FROM crm_leads WHERE customer_id = ? ORDER BY id DESC',
                (customer_id,),
            )
        ),
        'tickets': tickets,
        'contracts': contracts,
        'surveys': surveys,
        'visits': customer_visits(conn, customer_id),
    }


def customer_visits(conn: sqlite3.Connection, customer_id: int, *, limit: int = 30) -> list[dict]:
    try:
        from Services import crm_visits
        return crm_visits.list_visits(conn, customer_id=customer_id, limit=limit)
    except Exception:
        return []


def dashboard_stats(conn: sqlite3.Connection) -> dict:
    ready(conn)
    leads_open = conn.execute(
        "SELECT COUNT(*) AS n FROM crm_leads WHERE status NOT IN ('converted','lost')"
    ).fetchone()
    opp_open = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amt FROM crm_opportunities "
        "WHERE stage NOT IN ('won','lost')"
    ).fetchone()
    quotes_open = conn.execute(
        "SELECT COUNT(*) AS n FROM crm_quotes WHERE status IN ('draft','sent','accepted')"
    ).fetchone()
    followups = list_activities(conn, upcoming_only=True, limit=20)
    cust_follow = _rows(
        conn.execute(
            """
            SELECT id, name, company_name, phone, crm_next_contact_at, crm_owner, crm_lifecycle
            FROM customers
            WHERE crm_next_contact_at IS NOT NULL AND TRIM(crm_next_contact_at) != ''
              AND date(crm_next_contact_at) <= date('now','localtime','+7 day')
            ORDER BY crm_next_contact_at ASC LIMIT 20
            """
        )
    )
    recent_acts = list_activities(conn, limit=15)
    pipe = pipeline_summary(conn)
    visit_sessions_today = []
    try:
        from Services import crm_visits
        visit_sessions_today = crm_visits.list_visit_sessions_today(conn, limit=30)
    except Exception:
        pass
    inbound_today = conn.execute(
        """
        SELECT COUNT(*) AS n FROM crm_leads
        WHERE date(created_at) = date('now','localtime')
          AND COALESCE(channel, source, '') != ''
        """
    ).fetchone()
    recent_inbound = _rows(
        conn.execute(
            """
            SELECT id, contact_name, phone, source, channel, owner, created_at, status
            FROM crm_leads
            WHERE date(created_at) = date('now','localtime')
            ORDER BY id DESC LIMIT 12
            """
        )
    )
    return {
        'leads_open': int(_row(leads_open).get('n') or 0),
        'opportunities_open': int(_row(opp_open).get('n') or 0),
        'pipeline_amount': _f(_row(opp_open).get('amt')),
        'quotes_open': int(_row(quotes_open).get('n') or 0),
        'followups': followups,
        'customer_followups': cust_follow,
        'recent_activities': recent_acts,
        'pipeline': pipe,
        'visit_sessions_today': visit_sessions_today,
        'inbound_today': int(_row(inbound_today).get('n') or 0),
        'recent_inbound': recent_inbound,
    }
