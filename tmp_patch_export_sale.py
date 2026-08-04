# -*- coding: utf-8 -*-
"""One-shot patcher for export_sale.py — run then delete."""
from pathlib import Path

p = Path(r'C:\SME\Services\sme\export_sale.py')
text = p.read_text(encoding='utf-8')
marker = 'def create_or_update_export_sale'
idx = text.find(marker)
if idx < 0:
    raise SystemExit('marker not found')
tail = text[idx:]

header = r'''"""Lap phieu xuat khau 2 buoc: xuat kho ra cang (157) -> thong quan (632/157 + DT)."""
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
    """Chi buoc 1 (xuat kho ra cang). Thong quan: confirm_export_clearance."""
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
        f'Xuat kho ra cang (cho thong quan) — {sale_no}',
        sale_no, 'export_ship', 'Xuat noi bo', unit or 'Cai',
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
    """Xac nhan thong quan + B/L -> GV 632/157 + DT 131/511/3333."""
    ensure_export_sale_schema(conn, commit=False)
    data = data or {}
    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError(f'Khong tim thay phieu ban #{sale_id}')
    s = dict(sale)
    if str(s.get('sale_type') or '').upper() != 'EXPORT':
        raise ValueError('Khong phai phieu xuat khau')

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
        raise ValueError('Thieu so to khai hai quan da thong quan')
    if not str(s.get('bl_no') or '').strip():
        raise ValueError('Thieu so Bill of Lading (B/L)')
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
        'message': 'Da thong quan: No 632/Co 157 va No 131/Co 511. Co the xuat HDDT.',
    }


'''

# Fix Vietnamese messages in header (ASCII placeholders above are fine for logic;
# replace key user-facing strings with proper Vietnamese after write)
header = header.replace(
    '"""Lap phieu xuat khau 2 buoc: xuat kho ra cang (157) -> thong quan (632/157 + DT)."""',
    '"""Lập phiếu xuất khẩu 2 bước: xuất kho ra cảng (157) → thông quan (632/157 + DT)."""',
)
header = header.replace(
    '"""Chi buoc 1 (xuat kho ra cang). Thong quan: confirm_export_clearance."""',
    '"""Chỉ bước 1 (xuất kho ra cảng). Thông quan: confirm_export_clearance."""',
)
header = header.replace(
    "f'Xuat kho ra cang (cho thong quan) — {sale_no}'",
    "f'Xuất kho ra cảng (chờ thông quan) — {sale_no}'",
)
header = header.replace("'Xuat noi bo'", "'Xuất nội bộ'")
header = header.replace("unit or 'Cai'", "unit or 'Cái'")
header = header.replace(
    '"""Xac nhan thong quan + B/L -> GV 632/157 + DT 131/511/3333."""',
    '"""Xác nhận thông quan + B/L → GV 632/157 + DT 131/511/3333."""',
)
header = header.replace(
    "raise ValueError(f'Khong tim thay phieu ban #{sale_id}')",
    "raise ValueError(f'Không tìm thấy phiếu bán #{sale_id}')",
)
header = header.replace(
    "raise ValueError('Khong phai phieu xuat khau')",
    "raise ValueError('Không phải phiếu xuất khẩu')",
)
header = header.replace(
    "raise ValueError('Thieu so to khai hai quan da thong quan')",
    "raise ValueError('Thiếu số tờ khai hải quan đã thông quan')",
)
header = header.replace(
    "raise ValueError('Thieu so Bill of Lading (B/L)')",
    "raise ValueError('Thiếu số Bill of Lading (B/L)')",
)
header = header.replace(
    "'message': 'Da thong quan: No 632/Co 157 va No 131/Co 511. Co the xuat HDDT.'",
    "'message': 'Đã thông quan: Nợ 632/Có 157 và Nợ 131/Có 511. Có thể xuất HĐĐT trong 01 ngày làm việc.'",
)

p.write_text(header + '\n' + tail, encoding='utf-8')
print('OK bytes', p.stat().st_size)
