"""Đồng bộ khách trả hàng → nhật ký SME (THB: đảo doanh thu/VAT + nhập lại GV)."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from Services.sme.journal_engine import post_journal_entry, reverse_journal_entry

RETURN_SALE_DOCUMENT_TYPE = 'THB'


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _active_thb(conn: sqlite3.Connection, document_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE document_id = ? AND document_type = ?
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id
        """,
        (document_id, RETURN_SALE_DOCUMENT_TYPE),
    ).fetchall()
    return [int(r[0] if not isinstance(r, sqlite3.Row) else r['id']) for r in rows]


def reverse_return_sale_journals(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    created_by: str | None = None,
    reason: str = 'Hủy bút toán khách trả hàng',
) -> list[int]:
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    ensure_sme_accounting_ready(conn, commit=False)
    reversed_ids: list[int] = []
    for entry_id in _active_thb(conn, document_id):
        rev = reverse_journal_entry(
            conn, entry_id, created_by=created_by, reason=reason,
        )
        reversed_ids.append(int(rev['id']))
    return reversed_ids


def sync_return_sale_journals(
    conn: sqlite3.Connection,
    return_id: int,
    *,
    sale_id: int | None = None,
    product_id: int | None = None,
    quantity: float | None = None,
    unit_price: float | None = None,
    tax_pct: float | None = None,
    cost_price: float | None = None,
    posting_date: str | None = None,
    sale_no: str | None = None,
    customer_name: str | None = None,
    created_by: str | None = None,
    replace_existing: bool = True,
    warehouse_code: str | None = None,
) -> dict[str, Any]:
    """
    Hạch toán khách trả:
      Nợ 511 / Có 111|131 (hoàn tiền/công nợ) theo giá bán chưa VAT
      Nợ 3331 / Có 111|131 (VAT nếu có) — gộp vào hoàn tiền
      Nợ 156|152 / Có 632 (nhập lại giá vốn)
    document_id = return_sales.id
    """
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.branch_filter import warehouse_branch_or_session

    ensure_sme_accounting_ready(conn, commit=False)
    reversed_ids: list[int] = []
    if replace_existing:
        reversed_ids = reverse_return_sale_journals(
            conn, return_id, created_by=created_by,
        )

    # Enrich from DB if missing
    row = conn.execute(
        'SELECT * FROM return_sales WHERE id = ?', (return_id,)
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy phiếu khách trả')
    doc = dict(row)
    sid = int(sale_id or doc.get('sale_id') or 0)
    pid = int(product_id or doc.get('product_id') or 0)
    qty = float(quantity if quantity is not None else (doc.get('quantity') or 0))
    date_s = (posting_date or str(doc.get('date') or ''))[:10]
    if not date_s or qty <= 0 or not pid:
        return {
            'posted': False,
            'reason': 'insufficient_data',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    price = float(unit_price or 0)
    tax = float(tax_pct or 0)
    cost = float(cost_price or 0)
    sno = sale_no or ''
    cust = customer_name or ''

    if sid and (not price or not sno):
        sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sid,)).fetchone()
        if sale:
            sd = dict(sale)
            sno = sno or str(sd.get('sale_no') or '')
            cust = cust or str(sd.get('customer_name') or '')
        item = conn.execute(
            """
            SELECT price, discount_pct, tax_pct, cost_price
            FROM sale_items WHERE sale_id = ? AND product_id = ? LIMIT 1
            """,
            (sid, pid),
        ).fetchone()
        if item:
            it = dict(item)
            if not price:
                disc = float(it.get('discount_pct') or 0)
                price = float(it.get('price') or 0) * (1 - disc / 100.0)
            if not tax:
                tax = float(it.get('tax_pct') or 0)
            if not cost:
                cost = float(it.get('cost_price') or 0)

    net = _money(qty * price)
    vat = _money(net * Decimal(str(tax)) / Decimal('100'))
    cogs = _money(qty * cost)
    refund = net + vat

    inv_acc = '156'
    product_type = 'goods'
    try:
        pt = conn.execute(
            'SELECT COALESCE(product_type, ?) FROM products WHERE id = ?',
            ('goods', pid),
        ).fetchone()
        if pt:
            product_type = str(pt[0] or 'goods')
            if product_type.lower() in ('materials', 'material', 'nvl', 'raw_materials'):
                inv_acc = '152'
            elif product_type.lower() in ('finished_goods', 'finished', 'thanh_pham', 'ready_made'):
                inv_acc = '155'
    except sqlite3.Error:
        pass

    from Services.sme.cogs_accounts import cogs_accounts_for_line
    cogs_acc, inv_from_map, _ = cogs_accounts_for_line(
        product_type, channel='domestic',
    )
    if inv_from_map:
        inv_acc = inv_from_map

    desc = f'Khách trả hàng {sno or ("#" + str(sid))} — {cust}'.strip()
    lines: list[dict] = []
    seq = 1
    if refund > 0:
        # Hoàn tiền mặt mặc định (đồng bộ phiếu chi HKD 511/111)
        lines.append({
            'sequence': seq, 'account_code': '511',
            'debit': float(net), 'credit': 0, 'description': desc,
        })
        seq += 1
        if vat > 0:
            lines.append({
                'sequence': seq, 'account_code': '3331',
                'debit': float(vat), 'credit': 0, 'description': desc,
            })
            seq += 1
        lines.append({
            'sequence': seq, 'account_code': '111',
            'debit': 0, 'credit': float(refund), 'description': desc,
        })
        seq += 1
    if cogs > 0:
        lines.append({
            'sequence': seq, 'account_code': inv_acc,
            'debit': float(cogs), 'credit': 0, 'description': f'Nhập lại GV — {desc}',
        })
        seq += 1
        lines.append({
            'sequence': seq, 'account_code': cogs_acc,
            'debit': 0, 'credit': float(cogs), 'description': f'Nhập lại GV — {desc}',
        })

    if not lines:
        return {
            'posted': False,
            'reason': 'zero_amount',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    branch = warehouse_branch_or_session(conn, warehouse_code)
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type=RETURN_SALE_DOCUMENT_TYPE,
        document_no=f'THB{return_id:06d}',
        document_id=return_id,
        business_type='KHACH_TRA_HANG',
        description=desc,
        reference_document=sno,
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )
    return {
        'posted': True,
        'entry_ids': [entry['id']],
        'reversed_entry_ids': reversed_ids,
    }
