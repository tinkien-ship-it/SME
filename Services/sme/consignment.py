"""Hàng gửi đi bán (TK 157) — đại lý / ký gửi nội địa.

Luồng TT99:
  1) Xuất gửi: Nợ 157 / Có 155|156 + trừ tồn kho
  2) Xác nhận bán: Nợ 632 / Có 157 + Nợ 111|112|131 / Có 511|3331
  3) Trả lại: Nợ 155|156 (còn tốt) hoặc Nợ 632 (hỏng) / Có 157
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.inventory_stock_helpers import sync_inventory_quantity_from_moves
from Services.sme.cogs_accounts import cogs_accounts_for_line, cogs_spoilage_account
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    reverse_journal_entry,
    resolve_postable_account,
)

MONEY_Q = Decimal('0.01')
DOC_SHIP = 'CONSIGN_SHIP'
DOC_SALE_COGS = 'CONSIGN_SALE_COGS'
DOC_SALE_REV = 'CONSIGN_SALE_REV'
DOC_RETURN = 'CONSIGN_RETURN'
STATUS_SHIPPED = 'shipped'
STATUS_VOID = 'void'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def _ensure_col(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    if col in _cols(conn, table):
        return
    conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')


def ensure_consignment_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    from Services.sme.sale_forms import ensure_agent_delivery_schema

    ensure_agent_delivery_schema(conn, commit=False)
    for col, decl in (
        ('warehouse_code', 'TEXT'),
        ('customer_id', 'INTEGER'),
        ('journal_ship_id', 'INTEGER'),
        ('total_cost', 'REAL DEFAULT 0'),
        ('agent_address', 'TEXT'),
        ('agent_tax_code', 'TEXT'),
        ('agent_phone', 'TEXT'),
        ('agent_email', 'TEXT'),
        ('email_sent_at', 'TEXT'),
        ('email_error', 'TEXT'),
    ):
        _ensure_col(conn, 'sme_agent_deliveries', col, decl)
    for col, decl in (
        ('unit_cost', 'REAL DEFAULT 0'),
        ('amount', 'REAL DEFAULT 0'),
        ('product_type', 'TEXT'),
        ('qty_sold', 'REAL DEFAULT 0'),
        ('qty_returned', 'REAL DEFAULT 0'),
        ('qty_damaged', 'REAL DEFAULT 0'),
    ):
        _ensure_col(conn, 'sme_agent_delivery_lines', col, decl)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_consign_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_date TEXT NOT NULL,
            payment_method TEXT,
            journal_cogs_id INTEGER,
            journal_rev_id INTEGER,
            journal_return_id INTEGER,
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(delivery_id) REFERENCES sme_agent_deliveries(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_consign_event_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            delivery_line_id INTEGER NOT NULL,
            product_id INTEGER,
            quantity REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            tax_pct REAL NOT NULL DEFAULT 0,
            is_damaged INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(event_id) REFERENCES sme_consign_events(id)
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_agent_deliveries')
    if commit:
        conn.commit()


def _avg_cost(conn: sqlite3.Connection, product_id: int) -> float:
    row = conn.execute(
        'SELECT COALESCE(avg_cost, 0) FROM inventory WHERE product_id = ?',
        (product_id,),
    ).fetchone()
    return float(row[0] if row else 0)


def _product_meta(conn: sqlite3.Connection, product_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, name, unit, product_code, barcode,
               COALESCE(product_type, 'goods') AS product_type
        FROM products WHERE id = ?
        """,
        (product_id,),
    ).fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy sản phẩm #{product_id}')
    d = dict(row)
    code = str(d.get('product_code') or '').strip().upper()
    if code.startswith('TP'):
        d['product_type'] = 'finished_goods'
    elif code.startswith('SP'):
        d['product_type'] = 'goods'
    return d


def _is_shippable_consign_code(product_code: str | None) -> bool:
    code = str(product_code or '').strip().upper()
    return code.startswith('SP') or code.startswith('TP')


def _inv_role(product_type: str | None) -> str:
    _cogs, inv, _ = cogs_accounts_for_line(product_type, channel='domestic')
    return inv or 'inv.goods'


def _cogs_role(product_type: str | None) -> str:
    cogs, _inv, _ = cogs_accounts_for_line(product_type, channel='domestic')
    return cogs


def _revenue_role(product_type: str | None) -> str:
    pt = (product_type or 'goods').strip().lower()
    if pt in ('finished_goods', 'finished', 'thanh_pham', 'ready_made'):
        return 'revenue.fg'
    if pt in ('service', 'services', 'dich_vu'):
        return 'revenue.service'
    return 'revenue.goods'


def _payment_debit(conn: sqlite3.Connection, method: str) -> str:
    m = (method or '131').strip().lower()
    if m in ('111', 'cash', 'tm', 'tien_mat'):
        return resolve_postable_account(conn, '1111')
    if m in ('112', 'bank', 'ck', 'ngan_hang', '1121'):
        return resolve_postable_account(conn, '1121')
    return resolve_postable_account(conn, '131')


def _vat_out(conn: sqlite3.Connection) -> str:
    try:
        return resolve_postable_account(conn, 'tax.vat.out')
    except Exception:
        return resolve_postable_account(conn, '33311')


def _wh_qty(conn: sqlite3.Connection, product_id: int, warehouse: str) -> float:
    sm_cols = _cols(conn, 'stock_moves')
    if 'warehouse_code' in sm_cols and warehouse:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(quantity), 0) FROM stock_moves
            WHERE product_id = ? AND warehouse_code = ?
            """,
            (product_id, warehouse),
        ).fetchone()
        return float(row[0] if row else 0)
    row = conn.execute(
        'SELECT COALESCE(quantity, 0) FROM inventory WHERE product_id = ?',
        (product_id,),
    ).fetchone()
    return float(row[0] if row else 0)


def _insert_stock_export(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    qty: float,
    cost: float,
    doc_id: int,
    doc_no: str,
    warehouse: str,
    when: str,
    note: str,
) -> None:
    sm_cols = _cols(conn, 'stock_moves')
    fields = ['product_id', 'date', 'type', 'ref_id', 'ref_document', 'ref_type', 'quantity', 'note']
    values: list[Any] = [product_id, when, 'export', doc_id, doc_no, 'consign_ship', -abs(qty), note]
    if 'type1' in sm_cols:
        fields.append('type1')
        values.append('Hàng gửi đi bán')
    if 'cost_price' in sm_cols:
        fields.append('cost_price')
        values.append(cost)
    if 'avg_cost' in sm_cols:
        fields.append('avg_cost')
        values.append(cost)
    if 'warehouse_code' in sm_cols and warehouse:
        fields.append('warehouse_code')
        values.append(warehouse)
    conn.execute(
        f"INSERT INTO stock_moves ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
        values,
    )
    try:
        sync_inventory_quantity_from_moves(conn.cursor(), product_id)
    except Exception:
        pass


def _insert_stock_import(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    qty: float,
    cost: float,
    doc_id: int,
    doc_no: str,
    warehouse: str,
    when: str,
    note: str,
) -> None:
    sm_cols = _cols(conn, 'stock_moves')
    fields = ['product_id', 'date', 'type', 'ref_id', 'ref_document', 'ref_type', 'quantity', 'note']
    values: list[Any] = [product_id, when, 'import', doc_id, doc_no, 'consign_return', abs(qty), note]
    if 'type1' in sm_cols:
        fields.append('type1')
        values.append('Trả hàng gửi đi bán')
    if 'cost_price' in sm_cols:
        fields.append('cost_price')
        values.append(cost)
    if 'avg_cost' in sm_cols:
        fields.append('avg_cost')
        values.append(cost)
    if 'warehouse_code' in sm_cols and warehouse:
        fields.append('warehouse_code')
        values.append(warehouse)
    conn.execute(
        f"INSERT INTO stock_moves ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
        values,
    )
    try:
        sync_inventory_quantity_from_moves(conn.cursor(), product_id)
    except Exception:
        pass


def get_consignment(conn: sqlite3.Connection, delivery_id: int) -> dict[str, Any] | None:
    ensure_consignment_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_agent_deliveries WHERE id = ?', (delivery_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    lines = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_agent_delivery_lines WHERE delivery_id = ? ORDER BY id',
        (delivery_id,),
    ).fetchall()]
    for ln in lines:
        qty = _f(ln.get('quantity'))
        sold = _f(ln.get('qty_sold'))
        ret = _f(ln.get('qty_returned')) + _f(ln.get('qty_damaged'))
        ln['qty_open'] = max(0.0, qty - sold - ret)
    d['lines'] = lines
    d['events'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_consign_events WHERE delivery_id = ? ORDER BY id DESC',
        (delivery_id,),
    ).fetchall()]
    return d


def list_consignments(
    conn: sqlite3.Connection,
    *,
    agent_name: str | None = None,
    status: str | None = None,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_consignment_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where

    bf, bp = branch_where(branch_code)
    sql = 'SELECT * FROM sme_agent_deliveries WHERE 1=1'
    params: list[Any] = []
    if status:
        sql += ' AND status = ?'
        params.append(status)
    else:
        sql += " AND status != 'void'"
    if agent_name:
        sql += ' AND TRIM(agent_name) = ?'
        params.append(agent_name.strip())
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY delivery_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r['lines'] = [dict(x) for x in conn.execute(
            'SELECT * FROM sme_agent_delivery_lines WHERE delivery_id = ?',
            (r['id'],),
        ).fetchall()]
    return rows


def _valid_email(email: str) -> bool:
    e = (email or '').strip()
    return bool(e) and '@' in e and '.' in e.split('@')[-1]


def normalize_agent_contact(
    *,
    agent_name: str = '',
    agent_address: str = '',
    agent_tax_code: str = '',
    agent_phone: str = '',
    agent_email: str = '',
) -> dict[str, str]:
    out = {
        'name': (agent_name or '').strip(),
        'address': (agent_address or '').strip(),
        'tax_code': (agent_tax_code or '').strip(),
        'phone': (agent_phone or '').strip(),
        'email': (agent_email or '').strip().lower(),
    }
    missing = []
    if not out['name']:
        missing.append('Tên đại lý')
    if not out['address']:
        missing.append('Địa chỉ')
    if not out['tax_code']:
        missing.append('Mã số thuế')
    if not out['phone']:
        missing.append('Số điện thoại')
    if not out['email']:
        missing.append('Email')
    elif not _valid_email(out['email']):
        raise ValueError('Email đại lý không hợp lệ')
    if missing:
        raise ValueError('Thiếu thông tin đại lý: ' + ', '.join(missing))
    return out


def upsert_agent_customer(conn: sqlite3.Connection, contact: dict[str, str]) -> int:
    """Tạo/cập nhật khách hàng đại lý theo MST hoặc tên — trả customer_id."""
    cols = _cols(conn, 'customers')
    if not cols:
        raise ValueError('Chưa có bảng customers')
    tax = contact['tax_code']
    name = contact['name']
    row = None
    if tax:
        row = conn.execute(
            'SELECT id FROM customers WHERE TRIM(COALESCE(tax_code,\'\')) = ? LIMIT 1',
            (tax,),
        ).fetchone()
    if not row:
        row = conn.execute(
            'SELECT id FROM customers WHERE TRIM(COALESCE(name,\'\')) = ? LIMIT 1',
            (name,),
        ).fetchone()
    fields = {
        'name': name,
        'company_name': name,
        'address': contact['address'],
        'tax_code': tax,
        'phone': contact['phone'],
        'email': contact['email'],
    }
    if row:
        cid = int(row[0] if not hasattr(row, 'keys') else row['id'])
        sets = []
        params: list[Any] = []
        for k, v in fields.items():
            if k in cols:
                sets.append(f'{k} = ?')
                params.append(v)
        if sets:
            params.append(cid)
            conn.execute(f"UPDATE customers SET {', '.join(sets)} WHERE id = ?", params)
        return cid
    insert_cols = [k for k in fields if k in cols]
    placeholders = ', '.join('?' * len(insert_cols))
    conn.execute(
        f"INSERT INTO customers ({', '.join(insert_cols)}) VALUES ({placeholders})",
        [fields[k] for k in insert_cols],
    )
    return int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])


def _seller_info(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
        if not row:
            return {}
        d = dict(row)
        return {
            'name': d.get('company_name') or d.get('name') or d.get('business_name') or '',
            'address': d.get('address') or '',
            'tax_code': d.get('tax_code') or d.get('mst') or '',
            'phone': d.get('phone') or d.get('mobile') or '',
            'email': d.get('email') or '',
        }
    except Exception:
        return {}


def build_consignment_email(doc: dict[str, Any], seller: dict[str, str] | None = None) -> tuple[str, str, str]:
    """Trả (subject, text_body, html_body) phiếu xuất gửi đại lý."""
    seller = seller or {}
    doc_no = doc.get('doc_no') or ''
    date_s = doc.get('delivery_date') or ''
    agent = doc.get('agent_name') or ''
    lines = doc.get('lines') or []
    subject = f'Phiếu xuất kho gửi đại lý {doc_no} — {agent}'

    rows_txt = []
    rows_html = []
    for i, ln in enumerate(lines, 1):
        rows_txt.append(
            f"{i}. {ln.get('product_name')} | {ln.get('unit') or ''} | SL {ln.get('quantity')} "
            f"| ĐG gợi ý {ln.get('unit_price') or 0}"
        )
        rows_html.append(
            f"<tr><td>{i}</td><td>{ln.get('product_name') or ''}</td>"
            f"<td>{ln.get('unit') or ''}</td>"
            f"<td style='text-align:right'>{ln.get('quantity')}</td>"
            f"<td style='text-align:right'>{ln.get('unit_price') or 0}</td></tr>"
        )

    text = (
        f"Kính gửi Đại lý {agent},\n\n"
        f"Chúng tôi gửi Phiếu xuất kho hàng gửi đi bán số {doc_no} ngày {date_s}.\n\n"
        f"Bên gửi: {seller.get('name') or ''}\n"
        f"MST: {seller.get('tax_code') or ''}\n"
        f"Địa chỉ: {seller.get('address') or ''}\n"
        f"ĐT: {seller.get('phone') or ''}\n\n"
        f"Bên nhận (đại lý):\n"
        f"Tên: {agent}\n"
        f"MST: {doc.get('agent_tax_code') or ''}\n"
        f"Địa chỉ: {doc.get('agent_address') or ''}\n"
        f"ĐT: {doc.get('agent_phone') or ''}\n"
        f"Email: {doc.get('agent_email') or ''}\n"
        f"Kho xuất: {doc.get('warehouse_code') or ''}\n\n"
        f"Chi tiết hàng:\n" + '\n'.join(rows_txt) + "\n\n"
        f"Ghi chú: {doc.get('notes') or ''}\n\n"
        f"Trân trọng.\n"
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#111">
      <h2 style="margin:0 0 8px">Phiếu xuất kho gửi đại lý</h2>
      <p>Số: <b>{doc_no}</b> · Ngày: <b>{date_s}</b> · Kho: <b>{doc.get('warehouse_code') or ''}</b></p>
      <table style="width:100%;border-collapse:collapse;margin:12px 0">
        <tr>
          <td style="width:50%;vertical-align:top;padding:8px;border:1px solid #ddd">
            <b>Bên gửi</b><br>{seller.get('name') or ''}<br>
            MST: {seller.get('tax_code') or ''}<br>
            {seller.get('address') or ''}<br>
            ĐT: {seller.get('phone') or ''}
          </td>
          <td style="width:50%;vertical-align:top;padding:8px;border:1px solid #ddd">
            <b>Bên nhận (đại lý)</b><br>{agent}<br>
            MST: {doc.get('agent_tax_code') or ''}<br>
            {doc.get('agent_address') or ''}<br>
            ĐT: {doc.get('agent_phone') or ''}<br>
            Email: {doc.get('agent_email') or ''}
          </td>
        </tr>
      </table>
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="background:#f1f5f9">
            <th style="border:1px solid #ccc;padding:6px">#</th>
            <th style="border:1px solid #ccc;padding:6px">Hàng hóa / thành phẩm</th>
            <th style="border:1px solid #ccc;padding:6px">ĐVT</th>
            <th style="border:1px solid #ccc;padding:6px">SL</th>
            <th style="border:1px solid #ccc;padding:6px">Đơn giá gợi ý</th>
          </tr>
        </thead>
        <tbody>{''.join(rows_html)}</tbody>
      </table>
      <p style="margin-top:12px">Ghi chú: {doc.get('notes') or '—'}</p>
      <p style="color:#64748b;font-size:12px">Email tự động từ phần mềm kế toán — hàng gửi đi bán (TK 157).</p>
    </div>
    """
    return subject, text, html


def send_consignment_voucher_email(
    conn: sqlite3.Connection,
    delivery_id: int,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    """Gửi phiếu xuất gửi tới email đại lý."""
    from Services.email_service import send_email, smtp_configured

    doc = get_consignment(conn, delivery_id)
    if not doc:
        raise ValueError('Không tìm thấy phiếu gửi')
    email = (doc.get('agent_email') or '').strip()
    if not _valid_email(email):
        raise ValueError('Phiếu thiếu email đại lý hợp lệ')
    if not smtp_configured():
        raise ValueError('Chưa cấu hình SMTP (APP_PASSWORD / SENDER_EMAIL trong .env)')

    subject, text, html = build_consignment_email(doc, _seller_info(conn))
    ok, err = send_email(email, subject, text, html_body=html)
    when = _now()
    if ok:
        conn.execute(
            'UPDATE sme_agent_deliveries SET email_sent_at = ?, email_error = NULL WHERE id = ?',
            (when, delivery_id),
        )
    else:
        conn.execute(
            'UPDATE sme_agent_deliveries SET email_error = ? WHERE id = ?',
            ((err or 'Gửi email thất bại')[:500], delivery_id),
        )
    if commit:
        conn.commit()
    if not ok:
        raise ValueError(err or 'Gửi email thất bại')
    return {'success': True, 'email': email, 'sent_at': when}


def ship_consignment(
    conn: sqlite3.Connection,
    *,
    agent_name: str,
    delivery_date: str,
    warehouse_code: str,
    items: list[dict],
    notes: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    customer_id: int | None = None,
    agent_address: str = '',
    agent_tax_code: str = '',
    agent_phone: str = '',
    agent_email: str = '',
    send_email_to_agent: bool = True,
    commit: bool = False,
) -> dict[str, Any]:
    """Bước 1: Nợ 157 / Có 155|156 + trừ tồn + (tuỳ chọn) email phiếu XK cho đại lý."""
    from Services.sme.sale_forms import _next_delivery_no
    from Services.sme.branch_filter import stamp_row_branch, warehouse_branch_or_session

    ensure_consignment_schema(conn, commit=False)
    ensure_sme_journal_ready(conn, commit=False)

    # Nếu chọn KH có sẵn mà form thiếu field → bổ sung từ customers
    if customer_id and not all([agent_address, agent_tax_code, agent_phone, agent_email]):
        crow = conn.execute(
            'SELECT name, address, tax_code, phone, email FROM customers WHERE id = ?',
            (int(customer_id),),
        ).fetchone()
        if crow:
            cd = dict(crow)
            agent_name = agent_name or cd.get('name') or ''
            agent_address = agent_address or cd.get('address') or ''
            agent_tax_code = agent_tax_code or cd.get('tax_code') or ''
            agent_phone = agent_phone or cd.get('phone') or ''
            agent_email = agent_email or cd.get('email') or ''

    contact = normalize_agent_contact(
        agent_name=agent_name,
        agent_address=agent_address,
        agent_tax_code=agent_tax_code,
        agent_phone=agent_phone,
        agent_email=agent_email,
    )
    agent = contact['name']
    date_s = str(delivery_date or '')[:10]
    wh = (warehouse_code or '').strip()
    if not date_s:
        raise ValueError('Thiếu ngày giao')
    if not wh:
        raise ValueError('Chọn kho xuất gửi')
    if not items:
        raise ValueError('Không có dòng hàng')

    # Luôn đồng bộ hồ sơ KH đại lý (tên/MST/ĐT/email/địa chỉ) để lần sau tự điền
    synced_id = upsert_agent_customer(conn, contact)
    cid = int(customer_id) if customer_id else synced_id

    prepared: list[dict] = []
    for raw in items:
        pid = int(raw.get('product_id') or 0)
        qty = _money(raw.get('quantity'))
        if pid <= 0 or qty <= 0:
            continue
        avail = _wh_qty(conn, pid, wh)
        if float(qty) > avail + 1e-9:
            meta = _product_meta(conn, pid)
            raise ValueError(
                f'Không đủ tồn «{meta["name"]}» tại {wh}: còn {avail:g}, cần {float(qty):g}'
            )
        meta = _product_meta(conn, pid)
        if not _is_shippable_consign_code(meta.get('product_code')):
            raise ValueError(
                f'«{meta["name"]}» không phải hàng hóa (SP) hoặc thành phẩm (TP) — không xuất gửi'
            )
        cost = _money(raw.get('unit_cost') if raw.get('unit_cost') is not None else _avg_cost(conn, pid))
        prepared.append({
            'product_id': pid,
            'product_code': meta.get('product_code') or meta.get('barcode') or '',
            'product_name': meta.get('name') or '',
            'unit': meta.get('unit') or 'Cái',
            'product_type': meta.get('product_type') or 'goods',
            'quantity': qty,
            'unit_cost': cost,
            'amount': (qty * cost).quantize(MONEY_Q),
            'unit_price': _money(raw.get('unit_price') or raw.get('price') or 0),
        })
    if not prepared:
        raise ValueError('Không có dòng hợp lệ')

    doc_no = _next_delivery_no(conn)
    when = _now()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_agent_deliveries
            (form_code, doc_no, delivery_date, agent_name, notes, status,
             created_by, created_at, warehouse_code, customer_id, total_cost,
             agent_address, agent_tax_code, agent_phone, agent_email)
        VALUES ('01-BH', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_no, date_s, agent, notes or '', STATUS_SHIPPED,
            created_by, when, wh, cid,
            float(sum(p['amount'] for p in prepared)),
            contact['address'], contact['tax_code'], contact['phone'], contact['email'],
        ),
    )
    did = int(cur.lastrowid)
    stamp_row_branch(conn, 'sme_agent_deliveries', did, branch_code=branch_code)

    consign = resolve_postable_account(conn, 'inv.consignment')
    j_lines: list[dict] = []
    seq = 1
    for p in prepared:
        cur.execute(
            """
            INSERT INTO sme_agent_delivery_lines
                (delivery_id, product_id, product_code, product_name, unit, quantity,
                 unit_price, unit_cost, amount, product_type, qty_sold, qty_returned, qty_damaged)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0)
            """,
            (
                did, p['product_id'], p['product_code'], p['product_name'], p['unit'],
                float(p['quantity']), float(p['unit_price']), float(p['unit_cost']),
                float(p['amount']), p['product_type'],
            ),
        )
        _insert_stock_export(
            conn,
            product_id=p['product_id'],
            qty=float(p['quantity']),
            cost=float(p['unit_cost']),
            doc_id=did,
            doc_no=doc_no,
            warehouse=wh,
            when=when,
            note=f'Gửi đại lý {agent} — {doc_no}',
        )
        inv_acct = resolve_postable_account(conn, _inv_role(p['product_type']))
        amt = float(p['amount'])
        j_lines.extend([
            {
                'sequence': seq,
                'account_code': consign,
                'debit': amt,
                'credit': 0,
                'description': f'Hàng gửi đi bán {doc_no} — {p["product_name"]}',
            },
            {
                'sequence': seq + 1,
                'account_code': inv_acct,
                'debit': 0,
                'credit': amt,
                'description': f'Xuất kho gửi đại lý {doc_no} — {p["product_name"]}',
            },
        ])
        seq += 2

    branch = branch_code or warehouse_branch_or_session(conn, wh)
    posted = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type=DOC_SHIP,
        document_no=doc_no,
        document_id=did,
        business_type='HANG_GUI_DI_BAN',
        currency='VND',
        exchange_rate=1,
        description=f'Xuất gửi đại lý {agent} — {doc_no}',
        reference_document=doc_no,
        created_by=created_by,
        branch_code=branch,
        lines=j_lines,
    )
    conn.execute(
        'UPDATE sme_agent_deliveries SET journal_ship_id = ? WHERE id = ?',
        (posted['id'], did),
    )

    email_info = None
    email_err = None
    if send_email_to_agent:
        try:
            email_info = send_consignment_voucher_email(conn, did, commit=False)
        except Exception as exc:
            email_err = str(exc)
            conn.execute(
                'UPDATE sme_agent_deliveries SET email_error = ? WHERE id = ?',
                (email_err[:500], did),
            )

    if commit:
        conn.commit()
    out = get_consignment(conn, did)
    out['email_sent'] = bool(email_info)
    out['email_error'] = email_err
    if email_info:
        out['email_to'] = email_info.get('email')
    return out


def confirm_consignment_sale(
    conn: sqlite3.Connection,
    delivery_id: int,
    *,
    event_date: str,
    lines: list[dict],
    payment_method: str = '131',
    tax_pct: float = 10,
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Bước 2: GV Nợ 632/Có 157 + DT Nợ 111|112|131 / Có 511|3331."""
    ensure_consignment_schema(conn, commit=False)
    ensure_sme_journal_ready(conn, commit=False)
    doc = get_consignment(conn, delivery_id)
    if not doc:
        raise ValueError('Không tìm thấy phiếu gửi')
    if doc.get('status') == STATUS_VOID:
        raise ValueError('Phiếu đã hủy')

    date_s = str(event_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày xác nhận bán')

    by_id = {int(ln['id']): ln for ln in doc['lines']}
    prepared: list[dict] = []
    for raw in lines:
        lid = int(raw.get('delivery_line_id') or raw.get('line_id') or 0)
        qty = _money(raw.get('quantity'))
        if lid <= 0 or qty <= 0:
            continue
        ln = by_id.get(lid)
        if not ln:
            raise ValueError(f'Không có dòng #{lid}')
        open_qty = _f(ln.get('qty_open'))
        if float(qty) > open_qty + 1e-9:
            raise ValueError(
                f'«{ln.get("product_name")}» chỉ còn {open_qty:g} trên 157, không bán được {float(qty):g}'
            )
        price = _money(raw.get('unit_price') if raw.get('unit_price') is not None else ln.get('unit_price'))
        tp = float(raw.get('tax_pct') if raw.get('tax_pct') is not None else tax_pct)
        prepared.append({
            'line': ln,
            'quantity': qty,
            'unit_cost': _money(ln.get('unit_cost')),
            'unit_price': price,
            'tax_pct': tp,
            'cogs_amt': (qty * _money(ln.get('unit_cost'))).quantize(MONEY_Q),
            'revenue_gross': (qty * price).quantize(MONEY_Q),
        })
    if not prepared:
        raise ValueError('Chọn ít nhất một dòng đã bán')

    consign = resolve_postable_account(conn, 'inv.consignment')
    cogs_lines: list[dict] = []
    seq = 1
    total_cogs = Decimal('0.00')
    for p in prepared:
        pt = p['line'].get('product_type') or 'goods'
        cogs_acct = resolve_postable_account(conn, _cogs_role(pt))
        amt = float(p['cogs_amt'])
        total_cogs += p['cogs_amt']
        cogs_lines.extend([
            {
                'sequence': seq,
                'account_code': cogs_acct,
                'debit': amt,
                'credit': 0,
                'description': f'GV hàng gửi đã bán — {doc["doc_no"]}',
            },
            {
                'sequence': seq + 1,
                'account_code': consign,
                'debit': 0,
                'credit': amt,
                'description': f'Tất toán 157 — {p["line"].get("product_name")}',
            },
        ])
        seq += 2

    pay_acct = _payment_debit(conn, payment_method)
    vat_acct = _vat_out(conn)
    # Doanh thu: gom theo loại SP (5111 HH / 5112 TP)
    rev_groups: dict[str, Decimal] = {}
    vat_total = Decimal('0.00')
    gross_total = Decimal('0.00')
    for p in prepared:
        pt = p['line'].get('product_type') or 'goods'
        role = _revenue_role(pt)
        gross = p['revenue_gross']
        vat = (gross * _money(p['tax_pct']) / Decimal('100')).quantize(MONEY_Q)
        net = gross - vat
        rev_groups[role] = rev_groups.get(role, Decimal('0.00')) + net
        vat_total += vat
        gross_total += gross

    if gross_total <= 0:
        raise ValueError('Tổng giá bán phải > 0 khi xác nhận đã bán')

    rev_lines: list[dict] = [{
        'sequence': 1,
        'account_code': pay_acct,
        'debit': float(gross_total),
        'credit': 0,
        'description': f'Thu/phải thu đại lý — {doc["doc_no"]}',
    }]
    seq = 2
    for role, net in rev_groups.items():
        if net <= 0:
            continue
        rev_lines.append({
            'sequence': seq,
            'account_code': resolve_postable_account(conn, role),
            'debit': 0,
            'credit': float(net),
            'description': f'Doanh thu hàng gửi — {doc["doc_no"]}',
        })
        seq += 1
    if vat_total > 0:
        rev_lines.append({
            'sequence': seq,
            'account_code': vat_acct,
            'debit': 0,
            'credit': float(vat_total),
            'description': f'VAT đầu ra — {doc["doc_no"]}',
        })

    branch = doc.get('branch_code')
    cogs_je = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type=DOC_SALE_COGS,
        document_no=doc['doc_no'],
        document_id=delivery_id,
        business_type='HANG_GUI_BAN_GV',
        currency='VND',
        exchange_rate=1,
        description=f'Giá vốn hàng gửi đã bán — {doc["doc_no"]}',
        reference_document=doc['doc_no'],
        created_by=created_by,
        branch_code=branch,
        lines=cogs_lines,
    )
    rev_je = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type=DOC_SALE_REV,
        document_no=doc['doc_no'],
        document_id=delivery_id,
        business_type='HANG_GUI_BAN_DT',
        currency='VND',
        exchange_rate=1,
        description=f'Doanh thu hàng gửi đã bán — {doc["doc_no"]}',
        reference_document=doc['doc_no'],
        created_by=created_by,
        branch_code=branch,
        lines=rev_lines,
    )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_consign_events
            (delivery_id, event_type, event_date, payment_method,
             journal_cogs_id, journal_rev_id, notes, created_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            delivery_id, 'sale', date_s, payment_method,
            cogs_je['id'], rev_je['id'], notes or '', created_by, _now(),
        ),
    )
    eid = int(cur.lastrowid)
    for p in prepared:
        cur.execute(
            """
            INSERT INTO sme_consign_event_lines
                (event_id, delivery_line_id, product_id, quantity, unit_cost, unit_price, tax_pct, is_damaged)
            VALUES (?,?,?,?,?,?,?,0)
            """,
            (
                eid, p['line']['id'], p['line'].get('product_id'),
                float(p['quantity']), float(p['unit_cost']), float(p['unit_price']), float(p['tax_pct']),
            ),
        )
        cur.execute(
            """
            UPDATE sme_agent_delivery_lines
            SET qty_sold = COALESCE(qty_sold, 0) + ?
            WHERE id = ?
            """,
            (float(p['quantity']), p['line']['id']),
        )

    if commit:
        conn.commit()
    out = get_consignment(conn, delivery_id)
    out['last_event'] = {
        'type': 'sale',
        'cogs_entry_id': cogs_je['id'],
        'revenue_entry_id': rev_je['id'],
        'cogs_vnd': float(total_cogs),
        'revenue_vnd': float(gross_total),
        'vat_vnd': float(vat_total),
    }
    return out


def return_consignment(
    conn: sqlite3.Connection,
    delivery_id: int,
    *,
    event_date: str,
    lines: list[dict],
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Bước 3: trả lại — còn tốt Nợ 155|156; hỏng Nợ 632 / Có 157."""
    ensure_consignment_schema(conn, commit=False)
    ensure_sme_journal_ready(conn, commit=False)
    doc = get_consignment(conn, delivery_id)
    if not doc:
        raise ValueError('Không tìm thấy phiếu gửi')
    if doc.get('status') == STATUS_VOID:
        raise ValueError('Phiếu đã hủy')

    date_s = str(event_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày trả lại')
    wh = (doc.get('warehouse_code') or '').strip()
    by_id = {int(ln['id']): ln for ln in doc['lines']}
    prepared: list[dict] = []
    for raw in lines:
        lid = int(raw.get('delivery_line_id') or raw.get('line_id') or 0)
        qty = _money(raw.get('quantity'))
        damaged = 1 if str(raw.get('is_damaged') or raw.get('damaged') or '').lower() in ('1', 'true', 'yes') else 0
        if lid <= 0 or qty <= 0:
            continue
        ln = by_id.get(lid)
        if not ln:
            raise ValueError(f'Không có dòng #{lid}')
        open_qty = _f(ln.get('qty_open'))
        if float(qty) > open_qty + 1e-9:
            raise ValueError(
                f'«{ln.get("product_name")}» chỉ còn {open_qty:g} trên 157'
            )
        prepared.append({
            'line': ln,
            'quantity': qty,
            'unit_cost': _money(ln.get('unit_cost')),
            'amount': (qty * _money(ln.get('unit_cost'))).quantize(MONEY_Q),
            'is_damaged': damaged,
        })
    if not prepared:
        raise ValueError('Chọn dòng trả lại')

    consign = resolve_postable_account(conn, 'inv.consignment')
    spoil = resolve_postable_account(conn, cogs_spoilage_account())
    j_lines: list[dict] = []
    seq = 1
    when = _now()
    for p in prepared:
        pt = p['line'].get('product_type') or 'goods'
        amt = float(p['amount'])
        if p['is_damaged']:
            debit_acct = spoil
            desc = f'Hỏng hàng gửi — {p["line"].get("product_name")}'
        else:
            debit_acct = resolve_postable_account(conn, _inv_role(pt))
            desc = f'Nhập lại kho từ đại lý — {p["line"].get("product_name")}'
            if wh and p['line'].get('product_id'):
                _insert_stock_import(
                    conn,
                    product_id=int(p['line']['product_id']),
                    qty=float(p['quantity']),
                    cost=float(p['unit_cost']),
                    doc_id=delivery_id,
                    doc_no=doc['doc_no'],
                    warehouse=wh,
                    when=when,
                    note=f'Trả hàng gửi {doc["doc_no"]}',
                )
        j_lines.extend([
            {
                'sequence': seq,
                'account_code': debit_acct,
                'debit': amt,
                'credit': 0,
                'description': desc,
            },
            {
                'sequence': seq + 1,
                'account_code': consign,
                'debit': 0,
                'credit': amt,
                'description': f'Tất toán 157 trả hàng — {doc["doc_no"]}',
            },
        ])
        seq += 2

    ret_je = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type=DOC_RETURN,
        document_no=doc['doc_no'],
        document_id=delivery_id,
        business_type='HANG_GUI_TRA_LAI',
        currency='VND',
        exchange_rate=1,
        description=f'Trả hàng gửi đi bán — {doc["doc_no"]}',
        reference_document=doc['doc_no'],
        created_by=created_by,
        branch_code=doc.get('branch_code'),
        lines=j_lines,
    )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_consign_events
            (delivery_id, event_type, event_date, journal_return_id, notes, created_by, created_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (delivery_id, 'return', date_s, ret_je['id'], notes or '', created_by, when),
    )
    eid = int(cur.lastrowid)
    for p in prepared:
        cur.execute(
            """
            INSERT INTO sme_consign_event_lines
                (event_id, delivery_line_id, product_id, quantity, unit_cost, unit_price, tax_pct, is_damaged)
            VALUES (?,?,?,?,?,?,0,?)
            """,
            (
                eid, p['line']['id'], p['line'].get('product_id'),
                float(p['quantity']), float(p['unit_cost']), 0, p['is_damaged'],
            ),
        )
        if p['is_damaged']:
            cur.execute(
                'UPDATE sme_agent_delivery_lines SET qty_damaged = COALESCE(qty_damaged,0)+? WHERE id=?',
                (float(p['quantity']), p['line']['id']),
            )
        else:
            cur.execute(
                'UPDATE sme_agent_delivery_lines SET qty_returned = COALESCE(qty_returned,0)+? WHERE id=?',
                (float(p['quantity']), p['line']['id']),
            )

    if commit:
        conn.commit()
    out = get_consignment(conn, delivery_id)
    out['last_event'] = {'type': 'return', 'journal_entry_id': ret_je['id']}
    return out


def void_consignment(
    conn: sqlite3.Connection,
    delivery_id: int,
    *,
    reason: str = 'Hủy phiếu gửi đại lý',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Hủy phiếu gửi — chỉ khi chưa bán / chưa trả."""
    from Services.sme.branch_filter import assert_row_in_branch

    ensure_consignment_schema(conn, commit=False)
    assert_row_in_branch(conn, 'sme_agent_deliveries', delivery_id, label='Phiếu gửi đại lý')
    doc = get_consignment(conn, delivery_id)
    if not doc:
        raise ValueError('Không tìm thấy phiếu gửi')
    if doc.get('status') == STATUS_VOID:
        raise ValueError('Đã hủy')
    for ln in doc.get('lines') or []:
        if _f(ln.get('qty_sold')) > 0 or _f(ln.get('qty_returned')) > 0 or _f(ln.get('qty_damaged')) > 0:
            raise ValueError('Đã có bán / trả lại — không hủy được phiếu gửi. Dùng chức năng trả lại.')

    # Đảo stock
    moves = conn.execute(
        "SELECT * FROM stock_moves WHERE ref_type = 'consign_ship' AND ref_id = ?",
        (delivery_id,),
    ).fetchall()
    when = _now()
    wh = (doc.get('warehouse_code') or '').strip()
    for m in moves:
        md = dict(m)
        qty = abs(float(md.get('quantity') or 0))
        if qty <= 0:
            continue
        pid = int(md.get('product_id') or 0)
        cost = float(md.get('cost_price') or md.get('avg_cost') or 0)
        _insert_stock_import(
            conn,
            product_id=pid,
            qty=qty,
            cost=cost,
            doc_id=delivery_id,
            doc_no=doc['doc_no'],
            warehouse=wh or (md.get('warehouse_code') or ''),
            when=when,
            note=f'Hủy gửi {doc["doc_no"]}',
        )

    ship_jid = doc.get('journal_ship_id')
    if ship_jid:
        reverse_journal_entry(
            conn, int(ship_jid),
            posting_date=str(doc.get('delivery_date') or '')[:10] or None,
            created_by=created_by,
            reason=reason,
        )

    conn.execute(
        "UPDATE sme_agent_deliveries SET status = 'void', notes = ? WHERE id = ?",
        (((doc.get('notes') or '') + f' | {reason}').strip(' |'), delivery_id),
    )
    if commit:
        conn.commit()
    return get_consignment(conn, delivery_id)
