"""Đồng bộ phiếu nhập kho POS sang sổ nhật ký SME (TT99)."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from Services.sme.journal_engine import (
    build_import_stock_lines,
    post_journal_entry,
    reverse_journal_entry,
)

IMPORT_DOCUMENT_TYPE = 'PNK'
# finished_goods chỉ qua SX (TK 155) — không phải dòng mua hàng SME
STOCK_LINE_TYPES = frozenset({'goods', 'materials'})
PURCHASE_LINE_TYPES = frozenset({
    'goods', 'materials', 'service', 'fixed_asset', 'tools',
})
BUSINESS_TYPE_LABELS = {
    'NHAP_KHO_HANG_HOA': 'Nhập kho hàng hóa',
    'NHAP_KHO_NVL': 'Nhập kho nguyên vật liệu',
    'MUA_DICH_VU': 'Mua dịch vụ',
    'MUA_TSCD': 'Mua TSCĐ',
    'MUA_CCDC': 'Mua CCDC',
}
# Thuế NK phân bổ vào nguyên giá (không áp dụng dịch vụ)
IMPORT_TAX_BUSINESS_TYPES = frozenset({
    'NHAP_KHO_HANG_HOA', 'NHAP_KHO_NVL', 'MUA_TSCD', 'MUA_CCDC',
})


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def _business_type_for_line(line_type: str | None) -> str | None:
    lt = (line_type or 'goods').strip().lower()
    # Thành phẩm tự SX không nhập mua — nếu lọt vào thì coi như hàng hóa
    if lt == 'finished_goods':
        lt = 'goods'
    if lt not in PURCHASE_LINE_TYPES:
        return None
    if lt == 'materials':
        return 'NHAP_KHO_NVL'
    if lt == 'service':
        return 'MUA_DICH_VU'
    if lt == 'fixed_asset':
        return 'MUA_TSCD'
    if lt == 'tools':
        return 'MUA_CCDC'
    return 'NHAP_KHO_HANG_HOA'


def _resolve_payment_method(
    payment_status: str | None,
    payment_method: str | None,
) -> str:
    status = (payment_status or '').strip()
    if status in ('Chưa thanh toán', 'Unpaid', ''):
        return 'CREDIT'
    raw = str(payment_method or 'cash').strip().upper()
    if raw in ('CASH', '111', 'TIỀN MẶT', 'TIEN MAT'):
        return 'CASH'
    if raw in ('CREDIT', '331', 'CONG NO', 'CÔNG NỢ'):
        return 'CREDIT'
    return 'BANK_TRANSFER'


def _active_import_entries(conn: sqlite3.Connection, import_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE document_id = ? AND document_type = ?
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id
        """,
        (import_id, IMPORT_DOCUMENT_TYPE),
    ).fetchall()
    return [int(row[0]) for row in rows]


def reverse_import_journals(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    posting_date: str | None = None,
    created_by: str | None = None,
    reason: str = 'Hủy/thay thế bút toán phiếu nhập',
) -> list[int]:
    """Đảo tất cả bút toán PNK còn hiệu lực của phiếu nhập. Không commit."""
    from Services.sme.bootstrap import ensure_sme_accounting_ready

    ensure_sme_accounting_ready(conn, commit=False)
    reversed_ids: list[int] = []
    for entry_id in _active_import_entries(conn, import_id):
        rev = reverse_journal_entry(
            conn,
            entry_id,
            posting_date=posting_date,
            created_by=created_by,
            reason=reason,
        )
        reversed_ids.append(int(rev['id']))
    return reversed_ids


def sync_import_journals(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    accounting_regime: str | None,
    created_by: str | None = None,
    replace_existing: bool = False,
    features: dict | None = None,
    payment_method: str | None = None,
    import_type: str | None = None,
    import_tax_amount: Decimal | float | None = None,
    exchange_rate: Decimal | float | None = None,
) -> dict:
    """
    Ghi bút toán mua hàng từ phiếu import:
    hàng hóa/NVL (156/152), dịch vụ (642), TSCĐ (2112), CCDC (153) + VAT + đối ứng.
    Hỗ trợ DOMESTIC / IMPORT (thuế NK → Nợ kho + Có 3333, VAT ưu tiên 13312).

    Không commit — caller commit cùng giao dịch nhập kho.
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

    imp = conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone()
    if not imp:
        raise ValueError(f'Không tìm thấy phiếu nhập #{import_id}')

    active_ids = _active_import_entries(conn, import_id)
    reversed_ids: list[int] = []
    posting_date = str(imp['date'] or '')[:10] or None
    if active_ids and replace_existing:
        reversed_ids = reverse_import_journals(
            conn,
            import_id,
            posting_date=posting_date,
            created_by=created_by,
            reason='Thay thế bút toán do cập nhật phiếu nhập',
        )
        active_ids = []
    if active_ids:
        return {
            'posted': False,
            'reason': 'already_posted',
            'entry_ids': active_ids,
            'reversed_entry_ids': reversed_ids,
        }

    imp_keys = set(imp.keys()) if hasattr(imp, 'keys') else set()
    resolved_import_type = (
        (import_type or (imp['import_type'] if 'import_type' in imp_keys else None) or 'DOMESTIC')
        .strip()
        .upper()
    )
    if resolved_import_type not in ('DOMESTIC', 'IMPORT'):
        resolved_import_type = 'DOMESTIC'
    total_import_tax = _money(
        import_tax_amount
        if import_tax_amount is not None
        else (imp['import_tax_amount'] if 'import_tax_amount' in imp_keys else 0)
    )

    detail_cols = _table_columns(conn, 'import_details')
    select_parts = [
        'd.product_id',
        'd.qty',
        'd.buyprice',
        'COALESCE(d.subtotal, d.qty * d.buyprice) AS subtotal',
        'COALESCE(d.discount, 0) AS discount',
        'COALESCE(d.tax, 0) AS tax',
    ]
    if 'tax_pct' in detail_cols:
        select_parts.append('COALESCE(d.tax_pct, 0) AS tax_pct')
    else:
        select_parts.append('0 AS tax_pct')
    if 'line_type' in detail_cols:
        select_parts.append("COALESCE(d.line_type, 'goods') AS line_type")
    else:
        select_parts.append("'goods' AS line_type")
    if 'warehouse_code' in detail_cols:
        select_parts.append("COALESCE(d.warehouse_code, 'KHO_001') AS warehouse_code")
    else:
        select_parts.append("'KHO_001' AS warehouse_code")
    if 'product_name' in detail_cols:
        select_parts.append("COALESCE(d.product_name, '') AS detail_product_name")
    else:
        select_parts.append("'' AS detail_product_name")

    details = conn.execute(
        f"""
        SELECT {', '.join(select_parts)},
               COALESCE(p.name, '') AS product_name
        FROM import_details d
        LEFT JOIN products p ON p.id = d.product_id
        WHERE d.import_id = ?
        """,
        (import_id,),
    ).fetchall()

    # SME: CP phát sinh có HĐ riêng → phân bổ landed cost; không dùng extra_cost HKD
    extra_cost = Decimal('0.00')
    base_total = Decimal('0.00')
    stock_rows: list[dict] = []
    for row in details:
        b_type = _business_type_for_line(row['line_type'])
        if not b_type:
            continue
        # Hàng hóa/NVL/TSCĐ/CCDC cần product_id; dịch vụ cho phép NULL
        needs_product = b_type != 'MUA_DICH_VU'
        if needs_product and not row['product_id']:
            continue
        subtotal = _money(row['subtotal'])
        discount = _money(row['discount'])
        net = subtotal - discount
        if net <= 0:
            continue
        base_total += max(subtotal, Decimal('0.00'))
        name = (row['product_name'] or row['detail_product_name'] or '').strip() or (
            f"SP#{row['product_id']}" if row['product_id'] else 'Dịch vụ'
        )
        stock_rows.append({
            'business_type': b_type,
            'product_id': int(row['product_id']) if row['product_id'] else None,
            'product_name': name,
            'subtotal': subtotal,
            'net': net,
            'tax': _money(row['tax']),
            'tax_pct': float(row['tax_pct'] or 0),
            'warehouse_code': row['warehouse_code'],
        })

    if not stock_rows:
        return {
            'posted': False,
            'reason': 'no_stock_lines',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    base_safe = base_total if base_total > 0 else Decimal('1.00')
    nk_base_total = sum(
        (item['subtotal'] for item in stock_rows if item['business_type'] in IMPORT_TAX_BUSINESS_TYPES),
        Decimal('0.00'),
    )
    nk_base_safe = nk_base_total if nk_base_total > 0 else Decimal('1.00')
    pay_method = _resolve_payment_method(
        imp['payment_status'] if 'payment_status' in imp.keys() else None,
        payment_method
        or (imp['payment_method'] if 'payment_method' in imp.keys() else None),
    )

    supplier_id = imp['supplier_id'] if 'supplier_id' in imp.keys() else None
    bill_no = imp['bill_no'] if 'bill_no' in imp.keys() else None
    import_no = imp['import_no'] if 'import_no' in imp.keys() else None
    bill_date = None
    if 'bill_date' in imp.keys() and imp['bill_date']:
        bill_date = str(imp['bill_date'])[:10]
    tax_code = None
    if supplier_id:
        tax_row = conn.execute(
            'SELECT tax_code FROM suppliers WHERE id = ?',
            (supplier_id,),
        ).fetchone()
        if tax_row and tax_row[0]:
            tax_code = tax_row[0]

    groups: dict[str, list[dict]] = {}
    for item in stock_rows:
        groups.setdefault(item['business_type'], []).append(item)

    posted: list[dict] = []
    for b_type, items in groups.items():
        inventory_lines = []
        vat_total = Decimal('0.00')
        payable_total = Decimal('0.00')
        group_import_tax = Decimal('0.00')
        desc_text = BUSINESS_TYPE_LABELS.get(b_type, b_type)
        group_base = sum((item['subtotal'] for item in items), Decimal('0.00'))
        group_base_safe = group_base if group_base > 0 else Decimal('1.00')
        # Phân bổ thuế NK theo tỷ trọng subtotal nhóm đủ điều kiện (không vào dịch vụ)
        if (
            resolved_import_type == 'IMPORT'
            and total_import_tax > 0
            and b_type in IMPORT_TAX_BUSINESS_TYPES
        ):
            group_import_tax_budget = _money(total_import_tax * (group_base / nk_base_safe))
        else:
            group_import_tax_budget = Decimal('0.00')

        for item in items:
            allocated_extra = _money((item['subtotal'] / base_safe) * extra_cost)
            allocated_nk = _money((item['subtotal'] / group_base_safe) * group_import_tax_budget)
            inv_amount = item['net'] + allocated_extra + allocated_nk
            vat_total += item['tax']
            payable_total += item['net'] + item['tax'] + allocated_extra
            group_import_tax += allocated_nk
            inventory_lines.append({
                'product_id': item['product_id'],
                'product_name': item['product_name'],
                'amount': inv_amount,
                'tax_pct': item['tax_pct'],
                'warehouse_code': item['warehouse_code'],
                'description': f"{desc_text}: {item['product_name']}",
            })

        _rule, journal_lines = build_import_stock_lines(
            conn,
            business_type=b_type,
            payment_method=pay_method,
            inventory_lines=inventory_lines,
            vat_amount=vat_total,
            import_tax_amount=group_import_tax,
            payable_amount=payable_total,
            supplier_id=int(supplier_id) if supplier_id else None,
            bill_no=bill_no,
            tax_code=tax_code,
            import_type=resolved_import_type,
            description=f"{desc_text} HĐ {bill_no or import_no}",
        )
        posted.append(post_journal_entry(
            conn,
            posting_date=posting_date or '',
            document_date=bill_date or posting_date,
            document_type=IMPORT_DOCUMENT_TYPE,
            document_no=import_no,
            document_id=import_id,
            business_type=b_type,
            description=f"{desc_text} theo phiếu {import_no or ('#' + str(import_id))}",
            reference_document=bill_no or import_no,
            created_by=created_by,
            lines=journal_lines,
        ))

    return {
        'posted': bool(posted),
        'entry_ids': [item['id'] for item in posted],
        'reversed_entry_ids': reversed_ids,
        'import_type': resolved_import_type,
    }
