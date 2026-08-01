"""Hủy phiếu nhập SME — đảo journal PNK + stock_moves."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from Services.inventory_stock_helpers import sync_inventory_quantity_from_moves
from Services.sme.import_journal import reverse_import_journals


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _reverse_import_stock_moves(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    note: str,
) -> int:
    sm_cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_moves)').fetchall()}
    has_wh = 'warehouse_code' in sm_cols
    moves = conn.execute(
        """
        SELECT * FROM stock_moves
        WHERE ref_id = ?
          AND (
            LOWER(COALESCE(ref_type,'')) IN ('import')
            OR LOWER(COALESCE(type,'')) = 'import'
          )
        """,
        (import_id,),
    ).fetchall()
    when = _now()
    n = 0
    for m in moves:
        md = dict(m)
        qty = float(md.get('quantity') or 0)
        if qty == 0:
            continue
        rev_type = 'export' if qty > 0 else 'import'
        if has_wh:
            conn.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type,
                     quantity, note, type1, cost_price, warehouse_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    md['product_id'], when, rev_type, import_id,
                    md.get('ref_document') or '', 'import',
                    -qty, note, 'Hủy phiếu nhập', md.get('cost_price') or 0,
                    md.get('warehouse_code'),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type,
                     quantity, note, type1, cost_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    md['product_id'], when, rev_type, import_id,
                    md.get('ref_document') or '', 'import',
                    -qty, note, 'Hủy phiếu nhập', md.get('cost_price') or 0,
                ),
            )
        try:
            sync_inventory_quantity_from_moves(conn.cursor(), int(md['product_id']))
        except Exception:
            pass
        n += 1
    return n


def void_import(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    reason: str = 'Hủy phiếu nhập',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branches import assert_import_in_branch
    assert_import_in_branch(conn, import_id)

    imp = conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone()
    if not imp:
        raise ValueError('Không tìm thấy phiếu nhập')
    doc = dict(imp)

    ri = conn.execute(
        'SELECT COUNT(*) FROM return_import WHERE import_id = ?', (import_id,)
    ).fetchone()
    if ri and int(ri[0] if not isinstance(ri, sqlite3.Row) else ri[0]) > 0:
        raise ValueError('Phiếu nhập đã phát sinh trả NCC, không thể hủy')

    posting_date = str(doc.get('date') or doc.get('import_date') or '')[:10] or None
    rev_ids = reverse_import_journals(
        conn,
        import_id,
        posting_date=posting_date,
        created_by=created_by,
        reason=reason,
    )
    moved = _reverse_import_stock_moves(conn, import_id, note=reason)

    cols = {r[1] for r in conn.execute('PRAGMA table_info("import")').fetchall()}
    if 'status' in cols:
        conn.execute('UPDATE "import" SET status = ? WHERE id = ?', ('void', import_id))
    elif 'cancelled' in cols:
        conn.execute('UPDATE "import" SET cancelled = 1 WHERE id = ?', (import_id,))
    elif 'note' in cols:
        conn.execute(
            'UPDATE "import" SET note = COALESCE(note,\'\') || ? WHERE id = ?',
            (f' | VOID: {reason}', import_id),
        )

    if commit:
        conn.commit()
    out = dict(conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone())
    out['reversed_entry_ids'] = rev_ids
    out['stock_moves_reversed'] = moved
    return out
