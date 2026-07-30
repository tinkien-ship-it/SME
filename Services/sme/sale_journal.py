"""Đồng bộ một hóa đơn bán hàng POS/F&B sang sổ nhật ký SME."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from Services.sme.journal_engine import (
    get_posting_rule,
    post_journal_entry,
    reverse_journal_entry,
)

SALE_DOCUMENT_TYPES = ('SALE_REVENUE', 'SALE_COGS')
PAYMENT_RULES = {
    '111': ('BAN_HANG_TM', 'CASH'),
    '112': ('BAN_HANG_CK', 'BANK_TRANSFER'),
    '131': ('BAN_HANG_CONG_NO', 'CREDIT'),
}


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def _active_sale_entries(conn: sqlite3.Connection, sale_id: int) -> list[int]:
    placeholders = ','.join('?' for _ in SALE_DOCUMENT_TYPES)
    rows = conn.execute(
        f"""
        SELECT id FROM sme_journal_entries
        WHERE document_id = ? AND document_type IN ({placeholders})
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id
        """,
        (sale_id, *SALE_DOCUMENT_TYPES),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _sale_vat(conn: sqlite3.Connection, sale_id: int, business_line: str) -> Decimal:
    """Tính lại VAT theo đúng công thức checkout POS; F&B hiện chưa lưu VAT dòng."""
    cols = _table_columns(conn, 'sale_items')
    if business_line == 'fb_service' or not {'quantity', 'price', 'tax_pct'}.issubset(cols):
        return Decimal('0.00')

    discount_expr = 'COALESCE(discount_pct, 0)' if 'discount_pct' in cols else '0'
    rows = conn.execute(
        f"""
        SELECT quantity, price, {discount_expr} AS discount_pct,
               COALESCE(tax_pct, 0) AS tax_pct
        FROM sale_items WHERE sale_id = ?
        """,
        (sale_id,),
    ).fetchall()
    vat = Decimal('0.00')
    for row in rows:
        subtotal = float(row[0] or 0) * float(row[1] or 0)
        discount = round(subtotal * (float(row[2] or 0) / 100))
        taxable = subtotal - discount
        vat += _money(round(taxable * (float(row[3] or 0) / 100)))
    return vat


def _build_revenue_lines(
    conn: sqlite3.Connection,
    sale: sqlite3.Row,
) -> tuple[str, list[dict]]:
    payment_code = str(sale['payment_method'] or '111')
    mapping = PAYMENT_RULES.get(payment_code)
    if not mapping:
        raise ValueError(f'Phương thức thanh toán bán hàng không hỗ trợ: {payment_code}')
    business_type, payment_method = mapping
    rule = get_posting_rule(conn, business_type, payment_method, commit=False)
    if not rule:
        raise ValueError(f'Chưa có quy tắc định khoản {business_type}/{payment_method}')

    total = _money(sale['total_amount'])
    if total <= 0:
        return business_type, []
    business_line = str(sale['business_line'] or 'pos') if 'business_line' in sale.keys() else 'pos'
    vat = _sale_vat(conn, int(sale['id']), business_line)
    if vat > total:
        raise ValueError(f'Thuế GTGT {vat} lớn hơn tổng thanh toán {total}')
    revenue = total - vat
    revenue_account = '5113' if business_line == 'fb_service' else rule['credit_account_code']
    common = {
        'partner_type': 'customer',
        'tax_code': sale['tax_code'] if 'tax_code' in sale.keys() else None,
        'vat_invoice_no': sale['invoice_number'] if 'invoice_number' in sale.keys() else None,
    }
    lines = [{
        **common,
        'account_code': rule['debit_account_code'],
        'debit': total,
        'credit': 0,
        'description': 'Thu tiền/phải thu khách hàng',
    }]
    if revenue > 0:
        lines.append({
            **common,
            'account_code': revenue_account,
            'debit': 0,
            'credit': revenue,
            'description': 'Doanh thu bán hàng và cung cấp dịch vụ',
        })
    if vat > 0:
        lines.append({
            **common,
            'account_code': rule.get('vat_account_code') or '33311',
            'debit': 0,
            'credit': vat,
            'description': 'Thuế GTGT đầu ra',
        })
    return business_type, lines


def _build_cogs_lines(conn: sqlite3.Connection, sale_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT CASE WHEN UPPER(type) = 'SALE_RECIPE' THEN 'SALE_RECIPE' ELSE 'SALE' END AS move_type,
               SUM(
                   CASE WHEN UPPER(type) = 'RETURN_SALE' THEN -1 ELSE 1 END
                   * ABS(COALESCE(quantity, 0)) * COALESCE(cost_price, 0)
               ) AS amount
        FROM stock_moves
        WHERE ref_id = ? AND UPPER(type) IN ('SALE', 'SALE_RECIPE', 'RETURN_SALE')
        GROUP BY CASE WHEN UPPER(type) = 'SALE_RECIPE' THEN 'SALE_RECIPE' ELSE 'SALE' END
        """,
        (sale_id,),
    ).fetchall()
    lines: list[dict] = []
    sequence = 1
    for row in rows:
        amount = _money(row[1])
        if amount <= 0:
            continue
        is_recipe = row[0] == 'SALE_RECIPE'
        debit_code = '6322' if is_recipe else '6321'
        credit_code = '152' if is_recipe else '156'
        label = 'nguyên liệu chế biến' if is_recipe else 'hàng hóa'
        lines.extend([
            {
                'sequence': sequence,
                'account_code': debit_code,
                'debit': amount,
                'credit': 0,
                'description': f'Giá vốn {label}',
            },
            {
                'sequence': sequence + 1,
                'account_code': credit_code,
                'debit': 0,
                'credit': amount,
                'description': f'Xuất kho {label}',
            },
        ])
        sequence += 2
    return lines


def sync_sale_journals(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    accounting_regime: str | None,
    created_by: str | None = None,
    replace_existing: bool = False,
    features: dict | None = None,
) -> dict:
    """
    Ghi doanh thu/VAT và giá vốn của sale đã completed.

    Hàm không commit; caller phải commit/rollback cùng giao dịch bán hàng.
    Tenant HKD được bỏ qua để không trộn sổ SME với Services/hkd_*.
    """
    regime = str(accounting_regime or '').upper()
    if features is not None:
        if not features.get('journal_posting'):
            return {'posted': False, 'reason': 'journal_posting_disabled', 'entry_ids': []}
    elif not regime.startswith('SME'):
        return {'posted': False, 'reason': 'not_sme', 'entry_ids': []}

    from Services.sme.bootstrap import ensure_sme_accounting_ready
    ensure_sme_accounting_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError(f'Không tìm thấy hóa đơn bán #{sale_id}')

    active_ids = _active_sale_entries(conn, sale_id)
    reversed_ids: list[int] = []
    if active_ids and replace_existing:
        reverse_date = str(sale['date'] or '')[:10] or None
        for entry_id in active_ids:
            reversed_entry = reverse_journal_entry(
                conn,
                entry_id,
                posting_date=reverse_date,
                created_by=created_by,
                reason='Thay thế bút toán do cập nhật hóa đơn bán',
            )
            reversed_ids.append(reversed_entry['id'])
        active_ids = []
    if active_ids:
        return {
            'posted': False,
            'reason': 'already_posted',
            'entry_ids': active_ids,
            'reversed_entry_ids': reversed_ids,
        }

    if str(sale['status'] or '').lower() != 'completed':
        return {
            'posted': False,
            'reason': 'sale_not_completed',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    business_line = ''
    if 'business_line' in sale.keys():
        business_line = str(sale['business_line'] or '').strip().lower()
    sale_no = str(sale['sale_no'] or '') if 'sale_no' in sale.keys() else ''
    note = str(sale['note'] or '') if 'note' in sale.keys() else ''
    if (
        business_line == 'return_import'
        or sale_no.upper().startswith('TR')
        or 'Trả hàng NCC' in note
    ):
        return {
            'posted': False,
            'reason': 'return_import_sale',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    posting_date = str(sale['date'] or '')[:10]
    document_no = sale['sale_no'] if 'sale_no' in sale.keys() else None
    description = f"Bán hàng {document_no or ('#' + str(sale_id))}"
    business_type, revenue_lines = _build_revenue_lines(conn, sale)
    posted: list[dict] = []
    if revenue_lines:
        posted.append(post_journal_entry(
            conn,
            posting_date=posting_date,
            document_date=posting_date,
            document_type='SALE_REVENUE',
            document_no=document_no,
            document_id=sale_id,
            business_type=business_type,
            description=description,
            reference_document=document_no,
            created_by=created_by,
            lines=revenue_lines,
        ))

    cogs_lines = _build_cogs_lines(conn, sale_id)
    if cogs_lines:
        posted.append(post_journal_entry(
            conn,
            posting_date=posting_date,
            document_date=posting_date,
            document_type='SALE_COGS',
            document_no=document_no,
            document_id=sale_id,
            business_type='GIA_VON_BAN_HANG',
            description=f'Giá vốn {description.lower()}',
            reference_document=document_no,
            created_by=created_by,
            lines=cogs_lines,
        ))
    return {
        'posted': bool(posted),
        'entry_ids': [item['id'] for item in posted],
        'reversed_entry_ids': reversed_ids,
    }
