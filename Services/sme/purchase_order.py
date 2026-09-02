"""Đơn đặt hàng nhà cung cấp (SME) — theo dõi trước khi nhập kho / nhận HĐ."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')
STATUSES = ('draft', 'confirmed', 'partial', 'received', 'cancelled')

# PO#000001 (chuẩn) hoặc PO20250723001 (đơn cũ) trong GChu HĐ NCC
_PO_HASH_RE = re.compile(r'(?i)\bPO\s*#\s*(\d{1,8})\b')
_PO_COMPACT_RE = re.compile(r'(?i)\bPO(\d{8})(\d{2,5})\b')
_PO_SPACED_RE = re.compile(r'(?i)\bPO\s*[-./]?\s*(\d{8})\s*[-./]?\s*(\d{1,5})\b')
_PO_GENERIC_RE = re.compile(r'(?i)\b(PO[A-Z0-9][A-Z0-9-]{4,22})\b')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def ensure_purchase_order_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    from db_utils import sqlite_is_ready, sqlite_mark_ready

    if sqlite_is_ready(conn, 'purchase_order_schema_v1'):
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_no TEXT NOT NULL UNIQUE,
            po_date TEXT NOT NULL,
            expected_date TEXT,
            supplier_id INTEGER,
            supplier_code TEXT,
            supplier_name TEXT NOT NULL,
            supplier_tax_code TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            note TEXT,
            total_amount REAL NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_purchase_order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL DEFAULT 1,
            product_id INTEGER,
            product_code TEXT,
            product_name TEXT NOT NULL,
            unit TEXT,
            qty REAL NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            received_qty REAL NOT NULL DEFAULT 0,
            note TEXT,
            FOREIGN KEY (po_id) REFERENCES sme_purchase_orders(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sme_po_date ON sme_purchase_orders(po_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sme_po_status ON sme_purchase_orders(status)"
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_purchase_orders')
    from db.schema_helpers import add_column_if_missing, table_cols
    line_cols = table_cols(conn, 'sme_purchase_order_lines')
    if 'received_qty' not in {c.lower() for c in line_cols}:
        conn.execute(
            'ALTER TABLE sme_purchase_order_lines ADD COLUMN received_qty REAL NOT NULL DEFAULT 0'
        )
    add_column_if_missing(conn, 'sme_purchase_orders', 'sent_at', 'TEXT')
    add_column_if_missing(conn, 'sme_purchase_orders', 'sent_to_email', 'TEXT')
    try:
        from db.schema_helpers import table_cols as _tc
        imp_cols = {c.lower() for c in _tc(conn, 'import')}
        if 'po_id' not in imp_cols:
            conn.execute('ALTER TABLE import ADD COLUMN po_id INTEGER')
    except sqlite3.Error:
        pass
    sqlite_mark_ready(conn, 'purchase_order_schema_v1')
    if commit:
        sqlite_commit(conn, label='purchase_order')


def normalize_po_no(po_no: str | None) -> str | None:
    """Chuẩn hóa mã PO → PO#000001."""
    if not po_no:
        return None
    s = re.sub(r'\s+', '', str(po_no).strip().upper())
    m = re.match(r'^PO#(\d+)$', s)
    if m:
        return f'PO#{int(m.group(1)):06d}'
    return str(po_no).strip().upper() or None


def extract_po_no_from_text(text: str | None) -> str | None:
    """Trích số đơn đặt hàng từ GChu / ghi chú trên HĐ XML NCC."""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    m = _PO_HASH_RE.search(s)
    if m:
        return f'PO#{int(m.group(1)):06d}'
    m = _PO_COMPACT_RE.search(s)
    if m:
        return f'PO{m.group(1)}{int(m.group(2)):03d}'
    m = _PO_SPACED_RE.search(s)
    if m:
        return f'PO{m.group(1)}{int(m.group(2)):03d}'
    m = _PO_GENERIC_RE.search(s)
    if m:
        raw = re.sub(r'[^A-Z0-9#]', '', m.group(1).upper())
        if raw.startswith('PO#'):
            num = raw[3:]
            if num.isdigit():
                return f'PO#{int(num):06d}'
        if raw.startswith('PO') and len(raw) >= 10:
            return raw
    return None


def extract_gchu_from_invoice_payload(data: dict | None) -> str:
    """Lấy GChu từ payload Mắt Bảo / JSON đã parse."""
    if not isinstance(data, dict):
        return ''
    for key in ('GChu', 'gChu', 'gchu', 'GHI_CHU', 'ghiChu'):
        val = data.get(key)
        if val not in (None, ''):
            return str(val).strip()
    tt = data.get('TTChung')
    if isinstance(tt, dict):
        for key in ('GChu', 'gChu', 'gchu'):
            val = tt.get(key)
            if val not in (None, ''):
                return str(val).strip()
    return ''


def lookup_po_id_by_po_no(
    conn: sqlite3.Connection,
    po_no: str,
    *,
    supplier_tax_code: str | None = None,
) -> int | None:
    """Tìm id đơn mua theo mã PO (ưu tiên khớp MST NCC nếu có)."""
    ensure_purchase_order_schema(conn, commit=False)
    po_no = normalize_po_no(po_no) or (po_no or '').strip().upper()
    if not po_no:
        return None
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, supplier_tax_code FROM sme_purchase_orders
        WHERE UPPER(TRIM(po_no)) = ?
        ORDER BY id DESC
        LIMIT 5
        """,
        (po_no,),
    ).fetchall()
    if not rows:
        return None
    tax = (supplier_tax_code or '').strip()
    if tax:
        for row in rows:
            po_tax = (row['supplier_tax_code'] or '').strip()
            if po_tax and po_tax == tax:
                return int(row['id'])
    return int(rows[0]['id'])


def match_po_from_gchu(
    conn: sqlite3.Connection,
    gchu: str | None,
    *,
    supplier_tax_code: str | None = None,
) -> dict[str, Any]:
    """Khớp đơn mua từ GChu — trả po_id / po_no nếu tìm được."""
    note = (gchu or '').strip()
    po_no = extract_po_no_from_text(note)
    if not po_no:
        return {'po_id': None, 'po_no': None, 'gchu': note, 'source': None}
    po_id = lookup_po_id_by_po_no(conn, po_no, supplier_tax_code=supplier_tax_code)
    return {
        'po_id': po_id,
        'po_no': po_no,
        'gchu': note,
        'source': 'gchu' if po_id else None,
    }


def link_supplier_invoice_po(
    conn: sqlite3.Connection,
    invoice_id: int,
    *,
    po_id: int | None = None,
    po_no: str | None = None,
    gchu: str | None = None,
    source: str = 'manual',
    commit: bool = False,
) -> dict[str, Any]:
    """Gán / bỏ gán đơn mua trên HĐ NCC (tự động từ GChu hoặc thủ công)."""
    from Services.inward_invoice_helpers import ensure_supplier_invoice_po_schema

    ensure_supplier_invoice_po_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, seller_tax_code, po_id FROM supplier_invoice WHERE id = ?",
        (int(invoice_id),),
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy hóa đơn mua')

    resolved_id = int(po_id) if po_id not in (None, '', 0, '0') else None
    matched_no = normalize_po_no(po_no) or (po_no or '').strip().upper() or None
    note = (gchu or '').strip()

    if resolved_id is None and matched_no:
        resolved_id = lookup_po_id_by_po_no(
            conn, matched_no, supplier_tax_code=row['seller_tax_code'],
        )
    elif resolved_id is not None:
        po_row = conn.execute(
            "SELECT po_no FROM sme_purchase_orders WHERE id = ?", (resolved_id,),
        ).fetchone()
        if not po_row:
            raise ValueError('Không tìm thấy đơn đặt hàng')
        matched_no = str(
            po_row['po_no'] if isinstance(po_row, sqlite3.Row) else po_row[0]
        ).strip().upper()

    if resolved_id is None and source != 'manual':
        conn.execute(
            """
            UPDATE supplier_invoice SET
                gchu = COALESCE(NULLIF(?, ''), gchu)
            WHERE id = ?
            """,
            (note, int(invoice_id)),
        )
        if commit:
            sqlite_commit(conn, label='invoice_po_gchu')
        return {'po_id': None, 'po_no': matched_no, 'gchu': note, 'source': None}

    conn.execute(
        """
        UPDATE supplier_invoice SET
            po_id = ?,
            po_no_matched = ?,
            gchu = COALESCE(NULLIF(?, ''), gchu),
            po_match_source = ?
        WHERE id = ?
        """,
        (
            resolved_id,
            matched_no,
            note,
            source if resolved_id else None,
            int(invoice_id),
        ),
    )
    if commit:
        sqlite_commit(conn, label='invoice_po_link')
    return {
        'po_id': resolved_id,
        'po_no': matched_no,
        'gchu': note,
        'source': source if resolved_id else None,
    }


def apply_gchu_po_match_to_invoice_store(
    conn: sqlite3.Connection,
    inv_store: dict,
    *,
    invoice_id: int | None = None,
    seller_tax_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Đọc GChu từ payload HĐ, khớp PO và lưu vào supplier_invoice nếu có invoice_id."""
    from Services.inward_invoice_helpers import ensure_supplier_invoice_po_schema

    ensure_supplier_invoice_po_schema(conn)
    gchu = extract_gchu_from_invoice_payload(inv_store)
    tax = seller_tax_code or inv_store.get('NBanMST') or inv_store.get('nBanMST')
    matched = match_po_from_gchu(conn, gchu, supplier_tax_code=tax)
    if invoice_id and (matched.get('po_id') or gchu):
        cols = {c[1] for c in conn.execute('PRAGMA table_info(supplier_invoice)').fetchall()}
        if 'po_id' in cols:
            existing = conn.execute(
                "SELECT po_id, po_match_source FROM supplier_invoice WHERE id = ?",
                (int(invoice_id),),
            ).fetchone()
            keep_manual = (
                existing
                and existing[0]
                and len(existing) > 1
                and (existing[1] or '') == 'manual'
            )
            if not keep_manual and matched.get('po_id'):
                link_supplier_invoice_po(
                    conn,
                    int(invoice_id),
                    po_id=int(matched['po_id']),
                    po_no=matched.get('po_no'),
                    gchu=gchu,
                    source='gchu',
                    commit=False,
                )
            elif gchu and not keep_manual:
                conn.execute(
                    "UPDATE supplier_invoice SET gchu = ? WHERE id = ? AND COALESCE(gchu, '') = ''",
                    (gchu, int(invoice_id)),
                )
    if commit:
        sqlite_commit(conn, label='invoice_po_auto')
    inv_store['GChu'] = gchu
    if matched.get('po_id'):
        inv_store['po_id'] = matched['po_id']
        inv_store['po_no'] = matched.get('po_no')
    return matched


def _next_po_no(conn: sqlite3.Connection, po_date: str | None = None) -> str:
    """Sinh số đơn PO#000001, PO#000002, … (tăng dần toàn hệ thống)."""
    max_seq = 0
    for row in conn.execute(
        "SELECT po_no FROM sme_purchase_orders WHERE po_no LIKE 'PO#%'",
    ).fetchall():
        po = row[0] if not isinstance(row, sqlite3.Row) else row['po_no']
        m = re.match(r'(?i)^PO#(\d+)$', str(po or '').strip())
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return f'PO#{max_seq + 1:06d}'


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(row)


def get_purchase_order(conn: sqlite3.Connection, po_id: int) -> dict[str, Any] | None:
    ensure_purchase_order_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    head = conn.execute(
        "SELECT * FROM sme_purchase_orders WHERE id = ?", (po_id,)
    ).fetchone()
    if not head:
        return None
    lines = conn.execute(
        """
        SELECT * FROM sme_purchase_order_lines
        WHERE po_id = ? ORDER BY sequence, id
        """,
        (po_id,),
    ).fetchall()
    data = dict(head)
    data['lines'] = [dict(x) for x in lines]
    try:
        from Services import crm_email as crm_mail
        data['supplier_email'] = crm_mail.resolve_supplier_email(
            conn,
            supplier_id=data.get('supplier_id'),
            supplier_name=data.get('supplier_name'),
            supplier_tax_code=data.get('supplier_tax_code'),
        )
    except Exception:
        data['supplier_email'] = ''
    return data


def list_purchase_orders(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_purchase_order_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM sme_purchase_orders WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if keyword:
        sql += " AND (po_no LIKE ? OR supplier_name LIKE ? OR IFNULL(supplier_tax_code,'') LIKE ?)"
        like = f'%{keyword.strip()}%'
        params.extend([like, like, like])
    if date_from:
        sql += " AND po_date >= ?"
        params.append(date_from[:10])
    if date_to:
        sql += " AND po_date <= ?"
        params.append(date_to[:10])
    from Services.sme.branch_filter import branch_where
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += " ORDER BY po_date DESC, id DESC LIMIT ?"
    params.append(int(limit) or 200)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    ids = [int(r['id']) for r in rows]
    progress: dict[int, dict[str, float]] = {}
    if ids:
        ph = ','.join('?' * len(ids))
        for ln in conn.execute(
            f"""
            SELECT po_id,
                   COALESCE(SUM(qty), 0) AS ordered_qty,
                   COALESCE(SUM(received_qty), 0) AS received_qty
            FROM sme_purchase_order_lines
            WHERE po_id IN ({ph})
            GROUP BY po_id
            """,
            ids,
        ).fetchall():
            d = dict(ln)
            pid = int(d['po_id'])
            ordered = float(d['ordered_qty'] or 0)
            recv = float(d['received_qty'] or 0)
            progress[pid] = {
                'ordered_qty': ordered,
                'received_qty': recv,
                'receive_pct': round(100.0 * recv / ordered, 1) if ordered else 0.0,
            }
    for r in rows:
        r.update(progress.get(int(r['id']), {
            'ordered_qty': 0.0, 'received_qty': 0.0, 'receive_pct': 0.0,
        }))
    billed: dict[int, float] = {}
    invoiced: dict[int, float] = {}
    try:
        imp_cols = {c[1] for c in conn.execute('PRAGMA table_info(import)').fetchall()}
        if ids and 'po_id' in imp_cols:
            ph = ','.join('?' * len(ids))
            for ln in conn.execute(
                f"""
                SELECT po_id, COALESCE(SUM(total_value), 0) AS billed
                FROM import
                WHERE po_id IN ({ph})
                GROUP BY po_id
                """,
                ids,
            ).fetchall():
                d = dict(ln)
                billed[int(d['po_id'])] = float(d['billed'] or 0)
        si_cols = {c[1] for c in conn.execute('PRAGMA table_info(supplier_invoice)').fetchall()}
        if ids and 'po_id' in si_cols:
            ph = ','.join('?' * len(ids))
            for ln in conn.execute(
                f"""
                SELECT po_id, COALESCE(SUM(total), 0) AS invoiced
                FROM supplier_invoice
                WHERE po_id IN ({ph})
                GROUP BY po_id
                """,
                ids,
            ).fetchall():
                d = dict(ln)
                invoiced[int(d['po_id'])] = float(d['invoiced'] or 0)
        elif ids:
            # Fallback: ghi chú [PNK#id] trên đơn
            for r in rows:
                note = str(r.get('note') or '')
                pnk_ids = [int(x) for x in re.findall(r'\[PNK#(\d+)\]', note)]
                if not pnk_ids:
                    continue
                ph = ','.join('?' * len(pnk_ids))
                row = conn.execute(
                    f"SELECT COALESCE(SUM(total_value), 0) FROM import WHERE id IN ({ph})",
                    pnk_ids,
                ).fetchone()
                billed[int(r['id'])] = float((row[0] if row else 0) or 0)
    except sqlite3.Error:
        billed = {}
    for r in rows:
        total = float(r.get('total_amount') or 0)
        bill = billed.get(int(r['id']), 0.0)
        inv_amt = invoiced.get(int(r['id']), 0.0)
        r['billed_amount'] = bill
        r['invoiced_amount'] = inv_amt
        r['billed_pct'] = round(100.0 * bill / total, 1) if total else 0.0
        r['invoiced_pct'] = round(100.0 * inv_amt / total, 1) if total else 0.0
        recv_pct = float(r.get('receive_pct') or 0)
        qty_ok = recv_pct >= 99.5
        amt_ok = abs(bill - total) <= max(1.0, total * 0.02) if total else bill <= 0.5
        inv_ok = abs(inv_amt - total) <= max(1.0, total * 0.02) if total else inv_amt <= 0.5
        if r.get('status') == 'cancelled':
            r['match_status'] = 'cancelled'
        elif recv_pct <= 0.05 and bill <= 0.5 and inv_amt <= 0.5:
            r['match_status'] = 'open'
        elif qty_ok and amt_ok and bill > 0:
            r['match_status'] = 'matched'
        elif qty_ok and inv_ok and inv_amt > 0 and bill <= 0.5:
            r['match_status'] = 'invoiced_only'
        elif qty_ok and bill <= 0.5:
            r['match_status'] = 'unbilled'
        elif not qty_ok:
            r['match_status'] = 'qty_short'
        else:
            r['match_status'] = 'amount_diff'
    return rows


def create_purchase_order(
    conn: sqlite3.Connection,
    *,
    po_date: str,
    supplier_name: str,
    lines: list[dict],
    expected_date: str | None = None,
    supplier_id: int | None = None,
    supplier_code: str | None = None,
    supplier_tax_code: str | None = None,
    note: str | None = None,
    status: str = 'draft',
    created_by: str | None = None,
    po_no: str | None = None,
) -> dict[str, Any]:
    ensure_purchase_order_schema(conn, commit=False)
    supplier_name = (supplier_name or '').strip()
    if not supplier_name:
        raise ValueError('Thiếu tên nhà cung cấp')
    if not lines:
        raise ValueError('Đơn hàng phải có ít nhất một dòng')
    po_date = (po_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    status = status if status in STATUSES else 'draft'
    po_no = (po_no or '').strip() or _next_po_no(conn, po_date)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    prepared = []
    total = Decimal('0.00')
    for i, raw in enumerate(lines, start=1):
        name = str(raw.get('product_name') or '').strip()
        if not name:
            continue
        qty = _money(raw.get('qty'))
        price = _money(raw.get('unit_price'))
        if qty <= 0:
            continue
        amount = _money(qty * price)
        total += amount
        prepared.append({
            'sequence': int(raw.get('sequence') or i),
            'product_id': raw.get('product_id'),
            'product_code': raw.get('product_code'),
            'product_name': name,
            'unit': raw.get('unit') or '',
            'qty': float(qty),
            'unit_price': float(price),
            'amount': float(amount),
            'received_qty': float(_money(raw.get('received_qty') or 0)),
            'note': raw.get('note') or '',
        })
    if not prepared:
        raise ValueError('Không có dòng hợp lệ (số lượng > 0)')

    cur = conn.execute(
        """
        INSERT INTO sme_purchase_orders (
            po_no, po_date, expected_date, supplier_id, supplier_code, supplier_name,
            supplier_tax_code, status, note, total_amount, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            po_no, po_date, (expected_date or '')[:10] or None,
            supplier_id, supplier_code, supplier_name, supplier_tax_code,
            status, note, float(total), created_by, now, now,
        ),
    )
    po_id = int(cur.lastrowid)
    from Services.sme.branch_filter import stamp_row_branch
    stamp_row_branch(conn, 'sme_purchase_orders', po_id)
    for ln in prepared:
        conn.execute(
            """
            INSERT INTO sme_purchase_order_lines (
                po_id, sequence, product_id, product_code, product_name, unit,
                qty, unit_price, amount, received_qty, note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                po_id, ln['sequence'], ln['product_id'], ln['product_code'],
                ln['product_name'], ln['unit'], ln['qty'], ln['unit_price'],
                ln['amount'], ln['received_qty'], ln['note'],
            ),
        )
    return get_purchase_order(conn, po_id) or {'id': po_id}


def update_purchase_order(
    conn: sqlite3.Connection,
    po_id: int,
    *,
    po_date: str | None = None,
    expected_date: str | None = None,
    supplier_name: str | None = None,
    supplier_id: int | None = None,
    supplier_code: str | None = None,
    supplier_tax_code: str | None = None,
    note: str | None = None,
    status: str | None = None,
    lines: list[dict] | None = None,
) -> dict[str, Any]:
    ensure_purchase_order_schema(conn, commit=False)
    existing = get_purchase_order(conn, po_id)
    if not existing:
        raise ValueError('Không tìm thấy đơn đặt hàng')
    if existing.get('status') == 'cancelled':
        raise ValueError('Đơn đã hủy — không sửa được')
    if existing.get('status') == 'received' and lines is not None:
        raise ValueError('Đơn đã nhận đủ — không sửa dòng')

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fields = {
        'po_date': (po_date or existing['po_date'])[:10],
        'expected_date': (expected_date if expected_date is not None else existing.get('expected_date')),
        'supplier_name': (supplier_name or existing['supplier_name']).strip(),
        'supplier_id': supplier_id if supplier_id is not None else existing.get('supplier_id'),
        'supplier_code': supplier_code if supplier_code is not None else existing.get('supplier_code'),
        'supplier_tax_code': supplier_tax_code if supplier_tax_code is not None else existing.get('supplier_tax_code'),
        'note': note if note is not None else existing.get('note'),
        'status': status if status in STATUSES else existing.get('status'),
    }
    if not fields['supplier_name']:
        raise ValueError('Thiếu tên nhà cung cấp')

    total = _money(existing.get('total_amount'))
    if lines is not None:
        prepared = []
        total = Decimal('0.00')
        for i, raw in enumerate(lines, start=1):
            name = str(raw.get('product_name') or '').strip()
            if not name:
                continue
            qty = _money(raw.get('qty'))
            price = _money(raw.get('unit_price'))
            if qty <= 0:
                continue
            amount = _money(qty * price)
            total += amount
            prepared.append((
                po_id, int(raw.get('sequence') or i), raw.get('product_id'),
                raw.get('product_code'), name, raw.get('unit') or '',
                float(qty), float(price), float(amount),
                float(_money(raw.get('received_qty') or 0)), raw.get('note') or '',
            ))
        if not prepared:
            raise ValueError('Không có dòng hợp lệ')
        conn.execute("DELETE FROM sme_purchase_order_lines WHERE po_id = ?", (po_id,))
        conn.executemany(
            """
            INSERT INTO sme_purchase_order_lines (
                po_id, sequence, product_id, product_code, product_name, unit,
                qty, unit_price, amount, received_qty, note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            prepared,
        )

    conn.execute(
        """
        UPDATE sme_purchase_orders SET
            po_date=?, expected_date=?, supplier_id=?, supplier_code=?, supplier_name=?,
            supplier_tax_code=?, status=?, note=?, total_amount=?, updated_at=?
        WHERE id=?
        """,
        (
            fields['po_date'], fields['expected_date'], fields['supplier_id'],
            fields['supplier_code'], fields['supplier_name'], fields['supplier_tax_code'],
            fields['status'], fields['note'], float(total), now, po_id,
        ),
    )
    return get_purchase_order(conn, po_id) or existing


def set_purchase_order_status(
    conn: sqlite3.Connection, po_id: int, status: str,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError('Trạng thái không hợp lệ')
    return update_purchase_order(conn, po_id, status=status)


def delete_purchase_order(conn: sqlite3.Connection, po_id: int) -> bool:
    ensure_purchase_order_schema(conn, commit=False)
    existing = get_purchase_order(conn, po_id)
    if not existing:
        return False
    if existing.get('status') not in ('draft', 'cancelled'):
        raise ValueError('Chỉ xóa đơn nháp hoặc đã hủy')
    conn.execute("DELETE FROM sme_purchase_order_lines WHERE po_id = ?", (po_id,))
    conn.execute("DELETE FROM sme_purchase_orders WHERE id = ?", (po_id,))
    return True


def remaining_qty(line: dict) -> Decimal:
    ordered = _money(line.get('qty'))
    received = _money(line.get('received_qty'))
    rem = ordered - received
    return rem if rem > 0 else Decimal('0.00')


def build_import_draft_from_po(conn: sqlite3.Connection, po_id: int) -> dict[str, Any]:
    """Payload điền sẵn phiếu nhập kho từ ĐĐH (chỉ phần chưa nhận)."""
    po = get_purchase_order(conn, po_id)
    if not po:
        raise ValueError('Không tìm thấy đơn đặt hàng')
    if po.get('status') == 'cancelled':
        raise ValueError('Đơn đã hủy')
    if po.get('status') == 'received':
        raise ValueError('Đơn đã nhận đủ')

    items = []
    for ln in po.get('lines') or []:
        rem = remaining_qty(ln)
        if rem <= 0:
            continue
        items.append({
            'line_id': ln.get('id'),
            'product_id': ln.get('product_id'),
            'product_code': ln.get('product_code') or '',
            'invoice_name': ln.get('product_name'),
            'name': ln.get('product_name'),
            'unit': ln.get('unit') or '',
            'qty': float(rem),
            'ordered_qty': float(_money(ln.get('qty'))),
            'received_qty': float(_money(ln.get('received_qty'))),
            'price': float(_money(ln.get('unit_price'))),
            'type': 'goods',
        })
    if not items:
        raise ValueError('Không còn số lượng chờ nhập trên đơn này')

    supplier_id = po.get('supplier_id')
    if not supplier_id and po.get('supplier_name'):
        try:
            row = conn.execute(
                "SELECT id FROM suppliers WHERE name = ? COLLATE NOCASE LIMIT 1",
                (po['supplier_name'],),
            ).fetchone()
            if row:
                supplier_id = row[0] if not isinstance(row, sqlite3.Row) else row['id']
        except sqlite3.Error:
            supplier_id = None

    return {
        'po_id': po['id'],
        'po_no': po.get('po_no'),
        'po_date': po.get('po_date'),
        'status': po.get('status'),
        'note': po.get('note') or f"Theo ĐĐH {po.get('po_no')}",
        'supplier_id': supplier_id,
        'supplier_name': po.get('supplier_name'),
        'supplier_code': po.get('supplier_code'),
        'supplier_tax_code': po.get('supplier_tax_code'),
        'items': items,
    }


def apply_po_receipt(
    conn: sqlite3.Connection,
    po_id: int,
    received_lines: list[dict],
    *,
    import_id: int | None = None,
) -> dict[str, Any]:
    """Cộng số lượng đã nhận sau khi lập phiếu nhập; cập nhật trạng thái ĐĐH."""
    po = get_purchase_order(conn, po_id)
    if not po:
        raise ValueError('Không tìm thấy đơn đặt hàng')
    if po.get('status') == 'cancelled':
        raise ValueError('Đơn đã hủy')

    lines = list(po.get('lines') or [])
    by_id = {int(x['id']): x for x in lines if x.get('id') is not None}
    by_pid = {}
    by_name = {}
    for x in lines:
        if x.get('product_id'):
            by_pid.setdefault(int(x['product_id']), []).append(x)
        key = str(x.get('product_name') or '').strip().lower()
        if key:
            by_name.setdefault(key, []).append(x)

    def _pick(candidates: list[dict]) -> dict | None:
        for c in candidates:
            if remaining_qty(c) > 0:
                return c
        return candidates[0] if candidates else None

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for raw in received_lines or []:
        qty = _money(raw.get('qty') or raw.get('received_qty'))
        if qty <= 0:
            continue
        target = None
        if raw.get('line_id') and int(raw['line_id']) in by_id:
            target = by_id[int(raw['line_id'])]
        elif raw.get('product_id') and int(raw['product_id']) in by_pid:
            target = _pick(by_pid[int(raw['product_id'])])
        else:
            key = str(raw.get('product_name') or raw.get('name') or raw.get('invoice_name') or '').strip().lower()
            if key in by_name:
                target = _pick(by_name[key])
        if not target:
            continue
        new_recv = _money(target.get('received_qty')) + qty
        ordered = _money(target.get('qty'))
        if new_recv > ordered:
            new_recv = ordered
        conn.execute(
            "UPDATE sme_purchase_order_lines SET received_qty = ? WHERE id = ?",
            (float(new_recv), target['id']),
        )
        target['received_qty'] = float(new_recv)

    refreshed = get_purchase_order(conn, po_id) or po
    all_done = True
    any_recv = False
    for ln in refreshed.get('lines') or []:
        recv = _money(ln.get('received_qty'))
        ordered = _money(ln.get('qty'))
        if recv > 0:
            any_recv = True
        if recv + Decimal('0.0001') < ordered:
            all_done = False
    if all_done and any_recv:
        new_status = 'received'
    elif any_recv:
        new_status = 'partial'
    else:
        new_status = refreshed.get('status') or 'confirmed'
    if refreshed.get('status') == 'draft' and any_recv:
        # Nhập từ đơn nháp → coi như đã xác nhận
        if new_status == 'partial' or new_status == 'received':
            pass
        else:
            new_status = 'confirmed'

    note_extra = refreshed.get('note') or ''
    if import_id:
        tag = f'[PNK#{import_id}]'
        if tag not in note_extra:
            note_extra = (note_extra + ' ' + tag).strip()
        try:
            cols = {c[1] for c in conn.execute('PRAGMA table_info(import)').fetchall()}
            if 'po_id' in cols:
                conn.execute('UPDATE import SET po_id = ? WHERE id = ?', (int(po_id), int(import_id)))
        except sqlite3.Error:
            pass

    conn.execute(
        """
        UPDATE sme_purchase_orders
        SET status = ?, note = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_status, note_extra, now, po_id),
    )
    return get_purchase_order(conn, po_id) or refreshed


def purchasing_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Chỉ số hub Mua hàng: PS nhập kho, công nợ 331, ĐĐH chờ nhập."""
    from datetime import datetime as _dt
    from Services.sme.bctc_report import _closing_balances, _monthly_activity_batch, _period_activity
    from Services.sme.dashboard_metrics import _f, _money, _sum_activity, _sum_balance
    from Services.sme.journal_engine import ensure_sme_journal_ready

    ensure_sme_journal_ready(conn, commit=False)
    ensure_purchase_order_schema(conn, commit=False)
    period_to = period_to or _dt.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    activity = _period_activity(
        conn, fiscal_year, 1, period_to, branch_code=branch_code,
    )
    bals = _closing_balances(
        conn, fiscal_year, period_to, branch_code=branch_code,
    )

    # Giá trị mua hàng ≈ phát sinh Nợ TK hàng tồn (152/153/156)
    purchase = _sum_activity(activity, ('152', '153', '156'), side='debit')
    # Thanh toán NCC ≈ phát sinh Nợ 331 (giảm công nợ)
    paid = _sum_activity(activity, ('331',), side='debit')
    if paid < 0:
        paid = Decimal('0.00')
    payable = _sum_balance(bals, ('331',), normal='credit')
    if payable < 0:
        payable = Decimal('0.00')

    prev_to = max(1, period_to - 1)
    prev_act = (
        _period_activity(conn, fiscal_year, 1, prev_to, branch_code=branch_code)
        if period_to > 1 else {}
    )
    prev_purchase = _sum_activity(prev_act, ('152', '153', '156'), side='debit') if period_to > 1 else Decimal('0.00')
    growth = None
    if prev_purchase > 0:
        growth = float((purchase - prev_purchase) / prev_purchase * 100)

    open_pos = conn.execute(
        """
        SELECT COUNT(*) FROM sme_purchase_orders
        WHERE status IN ('draft', 'confirmed', 'partial')
        """
    ).fetchone()[0]
    open_value = conn.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN l.qty > IFNULL(l.received_qty,0)
                 THEN (l.qty - IFNULL(l.received_qty,0)) * l.unit_price ELSE 0 END
        ), 0)
        FROM sme_purchase_order_lines l
        JOIN sme_purchase_orders p ON p.id = l.po_id
        WHERE p.status IN ('draft', 'confirmed', 'partial')
        """
    ).fetchone()[0]

    monthly_map = _monthly_activity_batch(
        conn, fiscal_year, period_to, branch_code=branch_code,
    )
    monthly = []
    for m in range(1, period_to + 1):
        act = monthly_map.get(m, {})
        monthly.append({
            'period': m,
            'label': f'Tháng {m}',
            'purchase': _f(_sum_activity(act, ('152', '153', '156'), side='debit')),
        })

    fy_start = f'{fiscal_year}-01-01'
    fy_end = f'{fiscal_year + 1}-01-01'
    top_rows = conn.execute(
        """
        SELECT supplier_name, SUM(total_amount) AS amt, COUNT(*) AS cnt
        FROM sme_purchase_orders
        WHERE status != 'cancelled'
          AND po_date >= ? AND po_date < ?
        GROUP BY supplier_name
        ORDER BY amt DESC
        LIMIT 6
        """,
        (fy_start, fy_end),
    ).fetchall()
    suppliers = [
        {
            'name': r[0] if not isinstance(r, sqlite3.Row) else r['supplier_name'],
            'amount': float(r[1] if not isinstance(r, sqlite3.Row) else r['amt'] or 0),
            'count': int(r[2] if not isinstance(r, sqlite3.Row) else r['cnt'] or 0),
        }
        for r in top_rows
    ]

    recent_open = list_purchase_orders(conn, limit=8)
    recent_open = [x for x in recent_open if x.get('status') in ('draft', 'confirmed', 'partial')][:8]

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'total_purchase': _f(purchase),
        'total_paid': _f(paid),
        'total_payable': _f(payable),
        'pending_orders': int(open_pos or 0),
        'pending_order_value': float(open_value or 0),
        'growth_pct': growth,
        'paid_rate_pct': float(paid / purchase * 100) if purchase > 0 else None,
        'monthly': monthly,
        'suppliers': suppliers,
        'open_orders': recent_open,
    }


def send_purchase_order_email(
    conn: sqlite3.Connection,
    po_id: int,
    *,
    to_email: str | None = None,
    subject: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Gửi đơn đặt hàng cho NCC qua SMTP tenant (Thiết lập / CRM)."""
    from Services import crm_email as crm_mail

    po = get_purchase_order(conn, po_id)
    if not po:
        raise ValueError('Không tìm thấy đơn đặt hàng')
    if po.get('status') == 'cancelled':
        raise ValueError('Đơn đã hủy — không gửi email')
    dest = (to_email or '').strip() or crm_mail.resolve_supplier_email(
        conn,
        supplier_id=po.get('supplier_id'),
        supplier_name=po.get('supplier_name'),
        supplier_tax_code=po.get('supplier_tax_code'),
    )
    if not dest:
        raise ValueError(
            'NCC chưa có email. Cập nhật email trong danh mục NCC hoặc nhập email nhận.'
        )
    subj, text, html = crm_mail.build_purchase_order_email(
        po, business_name=crm_mail._business_name(conn),
    )
    if subject:
        subj = str(subject).strip() or subj
    ok, err, source = crm_mail.send_tenant_email(
        conn, dest, subj, text, html_body=html,
    )
    crm_mail.log_crm_email(
        conn, kind='purchase_order', ref_id=po_id, to_email=dest,
        subject=subj, status='ok' if ok else 'error', error=err,
    )
    if ok:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            """
            UPDATE sme_purchase_orders
            SET sent_at = ?, sent_to_email = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, dest, now, po_id),
        )
        if po.get('status') == 'draft':
            conn.execute(
                "UPDATE sme_purchase_orders SET status = 'confirmed', updated_at = ? WHERE id = ?",
                (now, po_id),
            )
    if commit:
        sqlite_commit(conn, label='po_email')
    if not ok:
        raise ValueError(err or 'Gửi email thất bại')
    out = get_purchase_order(conn, po_id) or po
    return {
        'po_id': po_id,
        'to_email': dest,
        'subject': subj,
        'source': source,
        'sent_at': out.get('sent_at'),
        'sent_by': created_by,
    }
