"""Phiếu nhập/xuất kho SME (mẫu 01-VT / 02-VT) — đọc từ import / phieu_xuat_kho, không đụng HKD."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def ensure_phieu_xuat_kho_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Đảm bảo bảng phiếu xuất kho 02-VT có đủ cột dùng cho in mẫu."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS phieu_xuat_kho (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT UNIQUE,
            date TEXT,
            customer_name TEXT,
            items_json TEXT,
            total_amount REAL,
            sale_id INTEGER
        )
        """
    )
    cols = {r[1] for r in conn.execute('PRAGMA table_info(phieu_xuat_kho)').fetchall()}
    for col, decl in (
        ('note', 'TEXT'),
        ('address', 'TEXT'),
        ('form_code', "TEXT DEFAULT '02-VT'"),
    ):
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE phieu_xuat_kho ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass
    if commit:
        conn.commit()


def _next_px_voucher_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT voucher_no FROM phieu_xuat_kho
        WHERE voucher_no LIKE 'PX%'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    n = 1
    if row:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row['voucher_no']
        try:
            n = int(str(raw)[2:]) + 1
        except (TypeError, ValueError):
            n = 1
    return f'PX{n:06d}'


def upsert_stock_out_voucher_for_sale(
    conn: sqlite3.Connection,
    *,
    sale_id: int,
    sale_date: str,
    customer_name: str,
    items: list[dict[str, Any]],
    total_amount: float,
    note: str = '',
    address: str = '',
    reuse_voucher_no: bool = True,
) -> dict[str, Any]:
    """Tạo/cập nhật phiếu xuất kho mẫu 02-VT gắn sale_id (số PX000001…)."""
    ensure_phieu_xuat_kho_schema(conn, commit=False)
    if not items:
        raise ValueError('Phiếu xuất kho 02-VT cần ít nhất một dòng hàng')
    date_s = str(sale_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày phiếu xuất kho')
    cols = {r[1] for r in conn.execute('PRAGMA table_info(phieu_xuat_kho)').fetchall()}
    existing = conn.execute(
        'SELECT id, voucher_no FROM phieu_xuat_kho WHERE sale_id = ? ORDER BY id DESC LIMIT 1',
        (sale_id,),
    ).fetchone()
    voucher_no = None
    if existing and reuse_voucher_no:
        voucher_no = existing[1] if not isinstance(existing, sqlite3.Row) else existing['voucher_no']
        eid = existing[0] if not isinstance(existing, sqlite3.Row) else existing['id']
        conn.execute('DELETE FROM phieu_xuat_kho WHERE sale_id = ? AND id != ?', (sale_id, eid))
    else:
        conn.execute('DELETE FROM phieu_xuat_kho WHERE sale_id = ?', (sale_id,))
        eid = None
    if not voucher_no:
        voucher_no = _next_px_voucher_no(conn)

    items_json = json.dumps(items, ensure_ascii=False)
    fields = [
        'voucher_no', 'date', 'customer_name', 'items_json', 'total_amount', 'sale_id',
    ]
    vals: list[Any] = [
        voucher_no, date_s, customer_name or '', items_json, float(total_amount or 0), sale_id,
    ]
    if 'note' in cols:
        fields.append('note')
        vals.append(note or 'Xuất kho')
    if 'address' in cols:
        fields.append('address')
        vals.append(address or '')
    if 'form_code' in cols:
        fields.append('form_code')
        vals.append('02-VT')

    if eid:
        sets = ', '.join(f'{f} = ?' for f in fields)
        conn.execute(
            f'UPDATE phieu_xuat_kho SET {sets} WHERE id = ?',
            vals + [eid],
        )
        voucher_id = int(eid)
    else:
        placeholders = ', '.join(['?'] * len(fields))
        conn.execute(
            f"INSERT INTO phieu_xuat_kho ({', '.join(fields)}) VALUES ({placeholders})",
            vals,
        )
        voucher_id = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])

    return {
        'id': voucher_id,
        'voucher_no': voucher_no,
        'form_code': '02-VT',
        'sale_id': sale_id,
        'total_amount': float(total_amount or 0),
    }


def list_stock_in(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_code: str | None = None,
    q: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    try:
        from Services.sme.import_settle import ensure_import_settle_schema
        ensure_import_settle_schema(conn, commit=False)
    except Exception:
        pass
    try:
        from Services.sme.import_transit import ensure_import_transit_schema
        ensure_import_transit_schema(conn, commit=False)
    except Exception:
        pass
    try:
        from Services.sme.import_payment import ensure_import_payment_schema
        ensure_import_payment_schema(conn, commit=False)
    except Exception:
        pass
    try:
        from Services.inward_invoice_helpers import migrate_import_for_service
        migrate_import_for_service(conn)
    except Exception:
        pass
    try:
        from Services.sme.tax_payment_ops import ensure_tax_payment_schema
        ensure_tax_payment_schema(conn, commit=False)
    except Exception:
        pass

    cols = {r[1] for r in conn.execute('PRAGMA table_info("import")').fetchall()}
    env_sel = (
        'COALESCE(i.env_tax_amount, 0) AS env_tax_amount'
        if 'env_tax_amount' in cols else '0 AS env_tax_amount'
    )

    sql = f"""
        SELECT
            i.id,
            COALESCE(i.import_no, 'PN' || printf('%06d', i.id)) AS voucher_no,
            COALESCE(i.import_no, 'PN' || printf('%06d', i.id)) AS import_no,
            i.date,
            COALESCE(s.name, '') AS supplier_name,
            COALESCE(i.total_value, 0) AS total_amount,
            COALESCE(i.bill_no, '') AS bill_no,
            COALESCE(i.payment_status, '') AS payment_status,
            COALESCE(i.payment_mode, '') AS payment_mode,
            i.linked_lc_id,
            i.settle_journal_id,
            COALESCE(i.settle_amount_fc, 0) AS settle_amount_fc,
            COALESCE(i.import_type, 'DOMESTIC') AS import_type,
            COALESCE(i.receipt_stage, 'RECEIVED') AS receipt_stage,
            i.tax_payment_voucher_id,
            i.receive_journal_id,
            COALESCE(i.import_tax_amount, 0) AS import_tax_amount,
            COALESCE(i.excise_tax_amount, 0) AS excise_tax_amount,
            {env_sel},
            COALESCE(i.amount_fc, 0) AS amount_fc,
            COALESCE(i.advance_fc, 0) AS advance_fc
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
        # DB chưa migrate cột stage — fallback cột cơ bản
        sql_basic = """
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
        params2: list[Any] = []
        if date_from:
            sql_basic += ' AND date(i.date) >= date(?)'
            params2.append(date_from[:10])
        if date_to:
            sql_basic += ' AND date(i.date) <= date(?)'
            params2.append(date_to[:10])
        if q_s:
            like = f'%{q_s}%'
            sql_basic += ' AND (i.import_no LIKE ? OR i.bill_no LIKE ? OR s.name LIKE ?)'
            params2.extend([like, like, like])
        sql_basic += bf
        params2.extend(bp)
        sql_basic += ' ORDER BY date(i.date) DESC, i.id DESC LIMIT ?'
        params2.append(int(limit))
        try:
            rows = [dict(r) for r in conn.execute(sql_basic, params2).fetchall()]
            for r in rows:
                r.setdefault('import_type', 'DOMESTIC')
                r.setdefault('receipt_stage', 'RECEIVED')
            return rows
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
    ensure_phieu_xuat_kho_schema(conn, commit=False)
    info_row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(info_row) if info_row else {}
    row = conn.execute(
        'SELECT * FROM phieu_xuat_kho WHERE id = ?', (voucher_id,)
    ).fetchone()
    if not row:
        return None
    px_raw = dict(row)
    try:
        items = json.loads(px_raw.get('items_json') or '[]')
    except (TypeError, json.JSONDecodeError):
        items = []
    hang_hoa = []
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = float(it.get('qty') if it.get('qty') is not None else (it.get('quantity') or 0))
        price = float(it.get('price') or 0)
        amount = float(it.get('amount') if it.get('amount') is not None else qty * price)
        hang_hoa.append({
            'product_id': it.get('product_id'),
            'product_name': it.get('product_name') or it.get('name') or '',
            'product_code': it.get('product_code') or it.get('barcode') or '',
            'unit': it.get('unit') or '',
            'qty': qty,
            'quantity': qty,
            'price': price,
            'amount': amount,
        })
    total = float(px_raw.get('total_amount') or 0)
    if total <= 0 and hang_hoa:
        total = sum(float(h['amount']) for h in hang_hoa)
    try:
        from helpers import so_thanh_chu
        total_str = so_thanh_chu(total).capitalize()
    except Exception:
        total_str = ''
    px = {
        **px_raw,
        'customer_name': px_raw.get('customer_name') or '',
        'address': px_raw.get('address') or '',
        'note': px_raw.get('note') or 'Xuất kho',
        'warehouse_location': info.get('warehouse_location') or 'Kho tổng',
        'total_amount': total,
        'total_str': total_str,
        'items': hang_hoa,
        'hang_hoa': hang_hoa,
    }
    return px, info
