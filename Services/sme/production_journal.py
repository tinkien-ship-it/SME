"""Hạch toán sản xuất SME — nhập thành phẩm / xuất NVL vào sổ kép (không đụng HKD)."""
from __future__ import annotations

import sqlite3
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry


def ensure_production_journal_column(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    cols = {r[1] for r in conn.execute('PRAGMA table_info(production_orders)').fetchall()}
    if 'journal_entry_id' not in cols:
        conn.execute('ALTER TABLE production_orders ADD COLUMN journal_entry_id INTEGER')
    if commit:
        conn.commit()


def post_production_journal(
    conn: sqlite3.Connection,
    order: dict[str, Any],
    *,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any] | None:
    """
    Bút toán gọn (DN nhỏ):
      Nợ 155 (thành phẩm) = total_cost
        Có 152 = NVL
        Có 3341 = nhân công (nếu > 0)
        Có 1111 = chi phí khác (nếu > 0) — mặc định tiền mặt; 0 thì bỏ
    Nếu labor/other = 0 → chỉ Nợ 155 / Có 152.
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_production_journal_column(conn, commit=False)

    order_id = int(order['id'])
    existing = conn.execute(
        'SELECT journal_entry_id FROM production_orders WHERE id = ?', (order_id,)
    ).fetchone()
    if existing:
        jid = existing[0] if not isinstance(existing, sqlite3.Row) else existing['journal_entry_id']
        if jid:
            return {'skipped': True, 'journal_entry_id': jid, 'reason': 'already_posted'}

    material = float(order.get('total_material_cost') or 0)
    labor = float(order.get('labor_cost') or 0)
    other = float(order.get('other_cost') or 0)
    total = float(order.get('total_cost') or (material + labor + other))
    if total <= 0:
        return None

    # Cân: nếu lệch làm tròn, ưu tiên điều chỉnh Có 152
    credit_mat = material
    credit_labor = labor
    credit_other = other
    credit_sum = credit_mat + credit_labor + credit_other
    if abs(credit_sum - total) >= 0.01:
        credit_mat = round(credit_mat + (total - credit_sum), 2)

    voucher_no = order.get('voucher_no') or f'SX{order_id}'
    date_s = str(order.get('production_date') or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày sản xuất để ghi sổ')

    desc = f"Sản xuất {voucher_no} — nhập thành phẩm"
    lines: list[dict] = [
        {
            'sequence': 1,
            'account_code': '155',
            'debit': total,
            'credit': 0,
            'product_id': order.get('finished_product_id'),
            'description': desc,
        },
    ]
    seq = 2
    if credit_mat > 0:
        lines.append({
            'sequence': seq,
            'account_code': '152',
            'debit': 0,
            'credit': credit_mat,
            'description': f'{voucher_no}: xuất NVL',
        })
        seq += 1
    if credit_labor > 0:
        lines.append({
            'sequence': seq,
            'account_code': '3341',
            'debit': 0,
            'credit': credit_labor,
            'description': f'{voucher_no}: nhân công SX',
        })
        seq += 1
    if credit_other > 0:
        lines.append({
            'sequence': seq,
            'account_code': '1111',
            'debit': 0,
            'credit': credit_other,
            'description': f'{voucher_no}: chi phí SX khác',
        })

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='SX',
        document_no=voucher_no,
        document_id=order_id,
        business_type='SAN_XUAT_TP',
        description=desc,
        reference_document=voucher_no,
        created_by=created_by,
        lines=lines,
    )
    conn.execute(
        'UPDATE production_orders SET journal_entry_id = ? WHERE id = ?',
        (entry['id'], order_id),
    )
    if commit:
        conn.commit()
    return {
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'total_cost': total,
        'voucher_no': voucher_no,
    }
