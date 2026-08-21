"""Lập phiếu xuất khẩu 2 bước: xuất kho ra cảng (157) → thông quan (632/157 + DT)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.export_clearance import (
    DOC_TYPES_ALL,
    DOC_TYPES_CLEARANCE,
    DOC_TYPES_SHIP,
    EXPORT_STATUS_CLEARED,
    EXPORT_STATUS_SHIPPED,
    STOCK_TYPE_EXPORT_SHIP,
    active_entries,
    export_status_of,
    reverse_export_journals,
    sync_export_clearance_journals,
    sync_export_ship_journals,
)
from Services.sme.export_payment import (
    PAYMENT_DOC_DISCOUNT,
    PAYMENT_LC,
    PAYMENT_LC_USANCE,
    PAYMENT_PREPAID_FULL,
    PAYMENT_PREPAID_PARTIAL,
    PAYMENT_UNPAID,
    REVENUE_ACCOUNT_DEFAULT,
    build_advance_payloads_from_request,
    compute_split_fx_revenue_vnd,
    ensure_export_sale_schema,
    list_sale_advances,
    normalize_payment_mode,
    payment_status_label,
    replace_sale_advances,
    validate_export_payment,
)
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    resolve_postable_account,
)

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _fx(val) -> Decimal:
    from Services.sme.export_payment import _fx as fx
    return fx(val)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _next_sale_no(conn: sqlite3.Connection, prefix: str = 'XK') -> str:
    row = conn.execute(
        """
        SELECT sale_no FROM sale
        WHERE sale_no LIKE ?
        ORDER BY id DESC LIMIT 1
        """,
        (f'{prefix}%',),
    ).fetchone()
    n = 1
    if row and row[0]:
        digits = ''.join(ch for ch in str(row[0]) if ch.isdigit())
        if digits:
            n = int(digits) + 1
    return f'{prefix}{n:06d}'


def _active_export_entries(conn: sqlite3.Connection, sale_id: int) -> list[int]:
    return active_entries(conn, sale_id, DOC_TYPES_ALL)


def sync_export_sale_journals(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    created_by: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Chỉ bước 1 (xuất kho ra cảng). Thông quan: confirm_export_clearance."""
    return sync_export_ship_journals(
        conn, sale_id, created_by=created_by, replace_existing=replace_existing,
    )


def _insert_sale_stock_move(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    sale_id: int,
    sale_date: str,
    qty: float,
    cost: float,
    sale_no: str,
    warehouse_code: str | None,
    unit: str,
) -> None:
    sm_cols = _cols(conn, 'stock_moves')
    if not sm_cols:
        return
    fields = [
        'product_id', 'date', 'type', 'ref_id', 'quantity', 'note',
        'ref_document', 'ref_type', 'type1', 'unit',
    ]
    values: list[Any] = [
        product_id, sale_date, STOCK_TYPE_EXPORT_SHIP, sale_id, -abs(float(qty)),
        f'Xuất kho ra cảng (chờ thông quan) — {sale_no}',
        sale_no, 'export_ship', 'Xuất nội bộ', unit or 'Cái',
    ]
    if 'cost_price' in sm_cols:
        idx = fields.index('quantity') + 1
        fields.insert(idx, 'cost_price')
        values.insert(idx, float(cost))
    elif 'avg_cost' in sm_cols:
        idx = fields.index('quantity') + 1
        fields.insert(idx, 'avg_cost')
        values.insert(idx, float(cost))
    if 'in_quantity' in sm_cols and 'out_quantity' in sm_cols:
        fields.extend(['in_quantity', 'out_quantity'])
        values.extend([0.0, abs(float(qty))])
    if 'warehouse_code' in sm_cols and warehouse_code:
        fields.append('warehouse_code')
        values.append(warehouse_code)
    paired = [(f, v) for f, v in zip(fields, values) if f in sm_cols]
    if not paired:
        return
    fields2, values2 = zip(*paired)
    conn.execute(
        f"INSERT INTO stock_moves ({', '.join(fields2)}) VALUES ({', '.join(['?'] * len(values2))})",
        list(values2),
    )


def confirm_export_clearance(
    conn: sqlite3.Connection,
    sale_id: int,
    data: dict[str, Any] | None = None,
    *,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Xác nhận thông quan + B/L → GV 632/157 + DT 131/511/3333."""
    ensure_export_sale_schema(conn, commit=False)
    data = data or {}
    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError(f'Không tìm thấy phiếu bán #{sale_id}')
    s = dict(sale)
    if str(s.get('sale_type') or '').upper() != 'EXPORT':
        raise ValueError('Không phải phiếu xuất khẩu')

    scols = _cols(conn, 'sale')
    updates, vals = [], []
    field_map = {
        'customs_decl_no': (data.get('customs_decl_no') or '').strip() or None,
        'bl_no': (data.get('bl_no') or '').strip() or None,
        'risk_transfer_date': str(
            data.get('risk_transfer_date') or data.get('clearance_date') or ''
        )[:10] or None,
        'customs_fx_rate': data.get('customs_fx_rate'),
        'exchange_rate': data.get('exchange_rate') or data.get('customs_fx_rate'),
        'export_tax_fc': data.get('export_tax_fc'),
        'export_tax_vnd': data.get('export_tax_vnd'),
        'incoterms': (data.get('incoterms') or '').strip() or None,
    }
    for col, val in field_map.items():
        if col in scols and val not in (None, ''):
            updates.append(f'{col} = ?')
            vals.append(val)
    if updates:
        vals.append(sale_id)
        conn.execute(f"UPDATE sale SET {', '.join(updates)} WHERE id = ?", vals)

    s = dict(conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone())
    if not str(s.get('customs_decl_no') or '').strip():
        raise ValueError('Thiếu số tờ khai hải quan đã thông quan')
    if not str(s.get('bl_no') or '').strip():
        raise ValueError('Thiếu số Bill of Lading (B/L)')
    if not str(s.get('risk_transfer_date') or '').strip() and 'risk_transfer_date' in scols:
        conn.execute(
            'UPDATE sale SET risk_transfer_date = ? WHERE id = ?',
            (str(s.get('date') or '')[:10], sale_id),
        )

    journal = sync_export_clearance_journals(
        conn, sale_id,
        created_by=created_by,
        replace_existing=bool(data.get('replace_existing')),
    )

    split = journal.get('split') or {}
    remain_vnd = _money(split.get('remain_vnd') or 0)
    mode = normalize_payment_mode(s.get('payment_mode'), sale_type='EXPORT')
    if remain_vnd > 0 and mode != PAYMENT_PREPAID_FULL:
        try:
            conn.execute('DELETE FROM cong_no WHERE sale_id = ?', (sale_id,))
            conn.execute(
                """
                INSERT INTO cong_no
                (customer_name, company_name, address, tax_code, debit_account, credit_account,
                 date_of_debt, unpaid_amount, sale_id, sale_no)
                VALUES (?,?,?,?, '131', '5111', ?, ?, ?, ?)
                """,
                (
                    s.get('customer_name') or '',
                    s.get('company_name') or s.get('customer_name') or '',
                    s.get('address') or '', s.get('tax_code') or '',
                    str(s.get('risk_transfer_date') or s.get('date') or '')[:10],
                    float(remain_vnd), sale_id, s.get('sale_no'),
                ),
            )
        except sqlite3.OperationalError:
            pass

    if commit:
        conn.commit()

    out = get_export_sale(conn, sale_id) or {'id': sale_id}
    return {
        'success': True,
        'sale_id': sale_id,
        'sale_no': s.get('sale_no'),
        'export_status': EXPORT_STATUS_CLEARED,
        'journal': journal,
        'data': out,
        'message': 'Đã thông quan: Nợ 632/Có 157 và Nợ 131/Có 511. Có thể xuất HĐĐT trong 01 ngày làm việc.',
    }



def create_or_update_export_sale(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Tạo/cập nhật phiếu xuất kho ra cảng (chờ thông quan) + Nợ 157/Có kho."""
    from Services.inventory_stock_helpers import apply_wac_outbound
    from Services.sme.inventory_ops import sync_inventory_quantity_from_moves

    ensure_export_sale_schema(conn, commit=False)
    ensure_sme_journal_ready(conn, commit=False)

    edit_id = data.get('sale_id') or data.get('edit_id') or data.get('id')
    try:
        edit_id = int(edit_id) if edit_id not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        edit_id = None

    if edit_id:
        old_chk = conn.execute('SELECT * FROM sale WHERE id = ?', (edit_id,)).fetchone()
        if old_chk and export_status_of(dict(old_chk)) == EXPORT_STATUS_CLEARED:
            raise ValueError(
                'Phiếu đã thông quan — không sửa xuất kho. '
                'Dùng thay thế HĐĐT nếu cần điều chỉnh doanh thu.'
            )
        if old_chk and active_entries(conn, edit_id, DOC_TYPES_CLEARANCE):
            raise ValueError(
                'Phiếu đã có bút toán thông quan (DT/GV) — không sửa xuất kho ra cảng.'
            )
    items = data.get('items') or []
    if not items:
        raise ValueError('Vui lòng nhập ít nhất một mặt hàng')

    customer_name = (data.get('customer_name') or '').strip()
    if not customer_name:
        raise ValueError('Thiếu tên khách hàng nước ngoài')

    sale_date = str(data.get('date') or '')[:10]
    risk_date = str(data.get('risk_transfer_date') or sale_date or '')[:10]
    if not sale_date:
        raise ValueError('Thiếu ngày chứng từ')
    if not risk_date:
        risk_date = sale_date

    currency = (data.get('currency') or 'USD').strip().upper() or 'USD'
    revenue_rate = _fx(data.get('exchange_rate') or data.get('revenue_fx_rate') or 1)
    customs_rate = _fx(data.get('customs_fx_rate') or revenue_rate)
    payment_mode = normalize_payment_mode(
        data.get('payment_mode'), sale_type='EXPORT',
    )
    warehouse_code = (data.get('warehouse_code') or '').strip() or None

    linked_lc_id = data.get('linked_lc_id') or data.get('lc_id')
    try:
        linked_lc_id = int(linked_lc_id) if linked_lc_id not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        linked_lc_id = None

    advance_payloads = build_advance_payloads_from_request(
        conn, data, exchange_rate=revenue_rate, exclude_sale_id=edit_id,
    )

    # Tính tổng FC / dòng
    normalized_items = []
    total_fc = Decimal('0.00')
    for it in items:
        pid = it.get('product_id')
        try:
            pid = int(pid) if pid not in (None, '', 0, '0') else None
        except (TypeError, ValueError):
            pid = None
        qty = _money(it.get('qty') or it.get('quantity') or 0)
        price_fc = _money(it.get('price') or it.get('buyprice') or 0)
        disc = Decimal(str(it.get('discount_pct') or 0))
        line_fc = _money(qty * price_fc)
        line_fc = _money(line_fc - _money(line_fc * (disc / Decimal('100'))))
        if qty <= 0 or line_fc < 0:
            continue
        if not pid:
            raise ValueError(f'Thiếu mã hàng: {it.get("name") or it.get("product_name") or "?"}')
        total_fc += line_fc
        normalized_items.append({
            'product_id': pid,
            'product_name': (it.get('name') or it.get('product_name') or '').strip(),
            'unit': it.get('unit') or 'Cái',
            'qty': float(qty),
            'price': float(price_fc),
            'discount_pct': float(disc),
            'line_fc': float(line_fc),
            'line_type': it.get('line_type') or 'goods',
            'warehouse_code': (it.get('warehouse_code') or warehouse_code or '').strip() or None,
            'tax_pct': 0,  # XK VAT 0%
        })
    if not normalized_items:
        raise ValueError('Không có dòng hàng hợp lệ')
    if total_fc <= 0:
        raise ValueError('Tổng giá trị NT phải > 0')

    # Re-validate với total_fc thật
    validate_export_payment(
        payment_mode=payment_mode,
        total_fc=total_fc,
        advances=advance_payloads,
        lc_id=linked_lc_id,
    )

    if linked_lc_id and payment_mode in (PAYMENT_LC, PAYMENT_LC_USANCE):
        from Services.sme.letter_of_credit import get_lc, get_lc_balance
        lc_doc = get_lc(conn, linked_lc_id)
        if not lc_doc or lc_doc.get('status') != 'open':
            raise ValueError('L/C không tồn tại hoặc không còn hiệu lực')
        direction = (lc_doc.get('direction') or 'export').lower()
        if direction == 'import':
            raise ValueError(
                'L/C này là L/C nhập khẩu (ký quỹ 244) — chọn / mở L/C xuất (direction=export)'
            )
        bal = get_lc_balance(conn, linked_lc_id)
        remain = _money(bal.get('remaining_fc') or 0)
        # trừ sale khác đã gắn chưa settle
        reserved = Decimal('0')
        try:
            sql = """
                SELECT COALESCE(SUM(COALESCE(amount_fc,0)),0) FROM sale
                WHERE linked_lc_id = ? AND COALESCE(settle_journal_id,0) = 0
            """
            params: list[Any] = [linked_lc_id]
            if edit_id:
                sql += ' AND id != ?'
                params.append(edit_id)
            reserved = _money(conn.execute(sql, params).fetchone()[0])
        except sqlite3.OperationalError:
            reserved = Decimal('0')
        avail = remain - reserved
        if avail < 0:
            avail = Decimal('0')
        if total_fc > avail + Decimal('0.0001'):
            raise ValueError(
                f'L/C còn khả dụng {float(avail):g} NT — không đủ cho đợt này ({float(total_fc):g} NT)'
            )

    export_tax_fc = _money(data.get('export_tax_fc') or 0)
    export_tax_vnd = _money(data.get('export_tax_vnd') or 0)
    if export_tax_vnd <= 0 and export_tax_fc > 0:
        export_tax_vnd = _money(export_tax_fc * customs_rate)

    split = compute_split_fx_revenue_vnd(
        total_fc=total_fc,
        revenue_rate=revenue_rate,
        advances=advance_payloads if payment_mode in (
            PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL,
        ) else [],
    )
    advance_payloads = split.get('advances') or advance_payloads
    advance_fc = _money(split.get('advance_fc') or 0)
    advance_vnd = _money(split.get('advance_vnd') or 0)
    revenue_vnd = _money(split.get('revenue_vnd') or 0)
    total_amount = float(revenue_vnd)  # VND ghi nhận trên sale.total_amount

    sale_no = (data.get('sale_no') or '').strip()
    scols = _cols(conn, 'sale')
    icols = _cols(conn, 'sale_items')

    if edit_id:
        old = conn.execute('SELECT * FROM sale WHERE id = ?', (edit_id,)).fetchone()
        if not old:
            raise ValueError(f'Không tìm thấy phiếu #{edit_id}')
        from Services.einvoice_export import sale_has_official_invoice
        if sale_has_official_invoice(dict(old)):
            inv_no = old['invoice_number'] if 'invoice_number' in old.keys() else ''
            raise ValueError(
                f'Phiếu đã xuất HĐĐT chính thức ({inv_no}). '
                'Không được sửa trực tiếp — dùng hóa đơn thay thế/điều chỉnh '
                f'(/sale/edit-reissue?invoice_no={inv_no}&sale_id={edit_id}).'
            )
        sale_no = sale_no or (old['sale_no'] if 'sale_no' in old.keys() else None) or _next_sale_no(conn)
        # Xóa stock cũ (EXPORT_SHIP mới + SALE cũ)
        conn.execute(
            """
            DELETE FROM stock_moves
            WHERE ref_id = ? AND UPPER(type) IN ('EXPORT_SHIP', 'SALE')
            """,
            (edit_id,),
        )
        conn.execute('DELETE FROM sale_items WHERE sale_id = ?', (edit_id,))
        # Đảo bút toán xuất kho ra cảng nếu có
        reverse_export_journals(
            conn, edit_id, posting_date=sale_date, created_by=created_by,
            reason='Thay thế phiếu xuất kho ra cảng',
            doc_types=DOC_TYPES_SHIP,
        )
        sets = [
            'date = ?', 'total_amount = ?', 'payment_method = ?',
            'customer_name = ?', 'company_name = ?', 'tax_code = ?', 'address = ?',
            'note = ?', 'status = ?',
        ]
        vals: list[Any] = [
            sale_date, total_amount, '131',
            customer_name,
            data.get('company_name') or customer_name,
            data.get('tax_code') or '',
            data.get('address') or '',
            data.get('note') or '',
            'completed',
        ]
        extra_map = {
            'sale_type': 'EXPORT',
            'payment_mode': payment_mode,
            'currency': currency,
            'exchange_rate': float(revenue_rate),
            'customs_fx_rate': float(customs_rate),
            'amount_fc': float(total_fc),
            'advance_fc': float(advance_fc),
            'advance_vnd': float(advance_vnd),
            'export_tax_fc': float(export_tax_fc),
            'export_tax_vnd': float(export_tax_vnd),
            'linked_lc_id': linked_lc_id,
            'incoterms': (data.get('incoterms') or '').strip() or None,
            'bl_no': (data.get('bl_no') or '').strip() or None,
            'customs_decl_no': (data.get('customs_decl_no') or '').strip() or None,
            'risk_transfer_date': risk_date,
            'warehouse_code': warehouse_code,
            'sale_no': sale_no,
            'tax_pct': 0,
            'tax_amount': 0,
            'export_status': EXPORT_STATUS_SHIPPED,
            'internal_transfer_doc_no': (data.get('internal_transfer_doc_no') or '').strip() or None,
        }
        for col, val in extra_map.items():
            if col in scols:
                sets.append(f'{col} = ?')
                vals.append(val)
        vals.append(edit_id)
        conn.execute(f"UPDATE sale SET {', '.join(sets)} WHERE id = ?", vals)
        sale_id = edit_id
    else:
        if not sale_no:
            sale_no = _next_sale_no(conn)
        fields = [
            'date', 'total_amount', 'payment_method', 'customer_name', 'company_name',
            'tax_code', 'address', 'note', 'status', 'sale_no', 'business_line',
            'tax_pct', 'tax_amount', 'created_at',
        ]
        values: list[Any] = [
            sale_date, total_amount, '131', customer_name,
            data.get('company_name') or customer_name,
            data.get('tax_code') or '', data.get('address') or '',
            data.get('note') or '', 'completed', sale_no, 'export',
            0, 0, _now(),
        ]
        extra_map = {
            'sale_type': 'EXPORT',
            'payment_mode': payment_mode,
            'currency': currency,
            'exchange_rate': float(revenue_rate),
            'customs_fx_rate': float(customs_rate),
            'amount_fc': float(total_fc),
            'advance_fc': float(advance_fc),
            'advance_vnd': float(advance_vnd),
            'export_tax_fc': float(export_tax_fc),
            'export_tax_vnd': float(export_tax_vnd),
            'linked_lc_id': linked_lc_id,
            'incoterms': (data.get('incoterms') or '').strip() or None,
            'bl_no': (data.get('bl_no') or '').strip() or None,
            'customs_decl_no': (data.get('customs_decl_no') or '').strip() or None,
            'risk_transfer_date': risk_date,
            'warehouse_code': warehouse_code,
            'ar_status': 'open',
            'export_status': EXPORT_STATUS_SHIPPED,
            'internal_transfer_doc_no': (data.get('internal_transfer_doc_no') or '').strip() or None,
        }
        for col, val in extra_map.items():
            if col in scols:
                fields.append(col)
                values.append(val)
        placeholders = ', '.join(['?'] * len(values))
        conn.execute(
            f"INSERT INTO sale ({', '.join(fields)}) VALUES ({placeholders})",
            values,
        )
        sale_id = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])

    # Dòng hàng + xuất kho WAC
    px_items: list[dict[str, Any]] = []
    px_total = Decimal('0.00')
    for it in normalized_items:
        pid = it['product_id']
        qty = float(it['qty'])
        # cost WAC
        try:
            _new_c, move_cost = apply_wac_outbound(conn.cursor(), pid, qty, None)
            cost = float(move_cost or 0)
        except Exception:
            pcols = _cols(conn, 'products')
            if 'cost_price' in pcols and 'buyprice' in pcols:
                sql = 'SELECT COALESCE(cost_price, buyprice, 0) FROM products WHERE id = ?'
            elif 'cost_price' in pcols:
                sql = 'SELECT COALESCE(cost_price, 0) FROM products WHERE id = ?'
            elif 'buyprice' in pcols:
                sql = 'SELECT COALESCE(buyprice, 0) FROM products WHERE id = ?'
            else:
                sql = None
            if sql:
                prow = conn.execute(sql, (pid,)).fetchone()
                cost = float(prow[0] if prow else 0)
            else:
                cost = 0.0

        icols = _cols(conn, 'sale_items')
        ifields = ['sale_id', 'product_id', 'quantity', 'price']
        ivals: list[Any] = [sale_id, pid, qty, it['price']]
        optional_item = [
            ('cost_price', cost),
            ('discount_pct', it['discount_pct']),
            ('tax_pct', 0),
            ('product_name', it['product_name']),
            ('unit', it['unit']),
            ('line_total', float(_money(Decimal(str(it['line_fc'])) * revenue_rate))),
        ]
        for col, val in optional_item:
            if col in icols:
                ifields.append(col)
                ivals.append(val)
        if 'line_type' in icols:
            ifields.append('line_type')
            ivals.append(it['line_type'])
        if 'warehouse_code' in icols:
            ifields.append('warehouse_code')
            ivals.append(it.get('warehouse_code'))
        conn.execute(
            f"INSERT INTO sale_items ({', '.join(ifields)}) VALUES ({', '.join(['?']*len(ivals))})",
            ivals,
        )
        _insert_sale_stock_move(
            conn,
            product_id=pid,
            sale_id=sale_id,
            sale_date=sale_date,
            qty=qty,
            cost=cost,
            sale_no=sale_no,
            warehouse_code=it.get('warehouse_code') or warehouse_code,
            unit=it['unit'],
        )
        try:
            sync_inventory_quantity_from_moves(conn.cursor(), pid)
        except Exception:
            pass

        # Dòng phiếu xuất kho 02-VT (giá xuất kho = WAC)
        pcols = _cols(conn, 'products')
        code_sel = "''"
        if 'product_code' in pcols:
            code_sel = 'COALESCE(product_code, barcode, \'\')'
        elif 'barcode' in pcols:
            code_sel = 'COALESCE(barcode, \'\')'
        prow = conn.execute(
            f'SELECT name, {code_sel} FROM products WHERE id = ?', (pid,),
        ).fetchone()
        pname = it['product_name'] or (prow[0] if prow else '')
        pcode = (prow[1] if prow else '') or ''
        line_amt = _money(Decimal(str(qty)) * Decimal(str(cost)))
        px_total += line_amt
        px_items.append({
            'product_id': pid,
            'product_name': pname,
            'product_code': pcode,
            'unit': it['unit'],
            'quantity': qty,
            'qty': qty,
            'price': float(cost),
            'amount': float(line_amt),
        })

    # Phiếu xuất kho mẫu 02-VT (PXxxxxxx) — bắt buộc khi xuất kho ra cảng
    from Services.sme.stock_vouchers import upsert_stock_out_voucher_for_sale
    px_voucher = upsert_stock_out_voucher_for_sale(
        conn,
        sale_id=sale_id,
        sale_date=sale_date,
        customer_name=customer_name,
        items=px_items,
        total_amount=float(px_total),
        note=f'Xuất kho ra cảng {sale_no}',
        address=(data.get('address') or '').strip(),
        reuse_voucher_no=True,
    )

    replace_sale_advances(
        conn, sale_id,
        advance_payloads if payment_mode in (
            PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL,
        ) else [],
        commit=False,
    )

    if linked_lc_id:
        try:
            conn.execute(
                """
                UPDATE sme_lc_docs
                SET sale_id = ?, updated_at = datetime('now','localtime')
                WHERE id = ? AND status = 'open'
                """,
                (sale_id, linked_lc_id),
            )
        except sqlite3.OperationalError:
            pass

    journal = sync_export_sale_journals(
        conn, sale_id, created_by=created_by, replace_existing=bool(edit_id),
    )

    if commit:
        conn.commit()

    return {
        'success': True,
        'sale_id': sale_id,
        'sale_no': sale_no,
        'total_fc': float(total_fc),
        'total_amount': total_amount,
        'payment_mode': payment_mode,
        'payment_status': payment_status_label(payment_mode),
        'export_status': EXPORT_STATUS_SHIPPED,
        'stock_out_voucher_id': px_voucher.get('id'),
        'stock_out_voucher_no': px_voucher.get('voucher_no'),
        'form_code': '02-VT',
        'journal': journal,
        'split': split,
        'message': (
            f'Đã xuất kho ra cảng {sale_no}: phiếu xuất kho 02-VT '
            f'{px_voucher.get("voucher_no")} + Nợ 157 / Có kho. '
            'Chờ thông quan (TKHQ + B/L) để ghi DT/GV và xuất HĐĐT.'
        ),
    }


def get_export_sale(conn: sqlite3.Connection, sale_id: int) -> dict[str, Any] | None:
    ensure_export_sale_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    items = [dict(r) for r in conn.execute(
        'SELECT * FROM sale_items WHERE sale_id = ?', (sale_id,),
    ).fetchall()]
    d['items'] = items
    d['linked_advances'] = list_sale_advances(conn, sale_id)
    d['export_status'] = export_status_of(d)
    if d['export_status'] == EXPORT_STATUS_SHIPPED and active_entries(conn, sale_id, DOC_TYPES_CLEARANCE):
        d['export_status'] = EXPORT_STATUS_CLEARED
    d['can_clearance'] = d['export_status'] == EXPORT_STATUS_SHIPPED
    d['can_einvoice'] = d['export_status'] == EXPORT_STATUS_CLEARED
    try:
        px = conn.execute(
            """
            SELECT id, voucher_no, total_amount, note
            FROM phieu_xuat_kho WHERE sale_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (sale_id,),
        ).fetchone()
        if px:
            d['stock_out_voucher_id'] = px[0] if not isinstance(px, sqlite3.Row) else px['id']
            d['stock_out_voucher_no'] = px[1] if not isinstance(px, sqlite3.Row) else px['voucher_no']
            d['form_code'] = '02-VT'
    except sqlite3.OperationalError:
        pass
    return d


def list_export_sales(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    q: str | None = None,
) -> list[dict]:
    ensure_export_sale_schema(conn, commit=False)
    if 'sale_type' not in _cols(conn, 'sale'):
        return []
    scols = _cols(conn, 'sale')
    base_cols = [
        'id', 'sale_no', 'date', 'risk_transfer_date', 'customer_name', 'currency',
        'amount_fc', 'exchange_rate', 'total_amount', 'payment_mode', 'ar_status',
        'incoterms', 'bl_no', 'linked_lc_id', 'status',
    ]
    extra = [
        c for c in (
            'customs_decl_no', 'invoice_number', 'invoice_status', 'invoice_id',
            'export_status', 'internal_transfer_doc_no',
            'export_tax_vnd', 'export_tax_fc', 'tax_payment_voucher_id',
        )
        if c in scols
    ]
    select_cols = ', '.join(base_cols + extra)
    sql = f"""
        SELECT {select_cols}
        FROM sale
        WHERE UPPER(COALESCE(sale_type,'')) = 'EXPORT'
    """
    params: list[Any] = []
    if q:
        sql += ' AND (sale_no LIKE ? OR customer_name LIKE ? OR bl_no LIKE ?)'
        params.extend([f'%{q}%'] * 3)
    sql += ' ORDER BY date(date) DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
