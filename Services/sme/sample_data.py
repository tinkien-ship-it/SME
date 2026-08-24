# -*- coding: utf-8 -*-
"""Dữ liệu mẫu kế toán SME (TT99) — sổ kép + chứng từ vận hành để test báo cáo.

Tạo bộ giao dịch năm hiện tại (hoặc ``fiscal_year``):
- Góp vốn, tiền mặt / ngân hàng
- Mua hàng + VAT đầu vào, TSCĐ + khấu hao
- Bán hàng tiền mặt / công nợ + VAT đầu ra + giá vốn
- Chi phí BH/QLDN, lương, BHXH
- Quyết toán GTGT + kết chuyển KQKD các kỳ đã đóng

Định danh bút toán mẫu: ``document_type`` bắt đầu ``SAMPLE_`` / ``reference_document=SME_SAMPLE``.
"""
from __future__ import annotations

import calendar
import json
import os
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from Services.sme.bootstrap import ensure_sme_accounting_ready
from Services.sme.journal_engine import post_journal_entry
from Services.sme.period_close import run_period_close
from Services.sme.vat_settlement import run_vat_settlement
from db_utils import sqlite_commit

SAMPLE_TAG = 'SME_SAMPLE'
SAMPLE_PREFIX = 'SAMPLE_'
WH_MAIN = 'KHO_001'


# Kế hoạch bán tháng 2–7: (month, day, customer, payment, items[(pid, qty, sell_price)])
SALE_PLANS = (
    (2, 12, 'Đại lý ABC', 'Công nợ', ((1, 120, 120000), (2, 60, 220000))),
    (2, 20, 'Siêu thị Mini Mart', 'Chuyển khoản', ((3, 300, 35000), (4, 90, 55000))),
    (3, 8, 'Đại lý ABC', 'Công nợ', ((1, 150, 120000), (3, 240, 35000))),
    (3, 18, 'Siêu thị Mini Mart', 'Tiền mặt', ((2, 75, 220000),)),
    (4, 5, 'Đại lý ABC', 'Công nợ', ((1, 180, 120000), (4, 120, 55000))),
    (4, 22, 'Siêu thị Mini Mart', 'Chuyển khoản', ((2, 90, 220000), (3, 360, 35000))),
    (5, 10, 'Đại lý ABC', 'Công nợ', ((1, 135, 120000), (2, 45, 220000))),
    (5, 25, 'Siêu thị Mini Mart', 'Tiền mặt', ((3, 450, 35000), (4, 150, 55000))),
    (6, 7, 'Đại lý ABC', 'Công nợ', ((1, 165, 120000), (3, 270, 35000))),
    (6, 19, 'Siêu thị Mini Mart', 'Chuyển khoản', ((2, 84, 220000),)),
    (7, 9, 'Đại lý ABC', 'Công nợ', ((1, 105, 120000), (4, 75, 55000))),
    (7, 21, 'Siêu thị Mini Mart', 'Tiền mặt', ((2, 54, 220000), (3, 300, 35000))),
)

# Bán tháng 1 (đồng bộ bút toán BAN-01 / GV-01)
JAN_SALE_ITEMS = ((1, 100, 120000), (2, 40, 220000), (3, 200, 35000), (4, 50, 55000))

# Nhập kho tháng 1 — đủ hàng cho bán + tồn cuối kỳ
JAN_IMPORT_LINES = (
    # (pid, qty, unit_cost)
    (1, 2000, 90000),
    (2, 800, 150000),
    (3, 3000, 25000),
    (4, 800, 40000),
    (5, 500, 12000),   # NVL đường
    (6, 200, 80000),   # Bao bì
)


def _money(v) -> float:
    return float(Decimal(str(v or 0)).quantize(Decimal('0.01')))


def _post(conn, *, date: str, doc_type: str, doc_no: str, doc_id: int, desc: str, lines: list[dict]):
    return post_journal_entry(
        conn,
        posting_date=date,
        document_date=date,
        document_type=f'{SAMPLE_PREFIX}{doc_type}',
        document_no=doc_no,
        document_id=doc_id,
        business_type=doc_type,
        description=desc,
        reference_document=SAMPLE_TAG,
        created_by='sample_seed',
        lines=[{**ln, 'sequence': i} for i, ln in enumerate(lines, start=1)],
    )


def clear_sample_journals(conn: sqlite3.Connection, *, commit: bool = False) -> int:
    """Xóa bút toán mẫu (và dòng) trước khi seed lại."""
    try:
        ids = [
            r[0]
            for r in conn.execute(
                """
                SELECT id FROM sme_journal_entries
                WHERE reference_document = ?
                   OR document_type LIKE ?
                """,
                (SAMPLE_TAG, f'{SAMPLE_PREFIX}%'),
            ).fetchall()
        ]
    except sqlite3.Error:
        return 0
    if not ids:
        return 0
    for eid in ids:
        conn.execute('DELETE FROM sme_journal_lines WHERE entry_id=?', (eid,))
        conn.execute('DELETE FROM sme_journal_entries WHERE id=?', (eid,))
    # Xóa QTGT/KCKQ gắn cùng kỳ nếu chỉ còn sample (an toàn: xóa theo created_by sample)
    for doc in ('QTGT', 'KCKQ'):
        rows = conn.execute(
            """
            SELECT id FROM sme_journal_entries
            WHERE document_type=? AND created_by='sample_seed'
            """,
            (doc,),
        ).fetchall()
        for r in rows:
            conn.execute('DELETE FROM sme_journal_lines WHERE entry_id=?', (r[0],))
            conn.execute('DELETE FROM sme_journal_entries WHERE id=?', (r[0],))
            ids.append(r[0])
    if commit:
        sqlite_commit(conn, label='sample_data')
    return len(ids)


def _ensure_ops_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS business_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_name TEXT, tax_code TEXT, address TEXT, phone TEXT, email TEXT,
            representative_name TEXT, bank_account TEXT, bank_name TEXT, account_holder TEXT,
            accounting_regime TEXT DEFAULT 'SME_TT99',
            filing_period TEXT DEFAULT 'monthly',
            vat_filing_period TEXT DEFAULT 'monthly'
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code TEXT UNIQUE, barcode TEXT, name TEXT NOT NULL,
            unit TEXT DEFAULT 'Cái', buyprice REAL DEFAULT 0, price REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS inventory (
            product_id INTEGER PRIMARY KEY, quantity REAL DEFAULT 0, avg_cost REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT, address TEXT, tax_code TEXT
        );
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, name TEXT NOT NULL,
            phone TEXT, address TEXT, tax_code TEXT
        );
        CREATE TABLE IF NOT EXISTS sale (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, total_amount REAL,
            payment_method TEXT, customer_name TEXT, status TEXT, tax_amount REAL DEFAULT 0,
            invoice_number TEXT DEFAULT '', note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER, product_id INTEGER, quantity REAL, price REAL, cost_price REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS import (
            id INTEGER PRIMARY KEY AUTOINCREMENT, import_no TEXT UNIQUE, date TEXT,
            supplier_id INTEGER, bill_no TEXT, note TEXT, payment_status TEXT,
            total_value REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS import_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT, import_id INTEGER, product_id INTEGER,
            qty REAL, buyprice REAL, cost_price REAL, tax REAL DEFAULT 0, subtotal REAL DEFAULT 0,
            payment_amt REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stock_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, date TEXT NOT NULL,
            type TEXT NOT NULL, ref_document TEXT DEFAULT '', ref_id INTEGER, ref_no TEXT,
            in_quantity REAL DEFAULT 0, out_quantity REAL DEFAULT 0, quantity REAL DEFAULT 0,
            avg_cost REAL DEFAULT 0, cost_price REAL DEFAULT 0, total_value REAL DEFAULT 0, note TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT,
            role TEXT, full_name TEXT
        );
        """
    )


def _seed_master_data(conn: sqlite3.Connection) -> dict[str, Any]:
    from Services.sme.branches import DEFAULT_BRANCH_CODE, ensure_sme_branches_schema

    ensure_sme_branches_schema(conn, commit=False)

    # Xóa vận hành cũ trước khi nạp master (tránh lệch tồn / FK)
    for sql in (
        "DELETE FROM sale_items",
        "DELETE FROM sale",
        "DELETE FROM import_details",
        "DELETE FROM import",
        "DELETE FROM stock_moves",
        "DELETE FROM inventory",
        "DELETE FROM products",
        "DELETE FROM customers",
        "DELETE FROM suppliers",
        "DELETE FROM employees",
    ):
        try:
            conn.execute(sql)
        except sqlite3.Error:
            pass

    bi_cols = {r[1] for r in conn.execute('PRAGMA table_info(business_info)').fetchall()}
    conn.execute('DELETE FROM business_info')
    bi_fields = [
        'business_name', 'tax_code', 'address', 'phone', 'email', 'representative_name',
        'bank_account', 'bank_name', 'account_holder', 'accounting_regime', 'filing_period',
    ]
    bi_vals = [
        'Công ty TNHH Demo SME', '0319999888', '123 Nguyễn Huệ, Q.1, TP.HCM',
        '0909000111', 'ketoan@demosme.vn', 'Nguyễn Văn Demo',
        '0123456789', 'Vietcombank', 'CONG TY TNHH DEMO SME', 'SME_TT99', 'monthly',
    ]
    if 'vat_filing_period' in bi_cols:
        bi_fields.append('vat_filing_period')
        bi_vals.append('monthly')
    conn.execute(
        f"INSERT INTO business_info ({','.join(bi_fields)}) VALUES ({','.join('?' for _ in bi_fields)})",
        bi_vals,
    )

    # Chi nhánh + kho
    conn.execute('DELETE FROM sme_branches')
    conn.execute(
        """
        INSERT INTO sme_branches (code, name, address, phone, is_default, is_active, notes)
        VALUES (?, ?, ?, ?, 1, 1, ?)
        """,
        (DEFAULT_BRANCH_CODE, 'Trụ sở chính', '123 Nguyễn Huệ, Q.1, TP.HCM', '0909000111', 'HQ demo'),
    )
    conn.execute(
        """
        INSERT INTO sme_branches (code, name, address, phone, is_default, is_active, notes)
        VALUES ('CN2', 'Chi nhánh Quận 7', '45 Nguyễn Văn Linh, Q.7', '0909000222', 0, 1, 'CN demo')
        """
    )
    try:
        from Services.import_line_helpers import ensure_warehouse_schema
        ensure_warehouse_schema(conn)
    except Exception:
        pass
    try:
        conn.execute('DELETE FROM warehouses')
    except sqlite3.Error:
        pass
    wh_cols = {r[1] for r in conn.execute('PRAGMA table_info(warehouses)').fetchall()}
    if wh_cols:
        # KHO_001 HQ (mặc định), KHO_002 → CN2, KHO_003 HQ
        rows = [
            ('KHO_001', 'Kho trung tâm', DEFAULT_BRANCH_CODE, 1, 1),
            ('KHO_002', 'Kho 2 — Chi nhánh Q7', 'CN2', 0, 1),
            ('KHO_003', 'Kho 3', DEFAULT_BRANCH_CODE, 0, 1),
        ]
        for code, name, br, is_def, active in rows:
            fields = ['code', 'name']
            vals: list[Any] = [code, name]
            if 'branch_code' in wh_cols:
                fields.append('branch_code')
                vals.append(br)
            if 'is_default' in wh_cols:
                fields.append('is_default')
                vals.append(is_def)
            if 'is_active' in wh_cols:
                fields.append('is_active')
                vals.append(active)
            conn.execute(
                f"INSERT INTO warehouses ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                vals,
            )
        # Đảm bảo đủ bộ mặc định nếu thiếu
        try:
            ensure_warehouse_schema(conn)
        except Exception:
            pass

    p_cols = {r[1] for r in conn.execute('PRAGMA table_info(products)').fetchall()}
    products = [
        (1, 'SP-GAO', 'Gạo Jasmine 5kg', 'Bao', 90000, 120000, 'goods', 'Thực phẩm'),
        (2, 'SP-CAFE', 'Cà phê rang xay 1kg', 'Kg', 150000, 220000, 'goods', 'Thực phẩm'),
        (3, 'SP-SUA', 'Sữa tươi hộp 1L', 'Hộp', 25000, 35000, 'goods', 'Thực phẩm'),
        (4, 'SP-TRA', 'Trà túi lọc hộp', 'Hộp', 40000, 55000, 'goods', 'Thực phẩm'),
        (5, 'NVL-DUONG', 'Đường cát trắng', 'Kg', 12000, 18000, 'goods', 'Nguyên liệu'),
        (6, 'NVL-BAOBI', 'Thùng carton 20 chỗ', 'Cái', 80000, 0, 'goods', 'Bao bì'),
    ]
    for pid, code, name, unit, buy, price, ptype, cat in products:
        fields = ['id', 'product_code', 'name', 'unit', 'buyprice', 'price']
        vals: list[Any] = [pid, code, name, unit, buy, price]
        if 'barcode' in p_cols:
            fields.append('barcode')
            vals.append(code)
        if 'product_type' in p_cols:
            fields.append('product_type')
            vals.append(ptype)
        if 'category' in p_cols:
            fields.append('category')
            vals.append(cat)
        if 'is_available' in p_cols:
            fields.append('is_available')
            vals.append(1)
        if 'unit_ratio' in p_cols:
            fields.append('unit_ratio')
            vals.append(1)
        conn.execute(
            f"INSERT INTO products ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            vals,
        )
        conn.execute(
            'INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?,?,?)',
            (pid, 0, buy),
        )

    customers = [
        (1, 'Đại lý ABC', '0901111222', 'Q.3, TP.HCM', '0301111222'),
        (2, 'Siêu thị Mini Mart', '0903333444', 'Q.7, TP.HCM', '0303333444'),
        (3, 'Khách lẻ', '', '', ''),
        (4, 'Nhà hàng Biển Đông', '0905555666', 'Q.1, TP.HCM', '0315555666'),
    ]
    c_cols = {r[1] for r in conn.execute('PRAGMA table_info(customers)').fetchall()}
    for row in customers:
        fields = ['id', 'name', 'phone', 'address', 'tax_code']
        vals = list(row)
        if 'code' in c_cols:
            fields.append('code')
            vals.append(f'KH{row[0]:03d}')
        conn.execute(
            f"INSERT INTO customers ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            vals,
        )

    suppliers = [
        (1, 'NCC01', 'NCC Thực phẩm Đồng Nai', '0251123456', 'Biên Hòa', '3601234567'),
        (2, 'NCC02', 'NCC Bao bì Minh Phát', '0287654321', 'Bình Tân', '0307654321'),
        (3, 'NCC03', 'NCC Đường Biên Hòa', '0251987654', 'Đồng Nai', '3609876543'),
    ]
    for row in suppliers:
        conn.execute(
            'INSERT INTO suppliers (id, code, name, phone, address, tax_code) VALUES (?,?,?,?,?,?)',
            row,
        )

    e_cols = {r[1] for r in conn.execute('PRAGMA table_info(employees)').fetchall()}
    employees = [
        (1, 'Nguyễn Văn An', 'Kế toán trưởng', 18000000, 'active'),
        (2, 'Trần Thị Bình', 'Thủ quỹ', 12000000, 'active'),
        (3, 'Lê Văn Cường', 'Nhân viên kho', 9000000, 'active'),
        (4, 'Phạm Thị Dung', 'Nhân viên bán hàng', 10000000, 'active'),
        (5, 'Hoàng Văn Em', 'Công nhân SX', 8500000, 'active'),
    ]
    for eid, name, pos, salary, status in employees:
        fields = ['id', 'fullname', 'position']
        vals: list[Any] = [eid, name, pos]
        if 'base_salary' in e_cols:
            fields.append('base_salary')
            vals.append(salary)
        if 'salary_rate' in e_cols:
            fields.append('salary_rate')
            vals.append(salary)
        if 'status' in e_cols:
            fields.append('status')
            vals.append(status)
        if 'join_date' in e_cols:
            fields.append('join_date')
            vals.append(f'{datetime.now().year}-01-01')
        conn.execute(
            f"INSERT INTO employees ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            vals,
        )

    return {
        'products': len(products),
        'customers': len(customers),
        'suppliers': len(suppliers),
        'employees': len(employees),
        'warehouses': 3,
        'branches': 2,
    }


def _add_stock(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    date: str,
    qty_in: float = 0,
    qty_out: float = 0,
    typ: str = 'import',
    ref_no: str = '',
    ref_id: int | None = None,
    cost: float = 0,
    warehouse_code: str = WH_MAIN,
) -> float:
    """Ghi stock_moves theo quy ước số có dấu (+ nhập / − xuất) rồi sync inventory."""
    from Services.inventory_stock_helpers import (
        apply_wac_inbound,
        apply_wac_outbound,
        sync_inventory_quantity_from_moves,
    )

    qty_in = float(qty_in or 0)
    qty_out = float(qty_out or 0)
    cost = float(cost or 0)
    qty_signed = qty_in - qty_out
    if abs(qty_signed) < 1e-9:
        return cost

    cur = conn.cursor()
    if qty_in > 0:
        apply_wac_inbound(cur, product_id, qty_in, qty_in * cost)
        move_cost = cost
    else:
        _new_c, move_cost = apply_wac_outbound(cur, product_id, qty_out, cost if cost > 0 else None)

    cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_moves)').fetchall()}
    fields = ['product_id', 'date', 'type', 'quantity', 'cost_price', 'note']
    values: list[Any] = [product_id, date, typ, qty_signed, move_cost, 'sample']
    if 'in_quantity' in cols:
        fields.append('in_quantity')
        values.append(qty_in if qty_in > 0 else 0)
    if 'out_quantity' in cols:
        fields.append('out_quantity')
        values.append(qty_out if qty_out > 0 else 0)
    if 'avg_cost' in cols:
        fields.append('avg_cost')
        values.append(move_cost)
    if 'total_value' in cols:
        fields.append('total_value')
        values.append(abs(qty_signed) * move_cost)
    if 'ref_document' in cols:
        fields.append('ref_document')
        values.append(ref_no or SAMPLE_TAG)
    if 'ref_id' in cols and ref_id is not None:
        fields.append('ref_id')
        values.append(ref_id)
    if 'ref_type' in cols:
        fields.append('ref_type')
        values.append('import' if qty_in > 0 else 'export')
    if 'type1' in cols:
        fields.append('type1')
        values.append('Nhập' if qty_in > 0 else 'Xuất')
    if 'warehouse_code' in cols:
        fields.append('warehouse_code')
        values.append(warehouse_code)
    if 'unit' in cols:
        fields.append('unit')
        u = conn.execute('SELECT unit FROM products WHERE id=?', (product_id,)).fetchone()
        values.append((u[0] if u else None) or 'Cái')

    conn.execute(
        f"INSERT INTO stock_moves ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
        values,
    )
    sync_inventory_quantity_from_moves(cur, product_id)
    return float(move_cost)


def _product_buy(conn: sqlite3.Connection, product_id: int) -> float:
    row = conn.execute('SELECT buyprice FROM products WHERE id=?', (product_id,)).fetchone()
    return float(row[0] if row else 0)


def _reconcile_inventory(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from Services.inventory_stock_helpers import reconcile_all_inventory
    return reconcile_all_inventory(conn.cursor(), product_type_filter=True)


def verify_inventory_vs_moves(conn: sqlite3.Connection) -> dict[str, Any]:
    """Đối chiếu inventory.quantity với SUM(stock_moves.quantity)."""
    rows = conn.execute(
        """
        SELECT p.id AS product_id, p.product_code,
               COALESCE(i.quantity, 0) AS inv_qty,
               COALESCE(i.avg_cost, 0) AS avg_cost,
               COALESCE((
                   SELECT SUM(sm.quantity) FROM stock_moves sm WHERE sm.product_id = p.id
               ), 0) AS ledger_qty
        FROM products p
        LEFT JOIN inventory i ON i.product_id = p.id
        WHERE COALESCE(p.product_type, 'goods') != 'service'
        ORDER BY p.id
        """
    ).fetchall()
    mismatches = []
    items = []
    for r in rows:
        d = dict(r)
        inv = float(d['inv_qty'] or 0)
        led = float(d['ledger_qty'] or 0)
        ok = abs(inv - led) < 0.0001
        items.append({**d, 'matched': ok, 'value': inv * float(d['avg_cost'] or 0)})
        if not ok:
            mismatches.append({
                'product_id': d['product_id'],
                'product_code': d['product_code'],
                'inventory': inv,
                'stock_moves': led,
                'diff': inv - led,
            })
    return {
        'ok': not mismatches,
        'products': len(items),
        'mismatches': mismatches,
        'items': items,
        'total_qty': sum(float(x['inv_qty'] or 0) for x in items),
        'total_value': sum(float(x['value'] or 0) for x in items),
    }


def _seed_ops_docs(conn: sqlite3.Connection, year: int) -> dict[str, Any]:
    """Phiếu nhập/bán/kho — quantity ký số; inventory luôn = SUM(stock_moves)."""
    # Đã xóa trong master; vẫn dọn sample sót nếu gọi lại
    try:
        conn.execute('DELETE FROM stock_moves')
        conn.execute('UPDATE inventory SET quantity = 0')
    except sqlite3.Error:
        pass

    # --- Nhập kho tháng 1 ---
    jan_total = sum(q * c for _, q, c in JAN_IMPORT_LINES)
    jan_vat = round(jan_total * 0.1)
    imp_cols = {r[1] for r in conn.execute('PRAGMA table_info(import)').fetchall()}
    imp_fields = ['id', 'import_no', 'date', 'supplier_id', 'bill_no', 'note', 'payment_status', 'total_value']
    imp_vals: list[Any] = [9001, 'NK-SAMPLE-01', f'{year}-01-15', 1, 'HD-NCC-01', SAMPLE_TAG, 'partial', jan_total + jan_vat]
    if 'warehouse_code' in imp_cols:
        imp_fields.append('warehouse_code')
        imp_vals.append(WH_MAIN)
    if 'paid_amount' in imp_cols:
        imp_fields.append('paid_amount')
        imp_vals.append(round((jan_total + jan_vat) * 0.6))
    conn.execute(
        f"INSERT INTO import ({','.join(imp_fields)}) VALUES ({','.join('?' for _ in imp_fields)})",
        imp_vals,
    )
    det_cols = {r[1] for r in conn.execute('PRAGMA table_info(import_details)').fetchall()}
    for pid, qty, cost in JAN_IMPORT_LINES:
        sub = qty * cost
        tax = round(sub * 0.1)
        fields = ['import_id', 'product_id', 'qty', 'buyprice', 'cost_price', 'tax', 'subtotal']
        vals: list[Any] = [9001, pid, qty, cost, cost, tax, sub]
        if 'payment_amt' in det_cols:
            fields.append('payment_amt')
            vals.append(0)
        if 'tax_pct' in det_cols:
            fields.append('tax_pct')
            vals.append(10)
        if 'warehouse_code' in det_cols:
            fields.append('warehouse_code')
            vals.append(WH_MAIN)
        name = conn.execute('SELECT name FROM products WHERE id=?', (pid,)).fetchone()
        if 'product_name' in det_cols and name:
            fields.append('product_name')
            vals.append(name[0])
        conn.execute(
            f"INSERT INTO import_details ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            vals,
        )
        _add_stock(
            conn, product_id=pid, date=f'{year}-01-15', qty_in=qty,
            typ='import', ref_no='NK-SAMPLE-01', ref_id=9001, cost=cost,
        )

    # --- Nhập bổ sung tháng 2–7 (khớp bút toán MUA-mm) ---
    monthly_imports = 0
    monthly_buys: dict[int, float] = {}
    for m in range(2, 8):
        buy = 20_000_000 + m * 1_000_000
        # Phân bổ giá trị vào SP 1–4 theo tỷ lệ cố định
        shares = ((1, 0.40), (2, 0.25), (3, 0.25), (4, 0.10))
        lines = []
        for pid, share in shares:
            unit = _product_buy(conn, pid)
            amount = buy * share
            qty = max(1, round(amount / unit))
            lines.append((pid, qty, unit, qty * unit))
        # chỉnh dòng cuối cho khớp tổng buy
        adj = buy - sum(a for *_, a in lines)
        if lines:
            pid, qty, unit, amount = lines[-1]
            # điều chỉnh qty gần nhất
            if unit > 0:
                qty2 = max(1, round((amount + adj) / unit))
                lines[-1] = (pid, qty2, unit, qty2 * unit)
        actual_buy = sum(a for *_, a in lines)
        imp_id = 9000 + m
        imp_no = f'NK-SAMPLE-{m:02d}'
        date = f'{year}-{m:02d}-08'
        vat = round(actual_buy * 0.1)
        fields = ['id', 'import_no', 'date', 'supplier_id', 'bill_no', 'note', 'payment_status', 'total_value']
        vals = [imp_id, imp_no, date, 1, f'HD-NCC-{m:02d}', SAMPLE_TAG, 'paid', actual_buy + vat]
        if 'warehouse_code' in imp_cols:
            fields.append('warehouse_code')
            vals.append(WH_MAIN)
        if 'paid_amount' in imp_cols:
            fields.append('paid_amount')
            vals.append(actual_buy + vat)
        conn.execute(
            f"INSERT INTO import ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            vals,
        )
        for pid, qty, unit, sub in lines:
            tax = round(sub * 0.1)
            dfields = ['import_id', 'product_id', 'qty', 'buyprice', 'cost_price', 'tax', 'subtotal']
            dvals: list[Any] = [imp_id, pid, qty, unit, unit, tax, sub]
            if 'payment_amt' in det_cols:
                dfields.append('payment_amt')
                dvals.append(sub + tax)
            if 'tax_pct' in det_cols:
                dfields.append('tax_pct')
                dvals.append(10)
            conn.execute(
                f"INSERT INTO import_details ({','.join(dfields)}) VALUES ({','.join('?' for _ in dfields)})",
                dvals,
            )
            _add_stock(
                conn, product_id=pid, date=date, qty_in=qty,
                typ='import', ref_no=imp_no, ref_id=imp_id, cost=unit,
            )
        monthly_imports += 1
        monthly_buys[m] = actual_buy

    # --- Bán tháng 1 ---
    sale_n = 0
    sid = 9100
    date = f'{year}-01-22'
    total = sum(q * p for _, q, p in JAN_SALE_ITEMS)
    tax = round(total * 0.1)
    conn.execute(
        """
        INSERT INTO sale (id, date, total_amount, payment_method, customer_name, status, tax_amount, invoice_number, note)
        VALUES (?,?,?,?,?,'completed',?,?,?)
        """,
        (sid, date, total + tax, 'Tiền mặt', 'Khách lẻ', tax, f'HD-{sid}', SAMPLE_TAG),
    )
    jan_cogs = 0.0
    for pid, qty, price in JAN_SALE_ITEMS:
        cost = _add_stock(
            conn, product_id=pid, date=date, qty_out=qty,
            typ='SALE', ref_no=f'HD-{sid}', ref_id=sid, cost=0,
        )
        jan_cogs += qty * cost
        conn.execute(
            'INSERT INTO sale_items (sale_id, product_id, quantity, price, cost_price) VALUES (?,?,?,?,?)',
            (sid, pid, qty, price, cost),
        )
    sale_n += 1
    sid += 1

    # --- Bán tháng 2–7 ---
    for month, day, cust, pay, items in SALE_PLANS:
        date = f'{year}-{month:02d}-{day:02d}'
        total = sum(q * p for _, q, p in items)
        tax = round(total * 0.1)
        conn.execute(
            """
            INSERT INTO sale (id, date, total_amount, payment_method, customer_name, status, tax_amount, invoice_number, note)
            VALUES (?,?,?,?,?,'completed',?,?,?)
            """,
            (sid, date, total + tax, pay, cust, tax, f'HD-{sid}', SAMPLE_TAG),
        )
        for pid, qty, price in items:
            cost = _add_stock(
                conn, product_id=pid, date=date, qty_out=qty,
                typ='SALE', ref_no=f'HD-{sid}', ref_id=sid, cost=0,
            )
            conn.execute(
                'INSERT INTO sale_items (sale_id, product_id, quantity, price, cost_price) VALUES (?,?,?,?,?)',
                (sid, pid, qty, price, cost),
            )
        sale_n += 1
        sid += 1

    fixes = _reconcile_inventory(conn)
    check = verify_inventory_vs_moves(conn)
    return {
        'imports': 1 + monthly_imports,
        'sales': sale_n,
        'jan_revenue': sum(q * p for _, q, p in JAN_SALE_ITEMS),
        'jan_cogs': jan_cogs,
        'jan_import_value': jan_total,
        'monthly_buys': monthly_buys,
        'inventory_reconcile_fixes': len(fixes),
        'inventory_matched': check['ok'],
        'inventory_total_qty': check['total_qty'],
        'inventory_total_value': check['total_value'],
        'inventory_mismatches': check['mismatches'],
    }


def seed_sample_journals(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
    close_through: int | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Seed nhật ký + master data mẫu trên ``conn`` (DB tenant)."""
    year = fiscal_year or datetime.now().year
    # Đóng kỳ đến tháng trước (giữ tháng hiện tại mở) — tối đa 6 nếu năm demo đầy đủ
    now = datetime.now()
    if close_through is None:
        if year < now.year:
            close_through = 12
        elif year == now.year:
            close_through = max(1, now.month - 1)
        else:
            close_through = 0
    close_through = max(0, min(12, int(close_through)))

    ensure_sme_accounting_ready(conn, accounting_regime='SME_TT99', commit=False)
    _ensure_ops_tables(conn)
    cleared = clear_sample_journals(conn, commit=False)
    master = _seed_master_data(conn)
    ops = _seed_ops_docs(conn, year)

    seq = 1
    entries = []

    def P(date, dtype, dno, desc, lines):
        nonlocal seq
        e = _post(conn, date=date, doc_type=dtype, doc_no=dno, doc_id=900000 + seq, desc=desc, lines=lines)
        seq += 1
        entries.append(e.get('id') or e.get('entry_id'))
        return e

    jan_buy = float(ops.get('jan_import_value') or 300_000_000)
    jan_vat_in = round(jan_buy * 0.1)
    jan_rev = float(ops.get('jan_revenue') or 30_000_000)
    jan_cogs = float(ops.get('jan_cogs') or 18_000_000)
    jan_vat_out = round(jan_rev * 0.1)
    monthly_buys = ops.get('monthly_buys') or {}

    # --- Tháng 1: vốn + ngân hàng + TSCĐ + nhập kho ---
    P(f'{year}-01-02', 'VON', 'VON-01', 'Góp vốn bằng tiền mặt', [
        {'account_code': '1111', 'debit': 500_000_000, 'credit': 0, 'description': 'Nhận vốn'},
        {'account_code': '4111', 'debit': 0, 'credit': 500_000_000, 'description': 'Vốn CSH'},
    ])
    P(f'{year}-01-05', 'TMNH', 'CK-01', 'Nộp tiền mặt vào NH', [
        {'account_code': '1121', 'debit': 400_000_000, 'credit': 0},
        {'account_code': '1111', 'debit': 0, 'credit': 400_000_000},
    ])
    P(f'{year}-01-10', 'TSCD', 'TSCD-01', 'Mua máy móc thiết bị (có VAT)', [
        {'account_code': '2112', 'debit': 50_000_000, 'credit': 0},
        {'account_code': '1332', 'debit': 5_000_000, 'credit': 0},
        {'account_code': '1121', 'debit': 0, 'credit': 55_000_000},
    ])
    P(f'{year}-01-15', 'MUA', 'MUA-01', 'Nhập hàng hóa NCC (VAT 10%)', [
        {'account_code': '156', 'debit': jan_buy, 'credit': 0},
        {'account_code': '13311', 'debit': jan_vat_in, 'credit': 0},
        {'account_code': '331', 'debit': 0, 'credit': jan_buy + jan_vat_in},
    ])
    pay_ncc_1 = round((jan_buy + jan_vat_in) * 0.6)
    P(f'{year}-01-20', 'TTNCC', 'TT-01', 'Thanh toán một phần công nợ NCC', [
        {'account_code': '331', 'debit': pay_ncc_1, 'credit': 0},
        {'account_code': '1121', 'debit': 0, 'credit': pay_ncc_1},
    ])
    P(f'{year}-01-25', 'CHI', 'CP-01', 'Chi phí QLDN tháng 1 (tiền mặt)', [
        {'account_code': '642', 'debit': 3_000_000, 'credit': 0},
        {'account_code': '1111', 'debit': 0, 'credit': 3_000_000},
    ])
    P(f'{year}-01-28', 'LUONG', 'L-01', 'Trích lương + BHXH T1', [
        {'account_code': '642', 'debit': 10_000_000, 'credit': 0},
        {'account_code': '3341', 'debit': 0, 'credit': 8_500_000},
        {'account_code': '3383', 'debit': 0, 'credit': 1_500_000},
    ])
    P(f'{year}-01-30', 'TRL', 'TRL-01', 'Chi trả lương T1', [
        {'account_code': '3341', 'debit': 8_500_000, 'credit': 0},
        {'account_code': '1121', 'debit': 0, 'credit': 8_500_000},
    ])
    P(f'{year}-01-31', 'KH', 'KH-01', 'Khấu hao TSCĐ T1', [
        {'account_code': '642', 'debit': 1_000_000, 'credit': 0},
        {'account_code': '2141', 'debit': 0, 'credit': 1_000_000},
    ])

    # Tháng 1 bán lẻ — khớp phiếu bán / stock_moves
    P(f'{year}-01-22', 'BAN', 'BAN-01', 'Doanh thu tháng 1', [
        {'account_code': '1111', 'debit': jan_rev + jan_vat_out, 'credit': 0},
        {'account_code': '5111', 'debit': 0, 'credit': jan_rev},
        {'account_code': '33311', 'debit': 0, 'credit': jan_vat_out},
    ])
    P(f'{year}-01-22', 'GV', 'GV-01', 'Giá vốn tháng 1', [
        {'account_code': '63211', 'debit': jan_cogs, 'credit': 0},
        {'account_code': '156', 'debit': 0, 'credit': jan_cogs},
    ])

    # Doanh số / GV / chi phí các tháng 2–7 (đồng bộ sale seed)
    def month_plan(m: int) -> dict:
        buy_cost = {1: 90000, 2: 150000, 3: 25000, 4: 40000, 5: 12000, 6: 80000}
        rev = cogs = cash = bank = ar = 0.0
        for month, _day, _cust, pay, items in SALE_PLANS:
            if month != m:
                continue
            sub = sum(q * p for _, q, p in items)
            cg = sum(q * buy_cost.get(pid, 0) for pid, q, _p in items)
            vat = round(sub * 0.1)
            gross = sub + vat
            rev += sub
            cogs += cg
            if pay == 'Tiền mặt':
                cash += gross
            elif pay == 'Chuyển khoản':
                bank += gross
            else:
                ar += gross
        return {
            'rev': rev, 'cogs': cogs, 'cash': cash, 'bank': bank, 'ar': ar,
            'vat_out': round(rev * 0.1),
            'buy': float(monthly_buys.get(m) or (20_000_000 + m * 1_000_000)),
        }

    for m in range(2, 8):
        mp = month_plan(m)
        d_sale = f'{year}-{m:02d}-15'
        # Bán hàng: Nợ tiền/NH/PT — Có 5111 + 33311
        lines = []
        if mp['cash']:
            lines.append({'account_code': '1111', 'debit': mp['cash'], 'credit': 0, 'description': 'Thu TM'})
        if mp['bank']:
            lines.append({'account_code': '1121', 'debit': mp['bank'], 'credit': 0, 'description': 'Thu NH'})
        if mp['ar']:
            lines.append({'account_code': '131', 'debit': mp['ar'], 'credit': 0, 'description': 'Phải thu'})
        lines.append({'account_code': '5111', 'debit': 0, 'credit': mp['rev'], 'description': 'DT HH'})
        lines.append({'account_code': '33311', 'debit': 0, 'credit': mp['vat_out'], 'description': 'VAT ra'})
        P(d_sale, 'BAN', f'BAN-{m:02d}', f'Ghi nhận doanh thu T{m}', lines)

        P(d_sale, 'GV', f'GV-{m:02d}', f'Xuất giá vốn T{m}', [
            {'account_code': '63211', 'debit': mp['cogs'], 'credit': 0},
            {'account_code': '156', 'debit': 0, 'credit': mp['cogs']},
        ])

        # Mua thêm hàng mỗi tháng — khớp phiếu nhập / stock_moves
        buy = mp['buy']
        vat_in = round(buy * 0.1)
        P(f'{year}-{m:02d}-08', 'MUA', f'MUA-{m:02d}', f'Nhập hàng bổ sung T{m}', [
            {'account_code': '156', 'debit': buy, 'credit': 0},
            {'account_code': '13311', 'debit': vat_in, 'credit': 0},
            {'account_code': '331', 'debit': 0, 'credit': buy + vat_in},
        ])
        # Thanh toán NCC
        pay = buy + vat_in
        P(f'{year}-{m:02d}-18', 'TTNCC', f'TT-{m:02d}', f'Thanh toán NCC T{m}', [
            {'account_code': '331', 'debit': pay, 'credit': 0},
            {'account_code': '1121', 'debit': 0, 'credit': pay},
        ])
        # Thu công nợ đại lý (một phần)
        if mp['ar']:
            collect = round(mp['ar'] * 0.7)
            P(f'{year}-{m:02d}-22', 'THUNO', f'THU-{m:02d}', f'Thu công nợ đại lý T{m}', [
                {'account_code': '1121', 'debit': collect, 'credit': 0},
                {'account_code': '131', 'debit': 0, 'credit': collect},
            ])

        P(f'{year}-{m:02d}-25', 'CHI', f'CPBH-{m:02d}', f'Chi phí bán hàng T{m}', [
            {'account_code': '641', 'debit': 1_500_000, 'credit': 0},
            {'account_code': '1111', 'debit': 0, 'credit': 1_500_000},
        ])
        P(f'{year}-{m:02d}-26', 'CHI', f'CPQL-{m:02d}', f'Chi phí QLDN T{m}', [
            {'account_code': '642', 'debit': 2_500_000, 'credit': 0},
            {'account_code': '1121', 'debit': 0, 'credit': 2_500_000},
        ])
        P(f'{year}-{m:02d}-27', 'LUONG', f'L-{m:02d}', f'Trích lương T{m}', [
            {'account_code': '642', 'debit': 10_000_000, 'credit': 0},
            {'account_code': '3341', 'debit': 0, 'credit': 8_500_000},
            {'account_code': '3383', 'debit': 0, 'credit': 1_500_000},
        ])
        P(f'{year}-{m:02d}-28', 'TRL', f'TRL-{m:02d}', f'Chi lương T{m}', [
            {'account_code': '3341', 'debit': 8_500_000, 'credit': 0},
            {'account_code': '1121', 'debit': 0, 'credit': 8_500_000},
        ])
        P(f'{year}-{m:02d}-{calendar.monthrange(year, m)[1]:02d}', 'KH', f'KH-{m:02d}', f'Khấu hao T{m}', [
            {'account_code': '642', 'debit': 1_000_000, 'credit': 0},
            {'account_code': '2141', 'debit': 0, 'credit': 1_000_000},
        ])

    # Thu nhập / chi phí khác tháng 6
    P(f'{year}-06-12', 'TN', 'TN-01', 'Thu nhập khác', [
        {'account_code': '1111', 'debit': 2_000_000, 'credit': 0},
        {'account_code': '711', 'debit': 0, 'credit': 2_000_000},
    ])
    P(f'{year}-06-14', 'CPK', 'CPK-01', 'Chi phí khác', [
        {'account_code': '811', 'debit': 500_000, 'credit': 0},
        {'account_code': '1111', 'debit': 0, 'credit': 500_000},
    ])

    features = {
        'journal_posting': True,
        'auto_vat_settlement': True,
        'auto_period_close': True,
        'auto_depreciation': True,
        'auto_lock_period': False,
    }
    closed = []
    vat_runs = []
    for m in range(1, close_through + 1):
        vat = run_vat_settlement(
            conn,
            fiscal_year=year,
            period=m,
            accounting_regime='SME_TT99',
            features=features,
            created_by='sample_seed',
            replace_existing=True,
        )
        vat_runs.append({
            'period': m,
            'posted': vat.get('posted'),
            'entry_id': vat.get('entry_id'),
            'offset_amount': vat.get('offset_amount'),
            'vat_payable': vat.get('vat_payable'),
            'reason': vat.get('reason'),
        })
        clo = run_period_close(
            conn,
            fiscal_year=year,
            period=m,
            accounting_regime='SME_TT99',
            features=features,
            created_by='sample_seed',
            replace_existing=True,
        )
        closed.append({'period': m, 'posted': clo.get('posted'), 'entry_ids': clo.get('entry_ids'), 'reason': clo.get('reason')})

    if commit:
        sqlite_commit(conn, label='sample_data')

    inv_check = verify_inventory_vs_moves(conn)
    if not inv_check['ok']:
        _reconcile_inventory(conn)
        inv_check = verify_inventory_vs_moves(conn)
        if commit:
            sqlite_commit(conn, label='sample_data')

    return {
        'fiscal_year': year,
        'cleared_entries': cleared,
        'posted_sample_entries': len(entries),
        'close_through': close_through,
        'vat_runs': vat_runs,
        'period_closes': closed,
        'master': master,
        'ops': ops,
        'inventory_check': inv_check,
        'tag': SAMPLE_TAG,
    }


def ensure_demo_user(conn: sqlite3.Connection, *, username: str = 'sme_demo', password: str = 'admin123') -> None:
    try:
        from flask_bcrypt import Bcrypt
        from flask import Flask
        app = Flask('seed')
        bcrypt = Bcrypt(app)
        pwd = bcrypt.generate_password_hash(password).decode('utf-8')
    except Exception:
        import hashlib
        pwd = hashlib.sha256(password.encode()).hexdigest()
    conn.execute('DELETE FROM users WHERE username=?', (username,))
    conn.execute(
        'INSERT INTO users (username, password, role, full_name) VALUES (?,?,?,?)',
        (username, pwd, 'admin', 'Admin Demo SME'),
    )


def register_demo_tenant(
    *,
    tenant_id: str = 'sme_demo',
    db_rel_path: str = 'tenants/sme_demo.db',
    username: str = '0909000111',
    password: str = 'admin123',
    force: bool = True,
    fiscal_year: int | None = None,
    close_through: int | None = None,
) -> dict[str, Any]:
    """Tạo tenant demo SME (TT99), seed sổ/báo cáo, map user đăng nhập."""
    from db_utils import BASE_DIR, sqlite_commit
    from tenant_middleware import init_tenant_database, add_user_to_mapping
    from Services.tenant_profile import update_registry_settings

    abs_db = os.path.join(BASE_DIR, 'tenants', f'{tenant_id}.db')
    # Giữ path chuẩn tenants/<id>.db (init_tenant_database luôn dùng pattern này)
    if force and os.path.exists(abs_db):
        os.remove(abs_db)

    init_tenant_database(
        tenant_id,
        business_name='Công ty TNHH Demo SME',
        phone=username,
        email='ketoan@demosme.vn',
        contact_email='ketoan@demosme.vn',
        tax_code='0319999888',
        representative_name='Nguyễn Văn Demo',
        accounting_regime='SME_TT99',
        business_line='pos',
        customer_password=password,
        support_password=password,
        empty_business_data=True,
        settings_json={
            'onboarding_completed': True,
            'sample_data': True,
            'vat_filing_period': 'monthly',
            'filing_period': 'monthly',
            'plan': 'demo',
        },
    )

    from db_utils import open_sqlite

    with open_sqlite(abs_db) as conn:
        # Cho phép đăng nhập ngay (không bắt đổi MK)
        try:
            conn.execute(
                "UPDATE users SET must_change_password=0 WHERE username=?",
                (username,),
            )
        except sqlite3.Error:
            pass
        summary = seed_sample_journals(
            conn,
            fiscal_year=fiscal_year,
            close_through=close_through,
            commit=True,
        )
        # Đảm bảo business_info vẫn SME sau seed
        conn.execute(
            """
            UPDATE business_info SET
                business_name=?, tax_code=?, address=?, phone=?, email=?,
                representative_name=?, accounting_regime='SME_TT99', filing_period='monthly'
            WHERE id=(SELECT id FROM business_info LIMIT 1)
            """,
            (
                'Công ty TNHH Demo SME',
                '0319999888',
                '123 Nguyễn Huệ, Q.1, TP.HCM',
                username,
                'ketoan@demosme.vn',
                'Nguyễn Văn Demo',
            ),
        )
        sqlite_commit(conn, label='sample_data')

    add_user_to_mapping(username, 'ketoan@demosme.vn', tenant_id)
    update_registry_settings(
        tenant_id,
        {
            'accounting_regime': 'SME_TT99',
            'vat_filing_period': 'monthly',
            'filing_period': 'monthly',
            'onboarding_completed': True,
            'sample_data': True,
        },
    )

    return {
        'tenant_id': tenant_id,
        'db_path': abs_db,
        'db_rel_path': f'tenants/{tenant_id}.db',
        'username': username,
        'password': password,
        'seed': summary,
    }
