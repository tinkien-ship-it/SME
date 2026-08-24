"""Biên bản kiểm nghiệm vật tư SME — mẫu 03-VT."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from db_utils import sqlite_commit


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_stock_inspection_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_stock_inspections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '03-VT',
            doc_no TEXT NOT NULL UNIQUE,
            inspect_date TEXT NOT NULL,
            import_id INTEGER,
            import_no TEXT,
            supplier_name TEXT,
            method TEXT DEFAULT 'Toàn diện',
            committee TEXT,
            opinion TEXT,
            status TEXT NOT NULL DEFAULT 'accepted',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_stock_inspection_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inspection_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL DEFAULT 1,
            product_id INTEGER,
            product_code TEXT,
            product_name TEXT NOT NULL,
            unit TEXT,
            invoice_qty REAL DEFAULT 0,
            actual_qty REAL DEFAULT 0,
            quality_note TEXT,
            result TEXT DEFAULT 'Đạt',
            FOREIGN KEY(inspection_id) REFERENCES sme_stock_inspections(id)
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_stock_inspections')
    if commit:
        sqlite_commit(conn, label='stock_inspection')


def _next_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_stock_inspections WHERE doc_no LIKE 'KN%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 'KN000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'KN{int(digits) + 1:06d}'


def create_stock_inspection(
    conn: sqlite3.Connection,
    *,
    inspect_date: str,
    lines: list[dict],
    import_id: int | None = None,
    import_no: str = '',
    supplier_name: str = '',
    method: str = 'Toàn diện',
    committee: str = '',
    opinion: str = '',
    status: str = 'accepted',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Lập biên bản kiểm nghiệm 03-VT (trước/ khi nhập kho) — không ghi GL."""
    ensure_sme_stock_inspection_schema(conn, commit=False)
    date_s = str(inspect_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày kiểm nghiệm')
    if not lines:
        raise ValueError('Thiếu dòng kiểm nghiệm')

    # Enrich from import if provided
    imp_no = import_no
    supplier = supplier_name
    if import_id and not imp_no:
        try:
            row = conn.execute(
                'SELECT import_no, supplier_name FROM import WHERE id = ?', (import_id,)
            ).fetchone()
            if row:
                r = dict(row)
                imp_no = r.get('import_no') or imp_no
                supplier = supplier or r.get('supplier_name') or ''
        except sqlite3.Error:
            pass

    doc_no = _next_no(conn)
    st = (status or 'accepted').strip().lower()
    if st not in ('accepted', 'rejected', 'partial'):
        st = 'accepted'

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_stock_inspections (
            form_code, doc_no, inspect_date, import_id, import_no, supplier_name,
            method, committee, opinion, status, notes, created_by, created_at
        ) VALUES ('03-VT',?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            doc_no, date_s, import_id, imp_no or None, supplier or '',
            method or 'Toàn diện', committee or '', opinion or '',
            st, notes or '', created_by, _now(),
        ),
    )
    iid = cur.lastrowid
    n = 0
    for i, raw in enumerate(lines, start=1):
        name = (raw.get('product_name') or raw.get('name') or '').strip()
        if not name and raw.get('product_id'):
            try:
                pr = conn.execute(
                    'SELECT name, unit FROM products WHERE id = ?', (int(raw['product_id']),)
                ).fetchone()
                if pr:
                    name = pr[0] if not isinstance(pr, sqlite3.Row) else pr['name']
                    raw.setdefault('unit', pr[1] if not isinstance(pr, sqlite3.Row) else pr['unit'])
            except sqlite3.Error:
                pass
        if not name:
            continue
        cur.execute(
            """
            INSERT INTO sme_stock_inspection_lines (
                inspection_id, line_no, product_id, product_code, product_name, unit,
                invoice_qty, actual_qty, quality_note, result
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                iid, i, raw.get('product_id'),
                raw.get('product_code') or '', name, raw.get('unit') or '',
                float(raw.get('invoice_qty') or 0), float(raw.get('actual_qty') or 0),
                raw.get('quality_note') or '', raw.get('result') or 'Đạt',
            ),
        )
        n += 1
    if n == 0:
        conn.execute('DELETE FROM sme_stock_inspections WHERE id = ?', (iid,))
        raise ValueError('Không có dòng hợp lệ')
    from Services.sme.branch_filter import stamp_row_branch
    stamp_row_branch(conn, 'sme_stock_inspections', iid)
    if commit:
        sqlite_commit(conn, label='stock_inspection')
    return get_stock_inspection(conn, iid)


def get_stock_inspection(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    ensure_sme_stock_inspection_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_stock_inspections WHERE id = ?', (doc_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_stock_inspection_lines WHERE inspection_id = ? ORDER BY line_no, id',
        (doc_id,),
    ).fetchall()]
    return d


def list_stock_inspections(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_stock_inspection_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = "SELECT * FROM sme_stock_inspections WHERE status != 'void'"
    params: list[Any] = []
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY inspect_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_stock_inspection(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy kiểm nghiệm VT',
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_stock_inspections', doc_id, label='Biên bản kiểm nghiệm')
    doc = get_stock_inspection(conn, doc_id)
    if not doc:
        raise ValueError('Không tìm thấy biên bản kiểm nghiệm')
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    conn.execute(
        "UPDATE sme_stock_inspections SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', doc_id),
    )
    if commit:
        sqlite_commit(conn, label='stock_inspection')
    return get_stock_inspection(conn, doc_id)
