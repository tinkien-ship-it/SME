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

SAMPLE_TAG = 'SME_SAMPLE'
SAMPLE_PREFIX = 'SAMPLE_'


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
        conn.commit()
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
    conn.execute('DELETE FROM business_info')
    conn.execute(
        """
        INSERT INTO business_info (
            business_name, tax_code, address, phone, email, representative_name,
            bank_account, bank_name, account_holder, accounting_regime, filing_period
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            'Công ty TNHH Demo SME',
            '0319999888',
            '123 Nguyễn Huệ, Q.1, TP.HCM',
            '0909000111',
            'ketoan@demosme.vn',
            'Nguyễn Văn Demo',
            '0123456789',
            'Vietcombank',
            'CONG TY TNHH DEMO SME',
            'SME_TT99',
            'monthly',
        ),
    )

    conn.execute('DELETE FROM products')
    products = [
        (1, 'SP-GAO', 'Gạo Jasmine 5kg', 'Bao', 90000, 120000),
        (2, 'SP-CAFE', 'Cà phê rang xay 1kg', 'Kg', 150000, 220000),
        (3, 'SP-SUA', 'Sữa tươi hộp 1L', 'Hộp', 25000, 35000),
        (4, 'SP-TRA', 'Trà túi lọc hộp', 'Hộp', 40000, 55000),
    ]
    for pid, code, name, unit, buy, price in products:
        conn.execute(
            'INSERT INTO products (id, product_code, name, unit, buyprice, price) VALUES (?,?,?,?,?,?)',
            (pid, code, name, unit, buy, price),
        )
        conn.execute(
            'INSERT OR REPLACE INTO inventory (product_id, quantity, avg_cost) VALUES (?,?,?)',
            (pid, 0, buy),
        )

    conn.execute('DELETE FROM customers')
    customers = [
        (1, 'Đại lý ABC', '0901111222', 'Q.3, TP.HCM', '0301111222'),
        (2, 'Siêu thị Mini Mart', '0903333444', 'Q.7, TP.HCM', '0303333444'),
        (3, 'Khách lẻ', '', '', ''),
    ]
    for row in customers:
        conn.execute(
            'INSERT INTO customers (id, name, phone, address, tax_code) VALUES (?,?,?,?,?)',
            row,
        )

    conn.execute('DELETE FROM suppliers')
    conn.execute(
        "INSERT INTO suppliers (id, code, name, phone, address, tax_code) VALUES (1,'NCC01','NCC Thực phẩm Đồng Nai','0251123456','Biên Hòa','3601234567')"
    )
    conn.execute(
        "INSERT INTO suppliers (id, code, name, phone, address, tax_code) VALUES (2,'NCC02','NCC Bao bì Minh Phát','0287654321','Bình Tân','0307654321')"
    )
    return {'products': len(products), 'customers': len(customers)}


def _add_stock(conn, *, product_id: int, date: str, qty_in: float = 0, qty_out: float = 0,
               typ: str = 'IMPORT', ref_no: str = '', cost: float = 0):
    cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_moves)').fetchall()}
    fields = ['product_id', 'date', 'type', 'in_quantity', 'out_quantity', 'quantity', 'cost_price', 'note']
    values = [product_id, date, typ, qty_in, qty_out, qty_in or qty_out, cost, 'sample']
    if 'ref_document' in cols:
        fields.append('ref_document')
        values.append(SAMPLE_TAG)
    if 'ref_no' in cols:
        fields.append('ref_no')
        values.append(ref_no)
    elif 'ref_type' in cols:
        fields.append('ref_type')
        values.append(ref_no or typ)
    if 'avg_cost' in cols:
        fields.append('avg_cost')
        values.append(cost)
    if 'total_value' in cols:
        fields.append('total_value')
        values.append((qty_in or qty_out) * cost)
    placeholders = ','.join('?' for _ in fields)
    conn.execute(
        f"INSERT INTO stock_moves ({','.join(fields)}) VALUES ({placeholders})",
        values,
    )
    inv = conn.execute('SELECT quantity, avg_cost FROM inventory WHERE product_id=?', (product_id,)).fetchone()
    cur_q = float(inv[0] if inv else 0)
    cur_c = float(inv[1] if inv else cost)
    if qty_in:
        new_q = cur_q + qty_in
        new_c = ((cur_q * cur_c) + (qty_in * cost)) / new_q if new_q else cost
        conn.execute(
            'INSERT OR REPLACE INTO inventory (product_id, quantity, avg_cost) VALUES (?,?,?)',
            (product_id, new_q, new_c),
        )
    elif qty_out:
        conn.execute(
            'UPDATE inventory SET quantity = COALESCE(quantity,0) - ? WHERE product_id=?',
            (qty_out, product_id),
        )


def _seed_ops_docs(conn: sqlite3.Connection, year: int) -> dict[str, int]:
    """Phiếu nhập/bán/kho đồng bộ với nhật ký (để test 01-BH/02-BH, PO, tồn)."""
    conn.execute("DELETE FROM sale_items WHERE sale_id IN (SELECT id FROM sale WHERE note=?)", (SAMPLE_TAG,))
    conn.execute('DELETE FROM sale WHERE note=?', (SAMPLE_TAG,))
    conn.execute("DELETE FROM import_details WHERE import_id IN (SELECT id FROM import WHERE note=?)", (SAMPLE_TAG,))
    conn.execute('DELETE FROM import WHERE note=?', (SAMPLE_TAG,))
    conn.execute('DELETE FROM stock_moves WHERE ref_document=?', (SAMPLE_TAG,))

    # Nhập kho tháng 1 — đủ hàng cho bán cả năm mẫu
    conn.execute(
        """
        INSERT INTO import (id, import_no, date, supplier_id, bill_no, note, payment_status, total_value)
        VALUES (9001, 'NK-SAMPLE-01', ?, 1, 'HD-NCC-01', ?, 'partial', 330000000)
        """,
        (f'{year}-01-15', SAMPLE_TAG),
    )
    details = [
        (9001, 1, 1500, 90000, 90000, 13500000, 135000000),
        (9001, 2, 600, 150000, 150000, 9000000, 90000000),
        (9001, 3, 2400, 25000, 25000, 6000000, 60000000),
        (9001, 4, 375, 40000, 40000, 1500000, 15000000),
    ]
    for import_id, pid, qty, buy, cost, tax, sub in details:
        conn.execute(
            """
            INSERT INTO import_details (import_id, product_id, qty, buyprice, cost_price, tax, subtotal, payment_amt)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (import_id, pid, qty, buy, cost, tax, sub, 0),
        )
        _add_stock(conn, product_id=pid, date=f'{year}-01-15', qty_in=qty, typ='IMPORT',
                   ref_no='NK-SAMPLE-01', cost=cost)

    sale_n = 0
    # Bán cho đại lý + siêu thị các tháng 2–7
    plans = [
        # (month, day, customer, payment, items[(pid,qty,price)])
        (2, 12, 'Đại lý ABC', 'Công nợ', [(1, 120, 120000), (2, 60, 220000)]),
        (2, 20, 'Siêu thị Mini Mart', 'Chuyển khoản', [(3, 300, 35000), (4, 90, 55000)]),
        (3, 8, 'Đại lý ABC', 'Công nợ', [(1, 150, 120000), (3, 240, 35000)]),
        (3, 18, 'Siêu thị Mini Mart', 'Tiền mặt', [(2, 75, 220000)]),
        (4, 5, 'Đại lý ABC', 'Công nợ', [(1, 180, 120000), (4, 120, 55000)]),
        (4, 22, 'Siêu thị Mini Mart', 'Chuyển khoản', [(2, 90, 220000), (3, 360, 35000)]),
        (5, 10, 'Đại lý ABC', 'Công nợ', [(1, 135, 120000), (2, 45, 220000)]),
        (5, 25, 'Siêu thị Mini Mart', 'Tiền mặt', [(3, 450, 35000), (4, 150, 55000)]),
        (6, 7, 'Đại lý ABC', 'Công nợ', [(1, 165, 120000), (3, 270, 35000)]),
        (6, 19, 'Siêu thị Mini Mart', 'Chuyển khoản', [(2, 84, 220000)]),
        (7, 9, 'Đại lý ABC', 'Công nợ', [(1, 105, 120000), (4, 75, 55000)]),
        (7, 21, 'Siêu thị Mini Mart', 'Tiền mặt', [(2, 54, 220000), (3, 300, 35000)]),
    ]
    sid = 9100
    for month, day, cust, pay, items in plans:
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
            cost = float(conn.execute('SELECT avg_cost FROM inventory WHERE product_id=?', (pid,)).fetchone()[0] or 0)
            conn.execute(
                'INSERT INTO sale_items (sale_id, product_id, quantity, price, cost_price) VALUES (?,?,?,?,?)',
                (sid, pid, qty, price, cost),
            )
            _add_stock(conn, product_id=pid, date=date, qty_out=qty, typ='SALE', ref_no=f'HD-{sid}', cost=cost)
        sale_n += 1
        sid += 1
    return {'imports': 1, 'sales': sale_n}


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
        {'account_code': '156', 'debit': 300_000_000, 'credit': 0},
        {'account_code': '13311', 'debit': 30_000_000, 'credit': 0},
        {'account_code': '331', 'debit': 0, 'credit': 330_000_000},
    ])
    P(f'{year}-01-20', 'TTNCC', 'TT-01', 'Thanh toán một phần công nợ NCC', [
        {'account_code': '331', 'debit': 200_000_000, 'credit': 0},
        {'account_code': '1121', 'debit': 0, 'credit': 200_000_000},
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

    # Tháng 1 cũng có bán lẻ để kỳ đầu có DT
    P(f'{year}-01-22', 'BAN', 'BAN-01', 'Doanh thu tháng 1', [
        {'account_code': '1111', 'debit': 33_000_000, 'credit': 0},
        {'account_code': '5111', 'debit': 0, 'credit': 30_000_000},
        {'account_code': '33311', 'debit': 0, 'credit': 3_000_000},
    ])
    P(f'{year}-01-22', 'GV', 'GV-01', 'Giá vốn tháng 1', [
        {'account_code': '6321', 'debit': 18_000_000, 'credit': 0},
        {'account_code': '156', 'debit': 0, 'credit': 18_000_000},
    ])

    # Doanh số / GV / chi phí các tháng 2–7 (đồng bộ sale seed)
    def month_plan(m: int) -> dict:
        """Tính DT/GV/VAT/thu tiền theo plans đã seed."""
        plans = {
            2: [('Đại lý ABC', 'Công nợ', [(1, 120, 120000, 90000), (2, 60, 220000, 150000)]),
                ('Siêu thị Mini Mart', 'Chuyển khoản', [(3, 300, 35000, 25000), (4, 90, 55000, 40000)])],
            3: [('Đại lý ABC', 'Công nợ', [(1, 150, 120000, 90000), (3, 240, 35000, 25000)]),
                ('Siêu thị Mini Mart', 'Tiền mặt', [(2, 75, 220000, 150000)])],
            4: [('Đại lý ABC', 'Công nợ', [(1, 180, 120000, 90000), (4, 120, 55000, 40000)]),
                ('Siêu thị Mini Mart', 'Chuyển khoản', [(2, 90, 220000, 150000), (3, 360, 35000, 25000)])],
            5: [('Đại lý ABC', 'Công nợ', [(1, 135, 120000, 90000), (2, 45, 220000, 150000)]),
                ('Siêu thị Mini Mart', 'Tiền mặt', [(3, 450, 35000, 25000), (4, 150, 55000, 40000)])],
            6: [('Đại lý ABC', 'Công nợ', [(1, 165, 120000, 90000), (3, 270, 35000, 25000)]),
                ('Siêu thị Mini Mart', 'Chuyển khoản', [(2, 84, 220000, 150000)])],
            7: [('Đại lý ABC', 'Công nợ', [(1, 105, 120000, 90000), (4, 75, 55000, 40000)]),
                ('Siêu thị Mini Mart', 'Tiền mặt', [(2, 54, 220000, 150000), (3, 300, 35000, 25000)])],
        }
        rev = cogs = cash = bank = ar = 0.0
        for _cust, pay, items in plans.get(m, []):
            sub = sum(q * p for _, q, p, _c in items)
            cg = sum(q * c for _, q, _p, c in items)
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
        return {'rev': rev, 'cogs': cogs, 'cash': cash, 'bank': bank, 'ar': ar, 'vat_out': round(rev * 0.1)}

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
            {'account_code': '6321', 'debit': mp['cogs'], 'credit': 0},
            {'account_code': '156', 'debit': 0, 'credit': mp['cogs']},
        ])

        # Mua thêm hàng mỗi tháng (trừ T1 đã mua lớn)
        buy = 20_000_000 + m * 1_000_000
        vat_in = round(buy * 0.1)
        P(f'{year}-{m:02d}-08', 'MUA', f'MUA-{m:02d}', f'Nhập hàng bổ sung T{m}', [
            {'account_code': '156', 'debit': buy, 'credit': 0},
            {'account_code': '13311', 'debit': vat_in, 'credit': 0},
            {'account_code': '331', 'debit': 0, 'credit': buy + vat_in},
        ])
        # Thanh toán NCC
        pay = buy + vat_in - 2_000_000
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
        conn.commit()

    return {
        'fiscal_year': year,
        'cleared_entries': cleared,
        'posted_sample_entries': len(entries),
        'close_through': close_through,
        'vat_runs': vat_runs,
        'period_closes': closed,
        'master': master,
        'ops': ops,
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
    from db_utils import BASE_DIR
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

    conn = sqlite3.connect(abs_db)
    conn.row_factory = sqlite3.Row
    try:
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
        conn.commit()
    finally:
        conn.close()

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
