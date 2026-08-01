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
    limit: int = 500,
) -> list[dict[str, Any]]:
    sql = """
        SELECT
            i.id,
            COALESCE(i.import_no, 'PN' || printf('%06d', i.id)) AS voucher_no,
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
    if not imp.get('import_no'):
        imp['import_no'] = f"PN{import_id:06d}"
    if imp.get('total_value') is None:
        imp['total_value'] = 0

    cols = {r[1] for r in conn.execute('PRAGMA table_info(import_details)').fetchall()}
    select_fields = [
        'id.*',
        'p.name AS product_name',
        'p.unit AS base_unit',
        'p.unit1 AS wholesale_unit',
        'p.barcode',
        'p.product_code',
    ]
    if 'unit' in cols:
        select_fields.append('id.unit AS import_unit')
    if 'unit_type' in cols:
        select_fields.append('id.unit_type')

    raw_items = conn.execute(
        f"""
        SELECT {', '.join(select_fields)}
        FROM import_details id
        JOIN products p ON p.id = id.product_id
        WHERE id.import_id = ?
        """,
        (import_id,),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in raw_items:
        item = dict(row)
        if item.get('import_unit'):
            item['display_unit'] = str(item['import_unit']).strip() or item.get('base_unit') or '—'
        elif item.get('unit_type') == 1 and item.get('wholesale_unit'):
            item['display_unit'] = str(item['wholesale_unit']).strip() or '—'
        else:
            item['display_unit'] = item.get('base_unit') or '—'
        items.append(item)

    return imp, items, info


def get_stock_out_print_payload(
    conn: sqlite3.Connection, voucher_id: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Trả (px, info) cho mẫu in 02-VT từ phieu_xuat_kho."""
    from helpers import so_thanh_chu

    info_row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(info_row) if info_row else {}

    row = conn.execute(
        'SELECT * FROM phieu_xuat_kho WHERE id = ?', (voucher_id,)
    ).fetchone()
    if not row:
        return None
    raw = dict(row)
    try:
        hang_hoa = json.loads(raw.get('items_json') or '[]')
    except (TypeError, json.JSONDecodeError):
        hang_hoa = []

    # Chuẩn hoá key số lượng / đơn giá từ các nguồn khác nhau
    normalized = []
    for it in hang_hoa:
        if not isinstance(it, dict):
            continue
        qty = float(it.get('quantity') or it.get('qty') or 0)
        price = float(it.get('price') or it.get('cost_price') or 0)
        amount = float(it.get('amount') or (qty * price))
        normalized.append({
            'product_name': it.get('product_name') or it.get('name') or '',
            'product_code': it.get('product_code') or it.get('barcode') or '',
            'unit': it.get('unit') or it.get('display_unit') or '—',
            'qty': qty,
            'price': price,
            'amount': amount,
        })

    total = float(raw.get('total_amount') or sum(i['amount'] for i in normalized) or 0)
    address = ''
    sale_id = raw.get('sale_id')
    if sale_id:
        sale = conn.execute(
            'SELECT address, company_name FROM sale WHERE id = ?', (sale_id,)
        ).fetchone()
        if sale:
            address = sale['address'] or ''
            if sale['company_name'] and not raw.get('customer_name'):
                raw['customer_name'] = sale['company_name']

    px = {
        'id': raw['id'],
        'voucher_no': raw.get('voucher_no') or f"PX{voucher_id:06d}",
        'date': (raw.get('date') or '')[:10],
        'customer_name': raw.get('customer_name') or 'Khách lẻ',
        'address': address,
        'note': raw.get('note') or 'Xuất kho bán hàng',
        'hang_hoa': normalized,
        'total_amount': total,
        'total_str': so_thanh_chu(round(total)),
        'warehouse_location': info.get('warehouse_location') or 'Kho tổng',
    }
    return px, info
