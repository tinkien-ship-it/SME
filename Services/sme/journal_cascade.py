"""Cascade xóa chứng từ ↔ bút toán SME (PT/PC, phiếu xuất 02-VT, FK nguồn)."""
from __future__ import annotations

import sqlite3
from typing import Any
from db_utils import sqlite_commit


# Bút toán gắn phiếu xuất kho 02-VT (phieu_xuat_kho.sale_id = document_id)
STOCK_OUT_JOURNAL_TYPES = frozenset({
    'EXPORT_SHIP',
    'SALE',
    'SALE_COGS',
    'SALE_REVENUE',
})

# Thông quan XK — không cho xóa PX khi còn các bút toán này
EXPORT_CLEARANCE_TYPES = frozenset({
    'EXPORT_REVENUE', 'EXPORT_COGS', 'EXPORT_TAX',
})


def _row(r) -> dict[str, Any]:
    return dict(r) if r is not None else {}


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except sqlite3.Error:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _nullify_journal_fks(conn: sqlite3.Connection, entry_id: int) -> list[str]:
    """Gỡ FK journal_entry_id / settle_journal_id trỏ tới bút toán đã xóa."""
    cleared: list[str] = []
    eid = int(entry_id)

    sale_ids_settle: list[int] = []
    if _table_exists(conn, 'sale') and 'settle_journal_id' in _table_cols(conn, 'sale'):
        sale_ids_settle = [
            int(r[0]) for r in conn.execute(
                'SELECT id FROM sale WHERE settle_journal_id = ?', (eid,),
            ).fetchall()
        ]

    settle_fc = 0.0
    if sale_ids_settle:
        if _table_exists(conn, 'sme_vouchers'):
            try:
                vrow = conn.execute(
                    """
                    SELECT COALESCE(amount_fc, 0)
                    FROM sme_vouchers WHERE journal_entry_id = ? LIMIT 1
                    """,
                    (eid,),
                ).fetchone()
                if vrow:
                    settle_fc = float(vrow[0] or 0)
            except sqlite3.Error:
                pass
        if settle_fc <= 0:
            try:
                crow = conn.execute(
                    """
                    SELECT COALESCE(SUM(credit_fc), 0)
                    FROM sme_journal_lines
                    WHERE entry_id = ? AND account_code LIKE '131%'
                    """,
                    (eid,),
                ).fetchone()
                if crow:
                    settle_fc = float(crow[0] or 0) or settle_fc
            except sqlite3.Error:
                pass

    targets: list[tuple[str, str]] = [
        ('sale', 'settle_journal_id'),
        ('import', 'settle_journal_id'),
        ('import', 'receive_journal_id'),
        ('sme_lc_docs', 'settle_journal_id'),
        ('sme_lc_docs', 'journal_entry_id'),
        ('sme_payroll_runs', 'journal_entry_id'),
        ('sme_material_allocations', 'journal_entry_id'),
        ('sme_stock_counts', 'journal_entry_id'),
        ('sme_advance_docs', 'journal_entry_id'),
        ('production_orders', 'journal_entry_id'),
        ('sme_production_cost_entries', 'journal_entry_id'),
        ('sme_loans', 'journal_entry_id'),
        ('sme_deposits', 'journal_entry_id'),
        ('sme_cash_counts', 'journal_entry_id'),
        ('sme_fx_revaluations', 'journal_entry_id'),
        ('sme_cit_docs', 'journal_entry_id'),
        ('sme_fa_docs', 'journal_entry_id'),
        ('sme_fa_disposals', 'journal_entry_id'),
        ('sme_labor_sheets', 'journal_entry_id'),
        ('sme_export_costs', 'journal_entry_id'),
        ('sme_export_doc_discounts', 'journal_entry_id'),
        ('sme_lc_settlements', 'journal_entry_id'),
    ]
    for table, col in targets:
        if not _table_exists(conn, table):
            continue
        cols = _table_cols(conn, table)
        if col not in cols:
            continue
        try:
            cur = conn.execute(
                f'UPDATE "{table}" SET {col} = NULL WHERE {col} = ?',
                (eid,),
            )
            if cur.rowcount:
                cleared.append(f'{table}.{col}')
        except sqlite3.Error:
            pass

    if sale_ids_settle:
        scols = _table_cols(conn, 'sale')
        for sid in sale_ids_settle:
            sets: list[str] = []
            vals: list[Any] = []
            # settle_journal_id đã NULL ở vòng targets; chỉ chỉnh số NT đã thu
            if 'settle_amount_fc' in scols and settle_fc > 0:
                sets.append(
                    """settle_amount_fc = CASE
                        WHEN COALESCE(settle_amount_fc, 0) - ? < 0 THEN 0
                        ELSE COALESCE(settle_amount_fc, 0) - ?
                    END"""
                )
                vals.extend([settle_fc, settle_fc])
            if 'ar_status' in scols:
                sets.append(
                    "ar_status = CASE WHEN COALESCE(ar_status,'') = 'settled' "
                    "THEN 'open' ELSE ar_status END"
                )
            if sets:
                vals.append(sid)
                try:
                    conn.execute(
                        f"UPDATE sale SET {', '.join(sets)} WHERE id = ?",
                        vals,
                    )
                except sqlite3.Error:
                    pass
            # cong_no: reverse_ar_receipt chạy trong _delete_vouchers_for_journal
    return cleared

def _delete_vouchers_for_journal(
    conn: sqlite3.Connection, entry_id: int,
) -> list[dict[str, Any]]:
    """Xóa sme_vouchers gắn journal (không gọi lại reverse/delete journal)."""
    out: list[dict[str, Any]] = []
    if not _table_exists(conn, 'sme_vouchers'):
        return out
    rows = conn.execute(
        """
        SELECT id, voucher_no, voucher_type, form_code, source_type, source_id, amount
        FROM sme_vouchers WHERE journal_entry_id = ?
        """,
        (int(entry_id),),
    ).fetchall()
    for r in rows:
        v = _row(r)
        vid = int(v['id'])
        try:
            conn.execute(
                'DELETE FROM sme_insurance_pay_alloc WHERE voucher_id = ?', (vid,),
            )
        except sqlite3.Error:
            pass
        # Hoàn công nợ nếu PT thu 131
        try:
            full = conn.execute(
                'SELECT * FROM sme_vouchers WHERE id = ?', (vid,),
            ).fetchone()
            if full:
                fd = _row(full)
                if (
                    fd.get('voucher_type') == 'receipt'
                    and fd.get('source_id')
                    and str(fd.get('credit_account') or '').startswith('131')
                ):
                    from Services.sme.cong_no_ops import reverse_ar_receipt
                    reverse_ar_receipt(conn, int(fd['source_id']), float(fd.get('amount') or 0))
        except sqlite3.Error:
            pass
        conn.execute('DELETE FROM sme_vouchers WHERE id = ?', (vid,))
        out.append(v)
    return out


def _delete_phieu_xuat_for_sale(
    conn: sqlite3.Connection, sale_id: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not _table_exists(conn, 'phieu_xuat_kho') or not sale_id:
        return out
    rows = conn.execute(
        'SELECT id, voucher_no, sale_id, total_amount FROM phieu_xuat_kho WHERE sale_id = ?',
        (int(sale_id),),
    ).fetchall()
    for r in rows:
        d = _row(r)
        conn.execute('DELETE FROM phieu_xuat_kho WHERE id = ?', (int(d['id']),))
        out.append(d)
    return out


def _remaining_journals(
    conn: sqlite3.Connection,
    document_id: int,
    doc_types: frozenset[str] | set[str],
    *,
    exclude_entry_id: int | None = None,
) -> list[int]:
    if not document_id or not doc_types:
        return []
    ph = ','.join('?' * len(doc_types))
    params: list[Any] = [int(document_id), *doc_types]
    sql = f"""
        SELECT id FROM sme_journal_entries
        WHERE document_id = ?
          AND UPPER(COALESCE(document_type,'')) IN ({ph})
          AND status = 'posted'
          AND reverses_id IS NULL
    """
    if exclude_entry_id:
        sql += ' AND id != ?'
        params.append(int(exclude_entry_id))
    return [int(r[0]) for r in conn.execute(sql, params).fetchall()]


def _revert_export_ship_stock(conn: sqlite3.Connection, sale_id: int) -> int:
    """Xóa stock_moves EXPORT_SHIP của phiếu XK và sync tồn."""
    if not sale_id or not _table_exists(conn, 'stock_moves'):
        return 0
    pids = [
        int(r[0]) for r in conn.execute(
            """
            SELECT DISTINCT product_id FROM stock_moves
            WHERE ref_id = ? AND UPPER(type) IN ('EXPORT_SHIP', 'SALE')
            """,
            (int(sale_id),),
        ).fetchall()
        if r[0]
    ]
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM stock_moves
        WHERE ref_id = ? AND UPPER(type) IN ('EXPORT_SHIP', 'SALE')
        """,
        (int(sale_id),),
    )
    n = cur.rowcount
    try:
        from Services.sme.inventory_ops import sync_inventory_quantity_from_moves
        for pid in pids:
            sync_inventory_quantity_from_moves(cur, pid)
    except Exception:
        pass
    return int(n or 0)


def _revert_domestic_sale_stock(conn: sqlite3.Connection, sale_id: int) -> None:
    try:
        from Services.inventory_stock_helpers import revert_sale_stock
        revert_sale_stock(conn.cursor(), int(sale_id))
    except Exception:
        pass


def cleanup_documents_for_deleted_journal(
    conn: sqlite3.Connection,
    entry: sqlite3.Row | dict,
) -> dict[str, Any]:
    """Gọi *trước* khi hard-delete bút toán — xóa chứng từ / gỡ FK liên quan."""
    e = dict(entry) if not isinstance(entry, dict) else entry
    eid = int(e['id'])
    dtype = str(e.get('document_type') or '').upper()
    try:
        doc_id = int(e['document_id']) if e.get('document_id') not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        doc_id = None

    result: dict[str, Any] = {
        'entry_id': eid,
        'document_type': dtype,
        'document_id': doc_id,
        'vouchers_deleted': [],
        'stock_out_deleted': [],
        'fk_cleared': [],
        'stock_moves_deleted': 0,
    }

    # Không gỡ xuất kho ra cảng khi vẫn còn bút toán thông quan
    if dtype == 'EXPORT_SHIP' and doc_id:
        clr = _remaining_journals(conn, doc_id, EXPORT_CLEARANCE_TYPES)
        if clr:
            raise ValueError(
                'Không xóa bút toán xuất kho ra cảng khi còn bút toán thông quan (DT/GV). '
                'Hãy xóa các bút toán thông quan trên nhật ký trước.'
            )

    result['vouchers_deleted'] = _delete_vouchers_for_journal(conn, eid)
    result['fk_cleared'] = _nullify_journal_fks(conn, eid)

    # Phiếu xuất kho 02-VT + kho khi xóa bút toán xuất kho / GV bán
    if dtype in STOCK_OUT_JOURNAL_TYPES and doc_id:
        others = _remaining_journals(
            conn, doc_id, STOCK_OUT_JOURNAL_TYPES, exclude_entry_id=eid,
        )
        if not others:
            result['stock_out_deleted'] = _delete_phieu_xuat_for_sale(conn, doc_id)
            if dtype == 'EXPORT_SHIP':
                result['stock_moves_deleted'] = _revert_export_ship_stock(conn, doc_id)
                # Đưa phiếu XK về trạng thái chưa xuất kho nếu còn sale
                if _table_exists(conn, 'sale'):
                    scols = _table_cols(conn, 'sale')
                    sets = []
                    if 'export_status' in scols:
                        sets.append("export_status = NULL")
                    if 'status' in scols:
                        sets.append("status = 'draft'")
                    if sets:
                        try:
                            conn.execute(
                                f"UPDATE sale SET {', '.join(sets)} WHERE id = ?",
                                (doc_id,),
                            )
                        except sqlite3.Error:
                            pass
            elif dtype in ('SALE', 'SALE_COGS', 'SALE_REVENUE'):
                _revert_domestic_sale_stock(conn, doc_id)
                result['stock_moves_deleted'] = -1  # reverted via helper

    # Xóa PT settle → reset settle_amount trên sale (đã nullify settle_journal_id)
    if dtype in ('PT', 'EXPORT_SETTLE') and doc_id and _table_exists(conn, 'sale'):
        scols = _table_cols(conn, 'sale')
        if 'settle_amount_fc' in scols:
            try:
                conn.execute(
                    """
                    UPDATE sale
                    SET settle_amount_fc = 0,
                        ar_status = CASE
                            WHEN COALESCE(ar_status,'') = 'settled' THEN 'open'
                            ELSE ar_status
                        END
                    WHERE id = ? AND settle_journal_id IS NULL
                    """,
                    (doc_id,),
                )
            except sqlite3.Error:
                pass

    # Xóa DT/GV thông quan → lùi export_status về shipped nếu hết clearance
    if dtype in EXPORT_CLEARANCE_TYPES and doc_id:
        left = _remaining_journals(
            conn, doc_id, EXPORT_CLEARANCE_TYPES, exclude_entry_id=eid,
        )
        if not left and _table_exists(conn, 'sale'):
            scols = _table_cols(conn, 'sale')
            if 'export_status' in scols:
                try:
                    conn.execute(
                        "UPDATE sale SET export_status = 'shipped' WHERE id = ?",
                        (doc_id,),
                    )
                except sqlite3.Error:
                    pass

    return result


def delete_stock_out_voucher(
    conn: sqlite3.Connection,
    voucher_id: int,
    *,
    reason: str = 'Xóa phiếu xuất kho 02-VT',
    deleted_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Xóa phiếu xuất kho 02-VT và các bút toán / stock liên quan."""
    from Services.sme.journal_engine import delete_journal_entry

    if not _table_exists(conn, 'phieu_xuat_kho'):
        raise ValueError('Không có bảng phiếu xuất kho')
    row = conn.execute(
        'SELECT * FROM phieu_xuat_kho WHERE id = ?', (int(voucher_id),),
    ).fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy phiếu xuất kho #{voucher_id}')
    px = _row(row)
    sale_id = px.get('sale_id')
    try:
        sale_id = int(sale_id) if sale_id not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        sale_id = None

    journals_deleted: list[dict[str, Any]] = []
    stock_moves = 0

    if sale_id:
        # Không xóa PX nếu đã thông quan XK
        blocked = _remaining_journals(conn, sale_id, EXPORT_CLEARANCE_TYPES)
        if blocked:
            raise ValueError(
                'Phiếu XK đã có bút toán thông quan (DT/GV). '
                'Hãy xóa các bút toán thông quan trên nhật ký trước, '
                'hoặc hủy thông quan rồi mới xóa phiếu xuất kho 02-VT.'
            )

        # Xóa bút toán xuất kho / GV·DT bán gắn sale
        jids = _remaining_journals(conn, sale_id, STOCK_OUT_JOURNAL_TYPES)
        for jid in jids:
            try:
                jr = delete_journal_entry(
                    conn, jid, reason=reason, deleted_by=deleted_by,
                )
                journals_deleted.append({
                    'id': jid,
                    'entry_no': (jr.get('snapshot') or {}).get('entry_no'),
                    'mode': jr.get('mode'),
                })
            except ValueError as exc:
                # Kỳ khóa → không xóa cứng
                raise ValueError(
                    f'Không xóa được bút toán #{jid} liên quan phiếu xuất: {exc}'
                ) from exc

        # Nếu journal cascade đã xóa PX + stock thì kiểm tra lại
        still = conn.execute(
            'SELECT id FROM phieu_xuat_kho WHERE id = ?', (int(voucher_id),),
        ).fetchone()
        if still:
            # Journal không có / cascade không đụng PX → xóa PX + hoàn kho
            sale = None
            if _table_exists(conn, 'sale'):
                sale = conn.execute(
                    'SELECT sale_type FROM sale WHERE id = ?', (sale_id,),
                ).fetchone()
            is_export = bool(
                sale and str(
                    sale[0] if not isinstance(sale, sqlite3.Row) else sale['sale_type']
                ).upper() == 'EXPORT'
            )
            if is_export:
                stock_moves = _revert_export_ship_stock(conn, sale_id)
            else:
                _revert_domestic_sale_stock(conn, sale_id)
            conn.execute('DELETE FROM phieu_xuat_kho WHERE id = ?', (int(voucher_id),))
    else:
        # Phiếu thủ công không gắn sale
        conn.execute('DELETE FROM phieu_xuat_kho WHERE id = ?', (int(voucher_id),))

    # Đảm bảo không còn bản ghi
    conn.execute('DELETE FROM phieu_xuat_kho WHERE id = ?', (int(voucher_id),))

    if commit:
        sqlite_commit(conn, label='journal_cascade')

    return {
        'success': True,
        'deleted': True,
        'voucher_id': int(voucher_id),
        'voucher_no': px.get('voucher_no'),
        'sale_id': sale_id,
        'journals_deleted': journals_deleted,
        'stock_moves_deleted': stock_moves,
        'message': (
            f"Đã xóa phiếu xuất kho 02-VT {px.get('voucher_no')}"
            + (
                f" và {len(journals_deleted)} bút toán liên quan"
                if journals_deleted else ''
            )
        ),
    }
