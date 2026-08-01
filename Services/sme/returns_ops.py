"""Hủy chứng từ trả hàng NCC / khách trả hàng — đảo journal + stock_moves."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from Services.inventory_stock_helpers import sync_inventory_quantity_from_moves
from Services.sme.return_import_journal import reverse_return_import_journals


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _ensure_return_import_status(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute('PRAGMA table_info(return_import)').fetchall()}
    if 'status' not in cols:
        try:
            conn.execute(
                "ALTER TABLE return_import ADD COLUMN status TEXT DEFAULT 'posted'"
            )
        except sqlite3.OperationalError:
            pass


def _ensure_return_sales_status(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='return_sales'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in conn.execute('PRAGMA table_info(return_sales)').fetchall()}
    if 'status' not in cols:
        try:
            conn.execute(
                "ALTER TABLE return_sales ADD COLUMN status TEXT DEFAULT 'posted'"
            )
        except sqlite3.OperationalError:
            pass


def _reverse_stock_moves(
    conn: sqlite3.Connection,
    *,
    ref_type: str,
    ref_id: int,
    note: str,
) -> int:
    sm_cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_moves)').fetchall()}
    has_wh = 'warehouse_code' in sm_cols
    # Khách trả: type='RETURN_SALE' / ref_type có thể là 'import'
    moves = conn.execute(
        """
        SELECT * FROM stock_moves
        WHERE ref_id = ?
          AND (
            LOWER(COALESCE(ref_type,'')) = LOWER(?)
            OR LOWER(COALESCE(type,'')) = LOWER(?)
          )
        """,
        (ref_id, ref_type, ref_type),
    ).fetchall()
    when = _now()
    n = 0
    for m in moves:
        md = dict(m)
        qty = float(md.get('quantity') or 0)
        if qty == 0:
            continue
        # Đảo chiều: nhập ↔ xuất
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
                    md['product_id'], when, rev_type, ref_id,
                    md.get('ref_document') or '', ref_type,
                    -qty, note, 'Hủy trả hàng', md.get('cost_price') or 0,
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
                    md['product_id'], when, rev_type, ref_id,
                    md.get('ref_document') or '', ref_type,
                    -qty, note, 'Hủy trả hàng', md.get('cost_price') or 0,
                ),
            )
        try:
            sync_inventory_quantity_from_moves(conn.cursor(), int(md['product_id']))
        except Exception:
            pass
        n += 1
    return n


def void_return_import(
    conn: sqlite3.Connection,
    return_id: int,
    *,
    reason: str = 'Hủy trả NCC',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Hủy một dòng/phiếu return_import: đảo THN + stock_moves RETURN_IMPORT."""
    _ensure_return_import_status(conn)
    row = conn.execute(
        'SELECT * FROM return_import WHERE id = ?', (return_id,)
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy phiếu trả NCC')
    doc = dict(row)
    if str(doc.get('status') or '').lower() == 'void':
        raise ValueError('Phiếu trả NCC đã hủy')

    try:
        from Services.sme.branches import assert_import_in_branch
        if doc.get('import_id'):
            assert_import_in_branch(conn, int(doc['import_id']))
    except ValueError:
        raise
    except Exception:
        pass

    # document_id trên journal có thể là return_import.id hoặc sale_id (checkout)
    doc_ids = {int(return_id)}
    if doc.get('sale_id'):
        try:
            doc_ids.add(int(doc['sale_id']))
        except (TypeError, ValueError):
            pass

    rev_ids: list[int] = []
    for did in doc_ids:
        rev_ids.extend(
            reverse_return_import_journals(
                conn, did, created_by=created_by, reason=reason,
            )
        )

    # stock_moves: thử theo return_import id và sale_id
    moved = 0
    for ref_type in ('RETURN_IMPORT', 'return_import', 'stock_transfer'):
        pass
    for rid in doc_ids:
        moved += _reverse_stock_moves(
            conn, ref_type='RETURN_IMPORT', ref_id=rid, note=reason,
        )
        moved += _reverse_stock_moves(
            conn, ref_type='return_import', ref_id=rid, note=reason,
        )

    # Void linked SME vouchers if any (by reference)
    try:
        from Services.sme.vouchers import void_voucher
        vouchers = conn.execute(
            """
            SELECT id FROM sme_vouchers
            WHERE status != 'void'
              AND (
                (source_type IN ('return_import','RETURN_IMPORT') AND source_id = ?)
                OR reference_document LIKE ?
              )
            """,
            (return_id, f'%TR%{return_id}%'),
        ).fetchall()
        for v in vouchers:
            try:
                void_voucher(
                    conn, int(v[0] if not isinstance(v, sqlite3.Row) else v['id']),
                    reason=reason, created_by=created_by, commit=False,
                )
            except Exception:
                pass
    except Exception:
        pass

    conn.execute(
        "UPDATE return_import SET status = 'void' WHERE id = ?",
        (return_id,),
    )
    if commit:
        conn.commit()
    out = dict(conn.execute(
        'SELECT * FROM return_import WHERE id = ?', (return_id,)
    ).fetchone())
    out['reversed_entry_ids'] = rev_ids
    out['stock_moves_reversed'] = moved
    return out


def void_return_sale(
    conn: sqlite3.Connection,
    return_id: int,
    *,
    reason: str = 'Hủy khách trả hàng',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Hủy return_sales: đảo stock_moves RETURN_SALE + journal THB nếu có."""
    _ensure_return_sales_status(conn)
    row = conn.execute(
        'SELECT * FROM return_sales WHERE id = ?', (return_id,)
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy phiếu khách trả hàng')
    doc = dict(row)
    if str(doc.get('status') or '').lower() == 'void':
        raise ValueError('Phiếu khách trả đã hủy')

    sale_id = doc.get('sale_id')
    try:
        from Services.sme.branches import assert_sale_in_branch
        if sale_id:
            assert_sale_in_branch(conn, int(sale_id))
    except ValueError:
        raise
    except Exception:
        pass

    rev_ids: list[int] = []

    from Services.sme.return_sale_journal import reverse_return_sale_journals
    rev_ids.extend(
        reverse_return_sale_journals(
            conn, return_id, created_by=created_by, reason=reason,
        )
    )

    # Fallback: re-sync sale journals after voiding return adjustments
    if sale_id:
        try:
            from Services.sme.sale_journal import sync_sale_journals
            sync_sale_journals(
                conn, int(sale_id), created_by=created_by, replace_existing=True,
            )
        except Exception:
            pass

    moved = _reverse_stock_moves(
        conn, ref_type='RETURN_SALE', ref_id=return_id, note=reason,
    )
    moved += _reverse_stock_moves(
        conn, ref_type='return_sale', ref_id=return_id, note=reason,
    )
    if sale_id:
        moved += _reverse_stock_moves(
            conn, ref_type='RETURN_SALE', ref_id=int(sale_id), note=reason,
        )

    try:
        from Services.sme.vouchers import void_voucher
        for v in conn.execute(
            """
            SELECT id FROM sme_vouchers
            WHERE status != 'void'
              AND source_type IN ('return_sale','RETURN_SALE')
              AND source_id = ?
            """,
            (return_id,),
        ).fetchall():
            void_voucher(
                conn, int(v[0] if not isinstance(v, sqlite3.Row) else v['id']),
                reason=reason, created_by=created_by, commit=False,
            )
    except Exception:
        pass

    conn.execute(
        "UPDATE return_sales SET status = 'void' WHERE id = ?",
        (return_id,),
    )
    if commit:
        conn.commit()
    out = dict(conn.execute(
        'SELECT * FROM return_sales WHERE id = ?', (return_id,)
    ).fetchone())
    out['reversed_entry_ids'] = rev_ids
    out['stock_moves_reversed'] = moved
    return out
