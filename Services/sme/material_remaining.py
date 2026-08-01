"""Bảng kê chi tiết vật tư còn lại cuối kỳ — mẫu 04-VT (TT99)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY_Q = Decimal('0.01')
FORM_CODE = '04-VT'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_material_remaining_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_material_remaining (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '04-VT',
            doc_no TEXT NOT NULL UNIQUE,
            as_of_date TEXT NOT NULL,
            department TEXT,
            notes TEXT,
            total_amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_material_remaining_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL DEFAULT 1,
            product_id INTEGER,
            product_code TEXT,
            product_name TEXT,
            unit TEXT,
            quantity REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            disposition TEXT DEFAULT 'continue',
            note TEXT,
            FOREIGN KEY(doc_id) REFERENCES sme_material_remaining(id)
        )
        """
    )
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_material_remaining)').fetchall()}
    for col, typ in (('branch_code', 'TEXT'), ('warehouse_code', 'TEXT')):
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE sme_material_remaining ADD COLUMN {col} {typ}')
            except sqlite3.OperationalError:
                pass
    if commit:
        conn.commit()


def _next_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_material_remaining WHERE doc_no LIKE 'BKVT%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 'BKVT000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'BKVT{int(digits) + 1:06d}'


def create_material_remaining(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    lines: list[dict],
    department: str = '',
    notes: str = '',
    warehouse_code: str = '',
    branch_code: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Lập 04-VT — chứng từ theo dõi VT còn lại tại bộ phận (không bắt buộc GL)."""
    from Services.sme.branches import get_warehouse_branch_code, resolve_posting_branch

    ensure_sme_material_remaining_schema(conn, commit=False)
    date_s = str(as_of_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày cuối kỳ')
    if not lines:
        raise ValueError('Thiếu dòng vật tư')

    wh = (warehouse_code or '').strip()
    if branch_code:
        branch = resolve_posting_branch(conn, branch_code)
    elif wh:
        branch = resolve_posting_branch(conn, get_warehouse_branch_code(conn, wh))
    else:
        branch = resolve_posting_branch(conn, None)

    prepared = []
    total = Decimal('0.00')
    for i, raw in enumerate(lines, start=1):
        name = (raw.get('product_name') or raw.get('name') or '').strip()
        qty = _money(raw.get('quantity') or 0)
        cost = _money(raw.get('unit_cost') or raw.get('unit_price') or 0)
        amt = _money(raw.get('amount')) if raw.get('amount') is not None else (qty * cost)
        if qty <= 0:
            continue
        if not name and not raw.get('product_id'):
            continue
        disp = (raw.get('disposition') or 'continue').strip().lower()
        if disp not in ('continue', 'return'):
            disp = 'continue'
        prepared.append({
            'line_no': i,
            'product_id': raw.get('product_id'),
            'product_code': raw.get('product_code') or '',
            'product_name': name or f"SP#{raw.get('product_id')}",
            'unit': raw.get('unit') or '',
            'quantity': float(qty),
            'unit_cost': float(cost),
            'amount': float(amt),
            'disposition': disp,
            'note': raw.get('note') or '',
        })
        total += amt
    if not prepared:
        raise ValueError('Không có dòng hợp lệ (SL > 0)')

    doc_no = _next_no(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_material_remaining (
            form_code, doc_no, as_of_date, department, notes, total_amount,
            status, created_by, created_at, branch_code, warehouse_code
        ) VALUES (?,?,?,?,?,?,'posted',?,?,?,?)
        """,
        (
            FORM_CODE, doc_no, date_s, department or '', notes or '', float(total),
            created_by, _now(), branch, wh or None,
        ),
    )
    doc_id = cur.lastrowid
    for ln in prepared:
        cur.execute(
            """
            INSERT INTO sme_material_remaining_lines (
                doc_id, line_no, product_id, product_code, product_name, unit,
                quantity, unit_cost, amount, disposition, note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                doc_id, ln['line_no'], ln['product_id'], ln['product_code'],
                ln['product_name'], ln['unit'], ln['quantity'], ln['unit_cost'],
                ln['amount'], ln['disposition'], ln['note'],
            ),
        )
    if commit:
        conn.commit()
    return get_material_remaining(conn, doc_id)


def get_material_remaining(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    ensure_sme_material_remaining_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_material_remaining WHERE id = ?', (doc_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_material_remaining_lines WHERE doc_id = ? ORDER BY line_no, id',
        (doc_id,),
    ).fetchall()]
    return d


def list_material_remaining(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_material_remaining_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE

    sql = "SELECT * FROM sme_material_remaining WHERE status != 'void'"
    params: list[Any] = []
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
        else:
            sql += ' AND branch_code = ?'
        params.append(code)
    sql += ' ORDER BY as_of_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_material_remaining(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy bảng VT còn lại',
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_material_remaining', doc_id, label='Bảng kê VT còn lại')
    doc = get_material_remaining(conn, doc_id)
    if not doc:
        raise ValueError('Không tìm thấy bảng kê VT còn lại')
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    conn.execute(
        "UPDATE sme_material_remaining SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', doc_id),
    )
    if commit:
        conn.commit()
    return get_material_remaining(conn, doc_id)
