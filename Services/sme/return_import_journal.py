"""Đồng bộ trả hàng NCC sang sổ nhật ký SME (đảo bút toán nhập kho)."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from Services.sme.journal_engine import (
    build_return_import_stock_lines,
    post_journal_entry,
    reverse_journal_entry,
)

RETURN_DOCUMENT_TYPE = 'THN'
PAYMENT_MAP = {
    '111': 'CASH',
    'CASH': 'CASH',
    '112': 'BANK_TRANSFER',
    'BANK': 'BANK_TRANSFER',
    'BANK_TRANSFER': 'BANK_TRANSFER',
    '131': 'CREDIT',
    'CREDIT': 'CREDIT',
    '331': 'CREDIT',
}


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def _resolve_payment_method(payment_method: str | None) -> str:
    raw = str(payment_method or '111').strip().upper()
    return PAYMENT_MAP.get(raw, 'CASH')


def _business_type_for_product(conn: sqlite3.Connection, product_id: int) -> str:
    cols = _table_columns(conn, 'products')
    if 'product_type' in cols:
        row = conn.execute(
            'SELECT COALESCE(product_type, ?) FROM products WHERE id = ?',
            ('goods', product_id),
        ).fetchone()
        pt = str(row[0] if row else 'goods').strip().lower()
        if pt == 'materials':
            return 'NHAP_KHO_NVL'
    return 'NHAP_KHO_HANG_HOA'


def _active_return_entries(conn: sqlite3.Connection, document_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE document_id = ? AND document_type = ?
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id
        """,
        (document_id, RETURN_DOCUMENT_TYPE),
    ).fetchall()
    return [int(row[0]) for row in rows]


def reverse_return_import_journals(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    posting_date: str | None = None,
    created_by: str | None = None,
    reason: str = 'Hủy/thay thế bút toán trả NCC',
) -> list[int]:
    from Services.sme.bootstrap import ensure_sme_accounting_ready

    ensure_sme_accounting_ready(conn, commit=False)
    reversed_ids: list[int] = []
    for entry_id in _active_return_entries(conn, document_id):
        rev = reverse_journal_entry(
            conn,
            entry_id,
            posting_date=posting_date,
            created_by=created_by,
            reason=reason,
        )
        reversed_ids.append(int(rev['id']))
    return reversed_ids


def _line_amounts_from_import(
    conn: sqlite3.Connection,
    import_id: int,
    product_id: int,
    return_qty: float,
) -> dict | None:
    """Tỷ lệ giá trị trả theo chứng từ nhập gốc (không VAT trong kho)."""
    detail_cols = _table_columns(conn, 'import_details')
    select = [
        'COALESCE(qty, 0) AS qty',
        'COALESCE(buyprice, 0) AS buyprice',
        'COALESCE(subtotal, qty * buyprice) AS subtotal',
        'COALESCE(discount, 0) AS discount',
        'COALESCE(tax, 0) AS tax',
    ]
    if 'unit_type' in detail_cols:
        select.append('COALESCE(unit_type, 0) AS unit_type')
    else:
        select.append('0 AS unit_type')
    if 'warehouse_code' in detail_cols:
        select.append("COALESCE(warehouse_code, 'KHO_001') AS warehouse_code")
    else:
        select.append("'KHO_001' AS warehouse_code")
    if 'tax_pct' in detail_cols:
        select.append('COALESCE(tax_pct, 0) AS tax_pct')
    else:
        select.append('0 AS tax_pct')
    if 'line_type' in detail_cols:
        select.append("COALESCE(line_type, 'goods') AS line_type")
    else:
        select.append("'goods' AS line_type")

    row = conn.execute(
        f"""
        SELECT {', '.join(select)}, COALESCE(p.name, '') AS product_name
        FROM import_details d
        LEFT JOIN products p ON p.id = d.product_id
        WHERE d.import_id = ? AND d.product_id = ?
        """,
        (import_id, product_id),
    ).fetchone()
    if not row:
        return None

    original_qty = float(row['qty'] or 0)
    if original_qty <= 0 or return_qty <= 0:
        return None
    ratio = Decimal(str(return_qty)) / Decimal(str(original_qty))
    net = (_money(row['subtotal']) - _money(row['discount'])) * ratio
    vat = _money(row['tax']) * ratio
    return {
        'product_id': product_id,
        'product_name': row['product_name'],
        'net': _money(net),
        'vat': _money(vat),
        'tax_pct': float(row['tax_pct'] or 0),
        'warehouse_code': row['warehouse_code'],
        'line_type': row['line_type'],
        'business_type': (
            'NHAP_KHO_NVL'
            if str(row['line_type'] or '').lower() == 'materials'
            else _business_type_for_product(conn, product_id)
        ),
    }


def sync_return_import_journals(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    import_id: int,
    lines: list[dict],
    payment_method: str | None,
    posting_date: str,
    document_no: str | None = None,
    accounting_regime: str | None = None,
    features: dict | None = None,
    created_by: str | None = None,
    replace_existing: bool = False,
    reason: str | None = None,
) -> dict:
    """
    Ghi bút toán trả NCC.

    lines: [{product_id, quantity}, ...] — quantity theo đơn vị trên phiếu nhập gốc.
    document_id: sale_id (checkout) hoặc return_import.id (API 1 dòng).
    Không commit.
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

    active_ids = _active_return_entries(conn, document_id)
    reversed_ids: list[int] = []
    date_key = str(posting_date or '')[:10] or None
    if active_ids and replace_existing:
        reversed_ids = reverse_return_import_journals(
            conn,
            document_id,
            posting_date=date_key,
            created_by=created_by,
        )
        active_ids = []
    if active_ids:
        return {
            'posted': False,
            'reason': 'already_posted',
            'entry_ids': active_ids,
            'reversed_entry_ids': reversed_ids,
        }

    imp = conn.execute(
        'SELECT * FROM "import" WHERE id = ?',
        (import_id,),
    ).fetchone()
    if not imp:
        raise ValueError(f'Không tìm thấy phiếu nhập #{import_id}')

    supplier_id = imp['supplier_id'] if 'supplier_id' in imp.keys() else None
    bill_no = imp['bill_no'] if 'bill_no' in imp.keys() else None
    import_no = imp['import_no'] if 'import_no' in imp.keys() else None
    tax_code = None
    if supplier_id:
        tax_row = conn.execute(
            'SELECT tax_code FROM suppliers WHERE id = ?',
            (supplier_id,),
        ).fetchone()
        if tax_row and tax_row[0]:
            tax_code = tax_row[0]

    pay_method = _resolve_payment_method(payment_method)
    groups: dict[str, list[dict]] = {}
    for raw in lines:
        pid = int(raw.get('product_id') or 0)
        qty = float(raw.get('quantity') or 0)
        if pid <= 0 or qty <= 0:
            continue
        amounts = _line_amounts_from_import(conn, import_id, pid, qty)
        if not amounts or amounts['net'] <= 0:
            continue
        groups.setdefault(amounts['business_type'], []).append(amounts)

    if not groups:
        return {
            'posted': False,
            'reason': 'no_stock_lines',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    posted: list[dict] = []
    desc_base = reason or f"Trả hàng NCC theo {import_no or ('PN#' + str(import_id))}"
    from Services.sme.tt58_tax_methods import tt58_input_vat_in_inventory_cost
    capitalize_vat = tt58_input_vat_in_inventory_cost(conn)
    for b_type, items in groups.items():
        inventory_lines = []
        vat_total = Decimal('0.00')
        refund_total = Decimal('0.00')
        label = (
            'Trả hàng hóa NCC' if b_type == 'NHAP_KHO_HANG_HOA' else 'Trả NVL NCC'
        )
        for item in items:
            vat_total += item['vat']
            refund_total += item['net'] + item['vat']
            inv_amt = item['net']
            if capitalize_vat:
                inv_amt += item['vat']
            inventory_lines.append({
                'product_id': item['product_id'],
                'product_name': item['product_name'],
                'amount': inv_amt,
                'tax_pct': item['tax_pct'],
                'warehouse_code': item['warehouse_code'],
                'description': f"{label}: {item['product_name']}",
            })

        _rule, journal_lines = build_return_import_stock_lines(
            conn,
            business_type=b_type,
            payment_method=pay_method,
            inventory_lines=inventory_lines,
            vat_amount=Decimal('0.00') if capitalize_vat else vat_total,
            refund_amount=refund_total,
            supplier_id=int(supplier_id) if supplier_id else None,
            bill_no=bill_no,
            tax_code=tax_code,
            description=desc_base,
        )
        posted.append(post_journal_entry(
            conn,
            posting_date=date_key or '',
            document_date=date_key,
            document_type=RETURN_DOCUMENT_TYPE,
            document_no=document_no or import_no,
            document_id=document_id,
            business_type=f'TRA_{b_type}',
            description=desc_base,
            reference_document=bill_no or import_no,
            created_by=created_by,
            lines=journal_lines,
        ))

    return {
        'posted': bool(posted),
        'entry_ids': [item['id'] for item in posted],
        'reversed_entry_ids': reversed_ids,
    }
