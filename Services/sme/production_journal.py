"""Hạch toán sản xuất SME — giá thành TT99: 621/622/627 → 154 → 155."""
from __future__ import annotations

import sqlite3
from typing import Any

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
        conn.commit()


def _link_step(conn: sqlite3.Connection, order_id: int, step: str, journal_entry_id: int) -> None:
    conn.execute(
        """
        INSERT INTO sme_production_cost_entries (order_id, step, journal_entry_id)
        VALUES (?, ?, ?)
        ON CONFLICT(order_id, step) DO UPDATE SET journal_entry_id = excluded.journal_entry_id
        """,
        (order_id, step, journal_entry_id),
    )


def post_production_journal(
    conn: sqlite3.Connection,
    order: dict[str, Any],
    *,
    created_by: str | None = None,
    costing_mode: str | None = None,
    commit: bool = False,
) -> dict[str, Any] | None:
    """
    Mặc định ``full`` (TT99):
      1) Tập hợp CP: Nợ 621/Có 152 · Nợ 622/Có 3341 · Nợ 627/Có 1111
      2) Kết chuyển dở dang: Nợ 154 / Có 621+622+627
      3) Nhập thành phẩm: Nợ 155 / Có 154

    ``simple`` (siêu nhỏ): Nợ 155 / Có 152(+3341/+1111) — giữ tương thích cũ.
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

    mode = (costing_mode or order.get('costing_mode') or 'full').strip().lower()
    if mode not in ('full', 'simple'):
        mode = 'full'

    if mode == 'simple':
        result = _post_simple(
            conn, order_id=order_id, voucher_no=voucher_no, date_s=date_s,
            total=total, credit_mat=credit_mat, credit_labor=credit_labor,
            credit_other=credit_other, finished_product_id=order.get('finished_product_id'),
            created_by=created_by,
        )
    else:
        result = _post_full(
            conn, order_id=order_id, voucher_no=voucher_no, date_s=date_s,
            total=total, credit_mat=credit_mat, credit_labor=credit_labor,
            credit_other=credit_other, finished_product_id=order.get('finished_product_id'),
            created_by=created_by,
        )

    conn.execute(
        'UPDATE production_orders SET journal_entry_id = ?, costing_mode = ? WHERE id = ?',
        (result['journal_entry_id'], mode, order_id),
    )
    if commit:
        conn.commit()
    result['costing_mode'] = mode
    result['voucher_no'] = voucher_no
    result['total_cost'] = total
    return result


def _post_simple(
    conn, *, order_id, voucher_no, date_s, total, credit_mat, credit_labor, credit_other,
    finished_product_id, created_by,
) -> dict[str, Any]:
    desc = f"Sản xuất {voucher_no} — nhập thành phẩm (đơn giản)"
    lines: list[dict] = [{
        'sequence': 1, 'account_code': '155', 'debit': total, 'credit': 0,
        'product_id': finished_product_id, 'description': desc,
    }]
    seq = 2
    if credit_mat > 0:
        lines.append({
            'sequence': seq, 'account_code': '152', 'debit': 0, 'credit': credit_mat,
            'description': f'{voucher_no}: xuất NVL',
        })
        seq += 1
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
        document_type='SX', document_no=voucher_no, document_id=order_id,
        business_type='SAN_XUAT_TP', description=desc,
        reference_document=voucher_no, created_by=created_by, lines=lines,
    )
    _link_step(conn, order_id, 'fg', entry['id'])
    return {'journal_entry_id': entry['id'], 'entry_no': entry.get('entry_no'), 'steps': ['fg']}


def _post_full(
    conn, *, order_id, voucher_no, date_s, total, credit_mat, credit_labor, credit_other,
    finished_product_id, created_by,
) -> dict[str, Any]:
    """Ba bước: tập hợp CP → 154 → 155."""
    steps = []

    # 1) Tập hợp chi phí sản xuất
    collect_lines: list[dict] = []
    seq = 1
    if credit_mat > 0:
        collect_lines.extend([
            {'sequence': seq, 'account_code': '621', 'debit': credit_mat, 'credit': 0,
             'description': f'{voucher_no}: NVL trực tiếp'},
            {'sequence': seq + 1, 'account_code': '152', 'debit': 0, 'credit': credit_mat,
             'description': f'{voucher_no}: xuất NVL'},
        ])
        seq += 2
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

    # 2) Kết chuyển sang 154
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

    # 3) Nhập thành phẩm
    fg = post_journal_entry(
        conn, posting_date=date_s, document_date=date_s,
        document_type='SX', document_no=voucher_no, document_id=order_id,
        business_type='SAN_XUAT_TP', description=f'Nhập thành phẩm {voucher_no}',
        reference_document=voucher_no, created_by=created_by,
        lines=[
            {
                'sequence': 1, 'account_code': '155', 'debit': total, 'credit': 0,
                'product_id': finished_product_id,
                'description': f'Nhập TP {voucher_no}',
            },
            {
                'sequence': 2, 'account_code': '154', 'debit': 0, 'credit': total,
                'product_id': finished_product_id,
                'description': f'Xuất 154 → 155 {voucher_no}',
            },
        ],
    )
    _link_step(conn, order_id, 'fg', fg['id'])
    steps.append('fg')

    return {
        'journal_entry_id': fg['id'],
        'entry_no': fg.get('entry_no'),
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
    """Đảo toàn bộ bút toán giá thành gắn lệnh SX (collect/wip/fg)."""
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
        # Legacy: chỉ có journal_entry_id trên order
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
