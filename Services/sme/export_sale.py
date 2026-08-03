"""Lập phiếu bán xuất khẩu + hạch toán DT / thuế XK / giá vốn."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

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
    post_journal_entry,
    reverse_journal_entry,
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
    rows = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE document_id = ?
          AND document_type IN ('EXPORT_REVENUE', 'EXPORT_COGS', 'EXPORT_TAX')
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id
        """,
        (sale_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def reverse_export_journals(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    posting_date: str | None = None,
    created_by: str | None = None,
    reason: str = 'Thay thế bút toán xuất khẩu',
) -> list[int]:
    reversed_ids = []
    for eid in _active_export_entries(conn, sale_id):
        rev = reverse_journal_entry(
            conn, eid, posting_date=posting_date, created_by=created_by, reason=reason,
        )
        reversed_ids.append(int(rev['id']))
    return reversed_ids


def _cogs_accounts(line_type: str | None) -> tuple[str, str]:
    pt = (line_type or 'goods').strip().lower()
    if pt in ('finished_goods', 'finished', 'thanh_pham', 'ready_made'):
        return '632', '155'
    if pt in ('materials', 'material', 'nvl', 'raw_materials'):
        return '632', '152'
    return '632', '156'


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
    fields = [
        'product_id', 'date', 'type', 'ref_id', 'quantity', 'cost_price', 'note',
        'ref_document', 'ref_type', 'type1', 'unit',
    ]
    values: list[Any] = [
        product_id, sale_date, 'SALE', sale_id, -abs(float(qty)), float(cost),
        f'Xuất khẩu — {sale_no}', sale_no, 'sale', 'Xuất', unit or 'Cái',
    ]
    if 'in_quantity' in sm_cols and 'out_quantity' in sm_cols:
        fields.extend(['in_quantity', 'out_quantity'])
        values.extend([0.0, abs(float(qty))])
    if 'warehouse_code' in sm_cols and warehouse_code:
        fields.append('warehouse_code')
        values.append(warehouse_code)
    placeholders = ', '.join(['?'] * len(values))
    conn.execute(
        f"INSERT INTO stock_moves ({', '.join(fields)}) VALUES ({placeholders})",
        values,
    )


def sync_export_sale_journals(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    created_by: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Ghi Phần I: DT XK (131/511) + thuế XK (511/3333) + COGS (632/kho)."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_export_sale_schema(conn, commit=False)

    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError(f'Không tìm thấy phiếu bán #{sale_id}')
    s = dict(sale)
    if str(s.get('sale_type') or '').upper() != 'EXPORT':
        return {'posted': False, 'reason': 'not_export', 'entry_ids': []}

    active = _active_export_entries(conn, sale_id)
    reversed_ids: list[int] = []
    posting_date = (
        str(s.get('risk_transfer_date') or s.get('date') or '')[:10] or None
    )
    if active and replace_existing:
        reversed_ids = reverse_export_journals(
            conn, sale_id, posting_date=posting_date, created_by=created_by,
        )
        active = []
    if active:
        return {
            'posted': False,
            'reason': 'already_posted',
            'entry_ids': active,
            'reversed_entry_ids': reversed_ids,
        }

    from Services.sme.branch_filter import warehouse_branch_or_session

    currency = (s.get('currency') or 'USD').upper()
    revenue_rate = _fx(s.get('exchange_rate') or 1)
    customs_rate = _fx(s.get('customs_fx_rate') or revenue_rate)
    total_fc = _money(s.get('amount_fc') or 0)
    mode = normalize_payment_mode(s.get('payment_mode'), sale_type='EXPORT')

    advances = list_sale_advances(conn, sale_id)
    if not advances and _money(s.get('advance_fc')) > 0:
        advances = [{
            'amount_fc': float(s.get('advance_fc') or 0),
            'exchange_rate': float(s.get('exchange_rate') or revenue_rate),
            'amount_vnd': float(s.get('advance_vnd') or 0),
        }]

    use_advances = mode in (PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL)
    split = compute_split_fx_revenue_vnd(
        total_fc=total_fc,
        revenue_rate=revenue_rate,
        advances=advances if use_advances else [],
    )
    revenue_vnd = _money(split['revenue_vnd'])
    export_tax_vnd = _money(s.get('export_tax_vnd') or 0)
    if export_tax_vnd <= 0 and _money(s.get('export_tax_fc')) > 0:
        export_tax_vnd = _money(_money(s.get('export_tax_fc')) * customs_rate)

    # Net DT sau thuế XK (giảm trừ DT)
    net_revenue = _money(revenue_vnd - export_tax_vnd)
    if net_revenue < 0:
        net_revenue = Decimal('0.00')

    branch = warehouse_branch_or_session(conn, s.get('warehouse_code'))
    sale_no = s.get('sale_no') or f'#{sale_id}'
    customer = s.get('customer_name') or ''
    desc = f'Xuất khẩu {sale_no} — {customer}'.strip()

    rev_acct = resolve_postable_account(conn, REVENUE_ACCOUNT_DEFAULT)
    ar_acct = resolve_postable_account(conn, '131')

    posted: list[dict] = []

    # --- Doanh thu: Nợ 131 / Có 511 (theo TG tách ứng nếu có) ---
    rev_lines = [
        {
            'sequence': 1,
            'account_code': ar_acct,
            'debit': float(revenue_vnd),
            'credit': 0,
            'debit_fc': float(total_fc) if currency != 'VND' else 0,
            'credit_fc': 0,
            'partner_type': 'customer',
            'description': f'Phải thu XK {sale_no}',
        },
        {
            'sequence': 2,
            'account_code': rev_acct,
            'debit': 0,
            'credit': float(revenue_vnd),
            'debit_fc': 0,
            'credit_fc': float(total_fc) if currency != 'VND' else 0,
            'description': f'Doanh thu xuất khẩu {sale_no}',
        },
    ]
    posted.append(post_journal_entry(
        conn,
        posting_date=posting_date or '',
        document_date=str(s.get('date') or posting_date or '')[:10],
        document_type='EXPORT_REVENUE',
        document_no=sale_no,
        document_id=sale_id,
        business_type='XUAT_KHAU_DT',
        currency=currency,
        exchange_rate=float(revenue_rate),
        description=desc,
        reference_document=s.get('bl_no') or s.get('customs_decl_no') or sale_no,
        created_by=created_by,
        branch_code=branch,
        lines=rev_lines,
    ))

    # --- Thuế xuất khẩu: Nợ 511 / Có 3333 ---
    if export_tax_vnd > 0:
        tax_acct = resolve_postable_account(conn, '3333')
        tax_lines = [
            {
                'sequence': 1,
                'account_code': rev_acct,
                'debit': float(export_tax_vnd),
                'credit': 0,
                'description': f'Thuế XK giảm DT — {sale_no}',
            },
            {
                'sequence': 2,
                'account_code': tax_acct,
                'debit': 0,
                'credit': float(export_tax_vnd),
                'description': f'Thuế xuất khẩu phải nộp — {sale_no}',
            },
        ]
        posted.append(post_journal_entry(
            conn,
            posting_date=posting_date or '',
            document_date=str(s.get('date') or posting_date or '')[:10],
            document_type='EXPORT_TAX',
            document_no=sale_no,
            document_id=sale_id,
            business_type='XUAT_KHAU_THUE',
            currency='VND',
            exchange_rate=float(customs_rate),
            description=f'Thuế xuất khẩu {sale_no}',
            created_by=created_by,
            branch_code=branch,
            lines=tax_lines,
        ))

    # --- Giá vốn từ stock_moves ---
    cogs_rows = conn.execute(
        """
        SELECT
            COALESCE(p.product_type, 'goods') AS product_type,
            SUM(ABS(COALESCE(sm.quantity, 0)) * COALESCE(sm.cost_price, 0)) AS amount
        FROM stock_moves sm
        LEFT JOIN products p ON p.id = sm.product_id
        WHERE sm.ref_id = ? AND UPPER(sm.type) = 'SALE'
        GROUP BY COALESCE(p.product_type, 'goods')
        """,
        (sale_id,),
    ).fetchall()
    cogs_lines = []
    seq = 1
    for row in cogs_rows:
        amt = _money(row[1] if not isinstance(row, sqlite3.Row) else row['amount'])
        if amt <= 0:
            continue
        pt = row[0] if not isinstance(row, sqlite3.Row) else row['product_type']
        deb, cred = _cogs_accounts(pt)
        cogs_lines.extend([
            {
                'sequence': seq,
                'account_code': resolve_postable_account(conn, deb),
                'debit': float(amt),
                'credit': 0,
                'description': f'Giá vốn XK {sale_no}',
            },
            {
                'sequence': seq + 1,
                'account_code': resolve_postable_account(conn, cred),
                'debit': 0,
                'credit': float(amt),
                'description': f'Xuất kho XK {sale_no}',
            },
        ])
        seq += 2
    if cogs_lines:
        posted.append(post_journal_entry(
            conn,
            posting_date=posting_date or '',
            document_date=str(s.get('date') or posting_date or '')[:10],
            document_type='EXPORT_COGS',
            document_no=sale_no,
            document_id=sale_id,
            business_type='XUAT_KHAU_GV',
            currency='VND',
            exchange_rate=1,
            description=f'Giá vốn xuất khẩu {sale_no}',
            created_by=created_by,
            branch_code=branch,
            lines=cogs_lines,
        ))

    # prepaid_full → AR đã được ứng trước (Có 131 lúc nhận PT) nên net 131 = 0
    ar_status = 'open'
    if mode == PAYMENT_PREPAID_FULL or (
        mode == PAYMENT_PREPAID_PARTIAL and _money(split['remain_fc']) <= 0
    ):
        ar_status = 'settled'
    elif mode == PAYMENT_LC_USANCE:
        ar_status = 'accepted'
    elif mode == PAYMENT_DOC_DISCOUNT:
        ar_status = 'open'

    scols = _cols(conn, 'sale')
    if 'ar_status' in scols:
        conn.execute(
            'UPDATE sale SET ar_status = ? WHERE id = ?',
            (ar_status, sale_id),
        )

    return {
        'posted': bool(posted),
        'entry_ids': [p['id'] for p in posted],
        'reversed_entry_ids': reversed_ids,
        'revenue_vnd': float(revenue_vnd),
        'net_revenue_vnd': float(net_revenue),
        'export_tax_vnd': float(export_tax_vnd),
        'split': split,
        'ar_status': ar_status,
    }


def create_or_update_export_sale(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Tạo/cập nhật phiếu bán xuất khẩu + xuất kho + bút toán."""
    from Services.inventory_stock_helpers import apply_wac_outbound
    from Services.sme.inventory_ops import sync_inventory_quantity_from_moves

    ensure_export_sale_schema(conn, commit=False)
    ensure_sme_journal_ready(conn, commit=False)

    edit_id = data.get('sale_id') or data.get('edit_id') or data.get('id')
    try:
        edit_id = int(edit_id) if edit_id not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        edit_id = None

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
        sale_no = sale_no or (old['sale_no'] if 'sale_no' in old.keys() else None) or _next_sale_no(conn)
        # Xóa stock cũ
        conn.execute(
            "DELETE FROM stock_moves WHERE ref_id = ? AND UPPER(type) = 'SALE'",
            (edit_id,),
        )
        conn.execute('DELETE FROM sale_items WHERE sale_id = ?', (edit_id,))
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
    for it in normalized_items:
        pid = it['product_id']
        qty = float(it['qty'])
        # cost WAC
        try:
            _new_c, move_cost = apply_wac_outbound(conn.cursor(), pid, qty, None)
            cost = float(move_cost or 0)
        except Exception:
            prow = conn.execute(
                'SELECT COALESCE(cost_price, buyprice, 0) FROM products WHERE id = ?',
                (pid,),
            ).fetchone()
            cost = float(prow[0] if prow else 0)

        ifields = ['sale_id', 'product_id', 'quantity', 'price', 'cost_price',
                   'discount_pct', 'tax_pct', 'product_name', 'unit', 'line_total']
        # line_total lưu VND theo tỷ giá DT (ước lượng)
        line_vnd = float(_money(Decimal(str(it['line_fc'])) * revenue_rate))
        ivals: list[Any] = [
            sale_id, pid, qty, it['price'], cost,
            it['discount_pct'], 0, it['product_name'], it['unit'], line_vnd,
        ]
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
            sale_date=risk_date,
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

    # cong_no AR nếu còn phải thu
    remain_vnd = _money(split.get('remain_vnd') or 0)
    if remain_vnd > 0 and payment_mode != PAYMENT_PREPAID_FULL:
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
                    customer_name, data.get('company_name') or customer_name,
                    data.get('address') or '', data.get('tax_code') or '',
                    risk_date, float(remain_vnd), sale_id, sale_no,
                ),
            )
        except sqlite3.OperationalError:
            pass

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
        'journal': journal,
        'split': split,
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
    sql = """
        SELECT id, sale_no, date, risk_transfer_date, customer_name, currency,
               amount_fc, exchange_rate, total_amount, payment_mode, ar_status,
               incoterms, bl_no, linked_lc_id, status
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
