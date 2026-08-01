"""Phiếu nhập/xuất kho SME (mẫu 01-VT / 02-VT) — đọc từ import / phieu_xuat_kho, không đụng HKD."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def list_stock_in(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_code: str | None = None,
    q: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    sql = """
        SELECT
            i.id,
            COALESCE(i.import_no, 'PN' || printf('%06d', i.id)) AS voucher_no,
            COALESCE(i.import_no, 'PN' || printf('%06d', i.id)) AS import_no,
            i.date,
            COALESCE(s.name, '') AS supplier_name,
            COALESCE(i.total_value, 0) AS total_amount,
            COALESCE(i.bill_no, '') AS bill_no,
            COALESCE(i.payment_status, '') AS payment_status
        FROM import i
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        WHERE 1=1
    """
    params: list[Any] = []
    if date_from:
        sql += ' AND date(i.date) >= date(?)'
        params.append(date_from[:10])
    if date_to:
        sql += ' AND date(i.date) <= date(?)'
        params.append(date_to[:10])
    q_s = (q or '').strip()
    if q_s:
        like = f'%{q_s}%'
        sql += ' AND (i.import_no LIKE ? OR i.bill_no LIKE ? OR s.name LIKE ?)'
        params.extend([like, like, like])
    from Services.sme.branches import import_branch_filter_sql
    bf, bp = import_branch_filter_sql(conn, branch_code, alias='i')
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY date(i.date) DESC, i.id DESC LIMIT ?'
    params.append(int(limit))
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def list_stock_out(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_code: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, voucher_no, date, customer_name, total_amount, note, sale_id
        FROM phieu_xuat_kho WHERE 1=1
    """
    params: list[Any] = []
    if date_from:
        sql += ' AND date(date) >= date(?)'
        params.append(date_from[:10])
    if date_to:
        sql += ' AND date(date) <= date(?)'
        params.append(date_to[:10])
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        from Services.sme.branches import sale_branch_filter_sql
        # Lọc theo sale.warehouse → CN (đồng bộ /api/sme/sales)
        try:
            bf, bp = sale_branch_filter_sql(conn, code, alias='s')
            if bf:
                sql += f"""
                    AND sale_id IN (
                        SELECT s.id FROM sale s WHERE 1=1 {bf}
                    )
                """
                params.extend(bp)
        except Exception:
            pass
    sql += ' ORDER BY date(date) DESC, id DESC LIMIT ?'

    params.append(int(limit))
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def get_stock_in_print_payload(
    conn: sqlite3.Connection, import_id: int
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]] | None:
    """Trả (imp, items, info) cho mẫu in 01-VT."""
    info_row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(info_row) if info_row else {}

    imp_row = conn.execute(
        """
        SELECT i.*, COALESCE(s.name, 'Nhà cung cấp') AS supplier_name,
               COALESCE(s.address, '') AS supplier_address
        FROM import i
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        WHERE i.id = ?
        """,
        (import_id,),
    ).fetchone()
    if not imp_row:
        return None
    imp = dict(imp_row)

    items = []
    try:
        detail_cols = {r[1] for r in conn.execute('PRAGMA table_info(import_details)').fetchall()}
        wh_sel = ', warehouse_code' if 'warehouse_code' in detail_cols else ''
        for r in conn.execute(
            f"""
            SELECT d.*, p.name AS product_name, p.unit
            FROM import_details d
            LEFT JOIN products p ON p.id = d.product_id
            WHERE d.import_id = ?
            ORDER BY d.id
            """,
            (import_id,),
        ).fetchall():
            items.append(dict(r))
    except sqlite3.Error:
        items = []

    return imp, items, info


def get_stock_out_print_payload(
    conn: sqlite3.Connection, voucher_id: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Trả (px, info) cho mẫu in 02-VT từ phieu_xuat_kho."""
    info_row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(info_row) if info_row else {}
    row = conn.execute(
        'SELECT * FROM phieu_xuat_kho WHERE id = ?', (voucher_id,)
    ).fetchone()
    if not row:
        return None
    px = dict(row)
    try:
        items = json.loads(px.get('items_json') or '[]')
    except (TypeError, json.JSONDecodeError):
        items = []
    px['items'] = items
    return px, info
