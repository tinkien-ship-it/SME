"""Hủy/xóa phiếu nhập SME — đảo journal + xóa dữ liệu các bảng liên quan."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from Services.inventory_cost import reverse_import_cost
from Services.inventory_stock_helpers import (
    import_base_qty,
    ledger_quantity,
    sync_inventory_quantities,
)
from Services.sme.import_journal import IMPORT_DOCUMENT_TYPE, reverse_journal_entry
from Services.sme.import_transit import DOC_TYPE_RECEIVE, DOC_TYPE_TAX, DOC_TYPE_TRANSIT
from db_utils import sqlite_commit

# Bút toán gắn phiếu nhập (G1 / nộp thuế / nhập kho / quyết toán)
_IMPORT_DOC_TYPES = (
    IMPORT_DOCUMENT_TYPE,  # PNK
    DOC_TYPE_TRANSIT,      # HMDD
    DOC_TYPE_TAX,          # NTHQ
    DOC_TYPE_RECEIVE,      # NKTT
    'TTNCC',               # Thanh toán NCC NK
    'TTLC',                # Tất toán L/C
)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _safe_exec(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> int:
    try:
        cur = conn.execute(sql, params)
        return cur.rowcount if cur.rowcount is not None else 0
    except sqlite3.Error:
        return 0


def _collect_import_journal_ids(conn: sqlite3.Connection, import_id: int, doc: dict) -> list[int]:
    ids: set[int] = set()
    for key in (
        'tax_payment_journal_id',
        'receive_journal_id',
        'settle_journal_id',
    ):
        raw = doc.get(key)
        if raw not in (None, '', 0, '0'):
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                pass

    if _table_exists(conn, 'sme_journal_entries'):
        ph = ','.join('?' * len(_IMPORT_DOC_TYPES))
        rows = conn.execute(
            f"""
            SELECT id FROM sme_journal_entries
            WHERE CAST(document_id AS INTEGER) = ?
              AND document_type IN ({ph})
              AND status = 'posted'
              AND reverses_id IS NULL
            """,
            (import_id, *_IMPORT_DOC_TYPES),
        ).fetchall()
        for r in rows:
            ids.add(int(r[0] if not isinstance(r, sqlite3.Row) else r['id']))

    return sorted(ids)


def _collect_import_voucher_ids(conn: sqlite3.Connection, import_id: int, doc: dict) -> list[int]:
    ids: set[int] = set()
    for key in ('tax_payment_voucher_id', 'settle_voucher_id'):
        raw = doc.get(key)
        if raw not in (None, '', 0, '0'):
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                pass

    if not _table_exists(conn, 'sme_vouchers'):
        return sorted(ids)

    vcols = _cols(conn, 'sme_vouchers')
    sql = """
        SELECT id FROM sme_vouchers
        WHERE status != 'void'
          AND (
            (COALESCE(source_type, '') IN (
                'import', 'import_customs_tax', 'import_settle', 'import_receive'
            ) AND source_id = ?)
    """
    params: list[Any] = [import_id]
    if 'purpose' in vcols:
        sql += """
            OR (COALESCE(purpose, '') IN (
                'customs_tax', 'settle_import_ap', 'settle_import_lc'
            ) AND source_id = ?)
        """
        params.append(import_id)
    sql += ')'
    try:
        for r in conn.execute(sql, params).fetchall():
            ids.add(int(r[0] if not isinstance(r, sqlite3.Row) else r['id']))
    except sqlite3.Error:
        pass
    return sorted(ids)


def _reverse_journals(
    conn: sqlite3.Connection,
    entry_ids: list[int],
    *,
    posting_date: str | None,
    created_by: str | None,
    reason: str,
) -> list[int]:
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    ensure_sme_accounting_ready(conn, commit=False)

    reversed_ids: list[int] = []
    for eid in entry_ids:
        try:
            rev = reverse_journal_entry(
                conn,
                eid,
                posting_date=posting_date,
                created_by=created_by,
                reason=reason,
            )
            if rev and rev.get('id'):
                reversed_ids.append(int(rev['id']))
            elif rev and rev.get('deleted'):
                reversed_ids.append(eid)
        except ValueError as exc:
            # Đã đảo/xóa trước đó
            if 'Không tìm thấy' not in str(exc) and 'đã đảo' not in str(exc).lower():
                # Một số bản reverse báo đã void — bỏ qua
                msg = str(exc).lower()
                if 'void' in msg or 'đã hủy' in msg or 'already' in msg:
                    continue
                raise
    return reversed_ids


def _void_related_vouchers(
    conn: sqlite3.Connection,
    voucher_ids: list[int],
    *,
    reason: str,
    created_by: str | None,
    posting_date: str | None,
    already_reversed_journals: set[int],
) -> int:
    """Hủy PC liên quan; bỏ qua đảo journal nếu đã đảo ở bước trước."""
    from Services.sme.vouchers import void_voucher

    n = 0
    for vid in voucher_ids:
        try:
            row = conn.execute(
                'SELECT id, journal_entry_id, status FROM sme_vouchers WHERE id = ?',
                (vid,),
            ).fetchone()
            if not row:
                continue
            vd = dict(row)
            if vd.get('status') == 'void':
                continue
            jid = vd.get('journal_entry_id')
            if jid and int(jid) in already_reversed_journals:
                # Journal đã xử lý — chỉ đánh dấu/xóa chứng từ
                _safe_exec(
                    conn,
                    "UPDATE sme_vouchers SET status = 'void', reason = COALESCE(reason,'') || ?, updated_at = ? WHERE id = ?",
                    (f' | {reason}', _now(), vid),
                )
                # Kỳ mở: xóa luôn cho sạch
                sealed = False
                try:
                    from Services.sme.period_lock import is_period_sealed
                    date_s = (posting_date or '')[:10]
                    if len(date_s) >= 7:
                        sealed = is_period_sealed(conn, int(date_s[:4]), int(date_s[5:7]))
                except Exception:
                    sealed = False
                if not sealed:
                    _safe_exec(conn, 'DELETE FROM sme_vouchers WHERE id = ?', (vid,))
                n += 1
                continue

            void_voucher(
                conn,
                vid,
                reason=reason,
                created_by=created_by,
                posting_date=posting_date,
                commit=False,
            )
            n += 1
        except ValueError:
            continue
        except Exception:
            continue
    return n


def void_import(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    reason: str = 'Hủy phiếu nhập',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """
    Xóa phiếu nhập SME và dữ liệu liên quan:
      - Đảo/xóa bút toán PNK/HMDD/NTHQ/NKTT/TTNCC/TTLC
      - Hủy phiếu chi thuế HQ / quyết toán gắn phiếu
      - Hoàn WAC + xóa stock_moves / inventory_transactions
      - Xóa TSCĐ/CCDC chưa kích hoạt, import_details, phieu_nhap_kho, advances
      - Gỡ liên kết L/C, reset HĐ đầu vào
      - DELETE bản ghi ``import``
    """
    from Services.fixed_assets_helpers import (
        count_active_assets_by_import_id,
        delete_assets_by_import_id,
    )
    from Services.inward_invoice_helpers import reset_supplier_invoice_after_import_removed
    from Services.sme.branches import assert_import_in_branch

    assert_import_in_branch(conn, import_id)

    imp = conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone()
    if not imp:
        raise ValueError('Không tìm thấy phiếu nhập')
    doc = dict(imp)
    import_no = doc.get('import_no') or f'#{import_id}'
    import_date = str(doc.get('date') or '')[:10]
    posting_date = import_date or None
    reason_s = reason or f'Hủy phiếu nhập {import_no}'

    # ── Guards ─────────────────────────────────────────────
    if _table_exists(conn, 'return_import'):
        ri = conn.execute(
            'SELECT COUNT(*) FROM return_import WHERE import_id = ?',
            (import_id,),
        ).fetchone()
        if ri and int(ri[0]) > 0:
            raise ValueError('Phiếu nhập đã phát sinh trả NCC, không thể xóa')

    c = conn.cursor()
    fa_act, tools_act = count_active_assets_by_import_id(c, import_id)
    if fa_act or tools_act:
        raise ValueError('Không thể xóa: TSCĐ/CCDC từ phiếu nhập đã đưa vào sử dụng')

    # Landed cost đã phân bổ vào phiếu này?
    if _table_exists(conn, 'sme_landed_cost_lines'):
        lc = conn.execute(
            'SELECT COUNT(*) FROM sme_landed_cost_lines WHERE target_import_id = ?',
            (import_id,),
        ).fetchone()
        if lc and int(lc[0]) > 0:
            raise ValueError(
                'Phiếu đã được phân bổ chi phí mua hàng (landed cost) — hủy phân bổ trước khi xóa'
            )

    # Kiểm tra xuất kho sau ngày nhập (hàng hóa/NVL)
    detail_cols = _cols(conn, 'import_details')
    if detail_cols and import_date:
        line_type_expr = (
            "COALESCE(d.line_type, p.product_type, 'goods')"
            if 'line_type' in detail_cols
            else "COALESCE(p.product_type, 'goods')"
        )
        unit_type_expr = 'COALESCE(d.unit_type, 0)' if 'unit_type' in detail_cols else '0'
        try:
            items = conn.execute(
                f"""
                SELECT d.product_id, d.qty, {unit_type_expr} AS unit_type,
                       {line_type_expr} AS line_type,
                       COALESCE(p.unit_ratio, 1) AS unit_ratio, p.name
                FROM import_details d
                JOIN products p ON p.id = d.product_id
                WHERE d.import_id = ?
                """,
                (import_id,),
            ).fetchall()
        except sqlite3.Error:
            items = []

        outbound_types = (
            'export', 'sale', 'SALE', 'adjustment_out', 'ADJUSTMENT_OUT',
            'RETURN_IMPORT', 'RETURN_SALE',
        )
        ph = ','.join('?' * len(outbound_types))
        for item in items:
            it = dict(item)
            lt = str(it.get('line_type') or 'goods').strip().lower()
            if lt in ('fixed_asset', 'intangible_asset', 'tools', 'service'):
                continue
            pid = it.get('product_id')
            if not pid:
                continue
            moved = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt FROM stock_moves
                WHERE product_id = ?
                  AND date(date) >= date(?)
                  AND type IN ({ph})
                """,
                (pid, import_date, *outbound_types),
            ).fetchone()
            if moved and int(moved[0] if not isinstance(moved, sqlite3.Row) else moved['cnt']) > 0:
                raise ValueError(
                    f"Không thể xóa vì «{it.get('name') or pid}» đã phát sinh xuất/trả kho sau ngày nhập"
                )
            imported_qty = import_base_qty(it.get('qty'), it.get('unit_type'), it.get('unit_ratio'))
            curr = ledger_quantity(c, int(pid))
            if imported_qty > curr + 0.0001:
                raise ValueError(
                    f"«{it.get('name') or pid}» tồn kho ({curr}) nhỏ hơn SL nhập ({imported_qty})"
                )

    deleted: dict[str, Any] = {
        'journals_reversed': [],
        'vouchers_voided': 0,
        'stock_pids': [],
        'details': 0,
        'advances': 0,
        'assets': (0, 0),
    }

    # ── 1. Đảo bút toán ────────────────────────────────────
    journal_ids = _collect_import_journal_ids(conn, import_id, doc)
    rev_ids = _reverse_journals(
        conn,
        journal_ids,
        posting_date=posting_date,
        created_by=created_by,
        reason=reason_s,
    )
    deleted['journals_reversed'] = rev_ids
    already_rev = set(journal_ids)  # gốc đã xử lý

    # ── 2. Hủy phiếu chi liên quan ─────────────────────────
    voucher_ids = _collect_import_voucher_ids(conn, import_id, doc)
    deleted['vouchers_voided'] = _void_related_vouchers(
        conn,
        voucher_ids,
        reason=reason_s,
        created_by=created_by,
        posting_date=posting_date,
        already_reversed_journals=already_rev,
    )

    # ── 3. Kho / WAC ───────────────────────────────────────
    sync_pids = set(reverse_import_cost(c, import_id, conn=conn) or [])
    _safe_exec(
        conn,
        """
        DELETE FROM stock_moves
        WHERE ref_id = ?
          AND (
            LOWER(COALESCE(type, '')) IN ('import', 'return_import')
            OR LOWER(COALESCE(ref_type, '')) = 'import'
          )
        """,
        (import_id,),
    )
    _safe_exec(
        conn,
        """
        DELETE FROM inventory_transactions
        WHERE (reference_id = ? AND COALESCE(reference_type, '') = 'import')
           OR import_id = ?
        """,
        (import_id, import_id),
    )

    # ── 4. TSCĐ / CCDC chưa kích hoạt ─────────────────────
    deleted['assets'] = delete_assets_by_import_id(c, import_id)

    # ── 5. Chi tiết + chứng từ phụ ─────────────────────────
    deleted['details'] = _safe_exec(
        conn, 'DELETE FROM import_details WHERE import_id = ?', (import_id,)
    )
    _safe_exec(conn, 'DELETE FROM chi_tiet_phieu_nhap_kho WHERE import_id = ?', (import_id,))
    _safe_exec(conn, 'DELETE FROM phieu_nhap_kho WHERE import_id = ?', (import_id,))
    _safe_exec(conn, 'DELETE FROM import_payments WHERE import_id = ?', (import_id,))

    # Phiếu chi HKD legacy gắn import (nếu còn)
    for tbl in ('phieu_chi', 'Phieu_chi'):
        if _table_exists(conn, tbl):
            _safe_exec(
                conn,
                f"""
                DELETE FROM "{tbl}"
                WHERE source_id = ?
                  AND COALESCE(source_type, '') IN ('import', 'import_service')
                """,
                (import_id,),
            )

    if _table_exists(conn, 'sme_import_advances'):
        deleted['advances'] = _safe_exec(
            conn, 'DELETE FROM sme_import_advances WHERE import_id = ?', (import_id,)
        )

    # Biên bản kiểm nghiệm gắn phiếu — gỡ liên kết (không xóa BB)
    if _table_exists(conn, 'sme_stock_inspections'):
        insp_cols = _cols(conn, 'sme_stock_inspections')
        sets = []
        if 'import_id' in insp_cols:
            sets.append('import_id = NULL')
        if 'import_no' in insp_cols:
            sets.append('import_no = NULL')
        if sets:
            _safe_exec(
                conn,
                f"UPDATE sme_stock_inspections SET {', '.join(sets)} WHERE import_id = ?",
                (import_id,),
            )

    # Sản phẩm tạo từ dòng nhập — gỡ import_id
    if _table_exists(conn, 'products') and 'import_id' in _cols(conn, 'products'):
        _safe_exec(conn, 'UPDATE products SET import_id = NULL WHERE import_id = ?', (import_id,))

    # Landed cost doc mà phiếu này là cost_import
    if _table_exists(conn, 'sme_landed_cost_docs'):
        cost_docs = conn.execute(
            'SELECT id FROM sme_landed_cost_docs WHERE cost_import_id = ?',
            (import_id,),
        ).fetchall()
        for cd in cost_docs:
            cid = int(cd[0] if not isinstance(cd, sqlite3.Row) else cd['id'])
            _safe_exec(conn, 'DELETE FROM sme_landed_cost_lines WHERE landed_cost_id = ?', (cid,))
            _safe_exec(conn, 'DELETE FROM sme_landed_cost_docs WHERE id = ?', (cid,))

    # ── 6. L/C gắn phiếu ───────────────────────────────────
    linked_lc = doc.get('linked_lc_id')
    try:
        from Services.sme.letter_of_credit import delete_lc_settlement_for_import
        delete_lc_settlement_for_import(conn, import_id)
    except Exception:
        pass
    if linked_lc and _table_exists(conn, 'sme_lc_docs'):
        lc_cols = _cols(conn, 'sme_lc_docs')
        sets = ["updated_at = ?"]
        vals: list[Any] = [_now()]
        # Mở lại nếu còn số dư (refresh_lc_status đã chạy trong delete_lc_settlement)
        if 'settle_journal_id' in lc_cols:
            sets.append('settle_journal_id = NULL')
        if 'settle_date' in lc_cols:
            sets.append('settle_date = NULL')
        if 'settled_import_id' in lc_cols:
            sets.append(
                "settled_import_id = CASE WHEN settled_import_id = ? THEN NULL ELSE settled_import_id END"
            )
            vals.append(import_id)
        if 'import_id' in lc_cols:
            sets.append(
                "import_id = CASE WHEN import_id = ? THEN NULL ELSE import_id END"
            )
            vals.append(import_id)
        vals.append(int(linked_lc))
        _safe_exec(
            conn,
            f"UPDATE sme_lc_docs SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        try:
            from Services.sme.letter_of_credit import refresh_lc_status_from_balance
            refresh_lc_status_from_balance(conn, int(linked_lc), commit=False)
        except Exception:
            pass
    # Gỡ mọi LC còn trỏ import_id này
    if _table_exists(conn, 'sme_lc_docs') and 'import_id' in _cols(conn, 'sme_lc_docs'):
        _safe_exec(
            conn,
            "UPDATE sme_lc_docs SET import_id = NULL, updated_at = ? WHERE import_id = ?",
            (_now(), import_id),
        )

    # ── 7. Reset HĐ đầu vào ────────────────────────────────
    supplier_tax = None
    if doc.get('supplier_id'):
        srow = conn.execute(
            'SELECT tax_code FROM suppliers WHERE id = ?',
            (doc['supplier_id'],),
        ).fetchone()
        if srow:
            supplier_tax = srow[0] if not isinstance(srow, sqlite3.Row) else srow['tax_code']
    try:
        reset_supplier_invoice_after_import_removed(
            c,
            from_invoice_id=doc.get('from_invoice_id'),
            bill_no=doc.get('bill_no'),
            tax_code=supplier_tax,
        )
    except Exception:
        pass

    # ── 8. Xóa header import ───────────────────────────────
    _safe_exec(conn, 'DELETE FROM "import" WHERE id = ?', (import_id,))

    if sync_pids:
        sync_inventory_quantities(c, list(sync_pids))
        deleted['stock_pids'] = sorted(sync_pids)

    if commit:
        sqlite_commit(conn, label='import_ops')

    return {
        'id': import_id,
        'import_no': import_no,
        'deleted': True,
        'reason': reason_s,
        'details': deleted,
        'message': f'Đã xóa phiếu nhập {import_no} và dữ liệu liên quan',
    }
