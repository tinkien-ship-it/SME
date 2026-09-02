"""Hạch toán sản xuất SME — giá thành TT99: 621/622/627 → 154; nhập TP 155 khi nhập kho."""
from __future__ import annotations

import sqlite3
from typing import Any

from db_utils import sqlite_commit
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    reverse_journal_entry,
)


def ensure_production_journal_column(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    cols = {r[1] for r in conn.execute('PRAGMA table_info(production_orders)').fetchall()}
    if 'journal_entry_id' not in cols:
        conn.execute('ALTER TABLE production_orders ADD COLUMN journal_entry_id INTEGER')
    if 'costing_mode' not in cols:
        conn.execute("ALTER TABLE production_orders ADD COLUMN costing_mode TEXT DEFAULT 'full'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_production_cost_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            step TEXT NOT NULL,
            journal_entry_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(order_id, step)
        )
        """
    )
    if commit:
        sqlite_commit(conn, label='production_journal')


def _link_step(conn: sqlite3.Connection, order_id: int, step: str, journal_entry_id: int) -> None:
    conn.execute(
        """
        INSERT INTO sme_production_cost_entries (order_id, step, journal_entry_id)
        VALUES (?, ?, ?)
        ON CONFLICT(order_id, step) DO UPDATE SET journal_entry_id = excluded.journal_entry_id
        """,
        (order_id, step, journal_entry_id),
    )


def _material_cost_buckets(
    conn: sqlite3.Connection,
    order: dict[str, Any],
    *,
    fallback_total: float = 0,
) -> dict[str, float]:
    """Gom chi phí NVL theo TK kho 152/155/156 từ dòng phiếu SX."""
    from Services.sme.inventory_ops import inventory_account_for_product

    materials = order.get('materials') or []
    if not materials and order.get('id'):
        rows = conn.execute(
            """
            SELECT material_product_id, total_cost
            FROM production_order_materials
            WHERE order_id = ?
            """,
            (int(order['id']),),
        ).fetchall()
        materials = [
            {'material_product_id': r[0], 'total_cost': r[1]}
            for r in rows
        ]

    buckets: dict[str, float] = {}
    for m in materials:
        cost = float(m.get('total_cost') or 0)
        if cost <= 0:
            continue
        pid = int(m['material_product_id'])
        acc = m.get('inventory_account') or inventory_account_for_product(conn, pid)
        buckets[str(acc)] = round(buckets.get(str(acc), 0) + cost, 2)

    if not buckets and fallback_total > 0:
        buckets['152'] = round(float(fallback_total), 2)
    return buckets


def _append_inv_credit_lines(
    lines: list[dict],
    *,
    seq: int,
    buckets: dict[str, float],
    voucher_no: str,
    label: str = 'xuất NVL',
) -> int:
    for acc in sorted(buckets.keys()):
        amt = float(buckets[acc] or 0)
        if amt <= 0:
            continue
        lines.append({
            'sequence': seq,
            'account_code': acc,
            'debit': 0,
            'credit': amt,
            'description': f'{voucher_no}: {label} ({acc})',
        })
        seq += 1
    return seq


def post_production_journal(
    conn: sqlite3.Connection,
    order: dict[str, Any],
    *,
    created_by: str | None = None,
    costing_mode: str | None = None,
    commit: bool = False,
) -> dict[str, Any] | None:
    """
    Khi lập lệnh SX (chưa nhập kho TP):

    ``full`` (TT99):
      1) Tập hợp CP: Nợ 621/Có 152|155|156 · Nợ 622/Có 3341 · Nợ 627/Có 1111
      2) Kết chuyển dở dang: Nợ 154 / Có 621+622+627
      (Bước Nợ 155 chỉ khi nhập kho thành phẩm — ``post_fg_receipt_journal``)

    ``materials_only`` (giá tạm giữa kỳ):
      Chỉ NVL: Nợ 621/Có kho → Nợ 154/Có 621.
      NCTT/CPSXC định mức chỉ dùng để định giá nhập 155; cuối kỳ phân bổ thực tế 622/627.

    ``simple`` (siêu nhỏ): Nợ 154 / Có 152|155|156 (+3341/+1111) — chờ nhập kho mới chuyển 155.
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

    voucher_no = order.get('voucher_no') or f'SX{order_id}'
    date_s = str(order.get('production_date') or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày sản xuất để ghi sổ')

    mode = (costing_mode or order.get('costing_mode') or 'full').strip().lower()
    if mode not in ('full', 'simple', 'materials_only'):
        mode = 'full'

    if mode == 'materials_only':
        if material <= 0:
            return None
        labor = 0.0
        other = 0.0
        total = material
        result = _post_full_to_wip(
            conn, order_id=order_id, voucher_no=voucher_no, date_s=date_s,
            total=total, mat_buckets=_material_cost_buckets(conn, order, fallback_total=material),
            credit_labor=0.0, credit_other=0.0,
            finished_product_id=order.get('finished_product_id'),
            created_by=created_by,
        )
    elif mode == 'simple':
        if total <= 0:
            return None
        credit_mat = material
        credit_labor = labor
        credit_other = other
        credit_sum = credit_mat + credit_labor + credit_other
        if abs(credit_sum - total) >= 0.01:
            credit_mat = round(credit_mat + (total - credit_sum), 2)
        result = _post_simple_wip(
            conn, order_id=order_id, voucher_no=voucher_no, date_s=date_s,
            total=total, mat_buckets=_material_cost_buckets(conn, order, fallback_total=credit_mat),
            credit_labor=credit_labor, credit_other=credit_other,
            finished_product_id=order.get('finished_product_id'),
            created_by=created_by,
        )
    else:
        if total <= 0:
            return None
        credit_mat = material
        credit_labor = labor
        credit_other = other
        credit_sum = credit_mat + credit_labor + credit_other
        if abs(credit_sum - total) >= 0.01:
            credit_mat = round(credit_mat + (total - credit_sum), 2)
        result = _post_full_to_wip(
            conn, order_id=order_id, voucher_no=voucher_no, date_s=date_s,
            total=total, mat_buckets=_material_cost_buckets(conn, order, fallback_total=credit_mat),
            credit_labor=credit_labor, credit_other=credit_other,
            finished_product_id=order.get('finished_product_id'),
            created_by=created_by,
        )

    conn.execute(
        'UPDATE production_orders SET journal_entry_id = ?, costing_mode = ? WHERE id = ?',
        (result['journal_entry_id'], mode, order_id),
    )
    if commit:
        sqlite_commit(conn, label='production_journal')
    result['costing_mode'] = mode
    result['voucher_no'] = voucher_no
    result['total_cost'] = total
    return result


def post_fg_receipt_journal(
    conn: sqlite3.Connection,
    order: dict[str, Any],
    receipt: dict[str, Any],
    *,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any] | None:
    """Nhập kho TP theo đợt: Nợ 155 / Có 154 = amount (ngày nhập kho)."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_production_journal_column(conn, commit=False)

    receipt_id = int(receipt['id'])
    step = f'fg:{receipt_id}'
    existing = conn.execute(
        """
        SELECT journal_entry_id FROM sme_production_cost_entries
        WHERE order_id = ? AND step = ?
        """,
        (int(order['id']), step),
    ).fetchone()
    if existing:
        jid = existing[0] if not isinstance(existing, sqlite3.Row) else existing['journal_entry_id']
        return {'skipped': True, 'journal_entry_id': jid, 'reason': 'already_posted'}

    amount = float(receipt.get('amount') or 0)
    if amount <= 0:
        return None

    voucher_no = order.get('voucher_no') or f"SX{order['id']}"
    receipt_no = receipt.get('receipt_no') or f'NTP{receipt_id}'
    date_s = str(receipt.get('receipt_date') or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày nhập kho thành phẩm')

    qty = float(receipt.get('qty') or 0)
    desc = f"Nhập TP {voucher_no}/{receipt_no} — SL {qty:g}"
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='SX',
        document_no=receipt_no,
        document_id=receipt_id,
        business_type='NHAP_TP_SX',
        description=desc,
        reference_document=voucher_no,
        created_by=created_by,
        lines=[
            {
                'sequence': 1,
                'account_code': '155',
                'debit': amount,
                'credit': 0,
                'product_id': order.get('finished_product_id'),
                'description': desc,
            },
            {
                'sequence': 2,
                'account_code': '154',
                'debit': 0,
                'credit': amount,
                'product_id': order.get('finished_product_id'),
                'description': f'Xuất 154 → 155 {receipt_no}',
            },
        ],
    )
    _link_step(conn, int(order['id']), step, entry['id'])
    conn.execute(
        'UPDATE production_fg_receipts SET journal_entry_id = ? WHERE id = ?',
        (entry['id'], receipt_id),
    )
    if commit:
        sqlite_commit(conn, label='production_journal')
    return {
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'amount': amount,
        'step': step,
    }


def _post_simple_wip(
    conn, *, order_id, voucher_no, date_s, total, mat_buckets, credit_labor, credit_other,
    finished_product_id, created_by,
) -> dict[str, Any]:
    """Siêu nhỏ: tập hợp thẳng vào 154 (chờ nhập kho mới sang 155)."""
    credit_mat = round(sum(float(v or 0) for v in mat_buckets.values()), 2)
    desc = f"Sản xuất {voucher_no} — CPSX dở dang (154)"
    lines: list[dict] = [{
        'sequence': 1, 'account_code': '154', 'debit': total, 'credit': 0,
        'product_id': finished_product_id, 'description': desc,
    }]
    seq = 2
    if credit_mat > 0:
        seq = _append_inv_credit_lines(
            lines, seq=seq, buckets=mat_buckets, voucher_no=voucher_no,
        )
    if credit_labor > 0:
        lines.append({
            'sequence': seq, 'account_code': '3341', 'debit': 0, 'credit': credit_labor,
            'description': f'{voucher_no}: nhân công SX',
        })
        seq += 1
    if credit_other > 0:
        lines.append({
            'sequence': seq, 'account_code': '1111', 'debit': 0, 'credit': credit_other,
            'description': f'{voucher_no}: chi phí SX khác',
        })
    entry = post_journal_entry(
        conn, posting_date=date_s, document_date=date_s,
        document_type='SX154', document_no=f'{voucher_no}-154', document_id=order_id,
        business_type='KET_CHUYEN_154', description=desc,
        reference_document=voucher_no, created_by=created_by, lines=lines,
    )
    _link_step(conn, order_id, 'wip', entry['id'])
    return {'journal_entry_id': entry['id'], 'entry_no': entry.get('entry_no'), 'steps': ['wip']}


def _post_full_to_wip(
    conn, *, order_id, voucher_no, date_s, total, mat_buckets, credit_labor, credit_other,
    finished_product_id, created_by,
) -> dict[str, Any]:
    """Hai bước khi lập lệnh: tập hợp CP → 154. Chưa ghi 155."""
    credit_mat = round(sum(float(v or 0) for v in mat_buckets.values()), 2)
    steps = []

    collect_lines: list[dict] = []
    seq = 1
    if credit_mat > 0:
        collect_lines.append({
            'sequence': seq, 'account_code': '621', 'debit': credit_mat, 'credit': 0,
            'description': f'{voucher_no}: NVL trực tiếp',
        })
        seq += 1
        seq = _append_inv_credit_lines(
            collect_lines, seq=seq, buckets=mat_buckets, voucher_no=voucher_no,
        )
    if credit_labor > 0:
        collect_lines.extend([
            {'sequence': seq, 'account_code': '622', 'debit': credit_labor, 'credit': 0,
             'description': f'{voucher_no}: nhân công trực tiếp'},
            {'sequence': seq + 1, 'account_code': '3341', 'debit': 0, 'credit': credit_labor,
             'description': f'{voucher_no}: phải trả NC'},
        ])
        seq += 2
    if credit_other > 0:
        collect_lines.extend([
            {'sequence': seq, 'account_code': '627', 'debit': credit_other, 'credit': 0,
             'description': f'{voucher_no}: CPSX chung'},
            {'sequence': seq + 1, 'account_code': '1111', 'debit': 0, 'credit': credit_other,
             'description': f'{voucher_no}: chi phí chung'},
        ])
        seq += 2

    if not collect_lines:
        raise ValueError('Không có chi phí để tập hợp giá thành')

    collect = post_journal_entry(
        conn, posting_date=date_s, document_date=date_s,
        document_type='SXCP', document_no=f'{voucher_no}-CP', document_id=order_id,
        business_type='TAP_HOP_CP_SX', description=f'Tập hợp CP SX {voucher_no}',
        reference_document=voucher_no, created_by=created_by, lines=collect_lines,
    )
    _link_step(conn, order_id, 'collect', collect['id'])
    steps.append('collect')

    wip_lines: list[dict] = [{
        'sequence': 1, 'account_code': '154', 'debit': total, 'credit': 0,
        'description': f'Kết chuyển CPSX dở dang {voucher_no}',
        'product_id': finished_product_id,
    }]
    seq = 2
    if credit_mat > 0:
        wip_lines.append({
            'sequence': seq, 'account_code': '621', 'debit': 0, 'credit': credit_mat,
            'description': f'KC 621 → 154 {voucher_no}',
        })
        seq += 1
    if credit_labor > 0:
        wip_lines.append({
            'sequence': seq, 'account_code': '622', 'debit': 0, 'credit': credit_labor,
            'description': f'KC 622 → 154 {voucher_no}',
        })
        seq += 1
    if credit_other > 0:
        wip_lines.append({
            'sequence': seq, 'account_code': '627', 'debit': 0, 'credit': credit_other,
            'description': f'KC 627 → 154 {voucher_no}',
        })

    wip = post_journal_entry(
        conn, posting_date=date_s, document_date=date_s,
        document_type='SX154', document_no=f'{voucher_no}-154', document_id=order_id,
        business_type='KET_CHUYEN_154', description=f'Kết chuyển 154 {voucher_no}',
        reference_document=voucher_no, created_by=created_by, lines=wip_lines,
    )
    _link_step(conn, order_id, 'wip', wip['id'])
    steps.append('wip')

    return {
        'journal_entry_id': wip['id'],
        'entry_no': wip.get('entry_no'),
        'collect_journal_entry_id': collect['id'],
        'wip_journal_entry_id': wip['id'],
        'steps': steps,
    }


def reverse_production_journals(
    conn: sqlite3.Connection,
    order_id: int,
    *,
    reason: str = 'Hủy sản xuất',
    created_by: str | None = None,
) -> list[int]:
    """Đảo toàn bộ bút toán giá thành gắn lệnh SX (collect/wip/fg:…)."""
    ensure_production_journal_column(conn, commit=False)
    rows = conn.execute(
        """
        SELECT step, journal_entry_id FROM sme_production_cost_entries
        WHERE order_id = ? ORDER BY id DESC
        """,
        (order_id,),
    ).fetchall()
    reversed_ids = []
    if rows:
        for r in rows:
            jid = r[1] if not isinstance(r, sqlite3.Row) else r['journal_entry_id']
            if not jid:
                continue
            try:
                reverse_journal_entry(conn, int(jid), created_by=created_by, reason=reason)
                reversed_ids.append(int(jid))
            except ValueError:
                pass
        conn.execute('DELETE FROM sme_production_cost_entries WHERE order_id = ?', (order_id,))
    else:
        row = conn.execute(
            'SELECT journal_entry_id FROM production_orders WHERE id = ?', (order_id,)
        ).fetchone()
        jid = row[0] if row else None
        if jid:
            try:
                reverse_journal_entry(conn, int(jid), created_by=created_by, reason=reason)
                reversed_ids.append(int(jid))
            except ValueError:
                pass
    conn.execute(
        'UPDATE production_orders SET journal_entry_id = NULL WHERE id = ?',
        (order_id,),
    )
    return reversed_ids
