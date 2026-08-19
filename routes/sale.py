"""Routes bán hàng (POS / Sale) — tách từ app.py."""
import json
import logging
import sqlite3
import traceback
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from urllib.parse import quote

import pandas as pd
import requests
from flask import (
    Response,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import login_required

from db_utils import begin_immediate, get_db_connection, rollback_quietly, _is_locked_error
from Services.customer_utils import normalize_tax_code_digits, tax_code_validation_message
from Services.invoice_buyer import DEFAULT_RETAIL_BUYER_NAME, is_retail_buyer_name, normalize_retail_buyer_name
from Services.hkd_sector import requires_stock_check
from Services.inventory_stock_helpers import (
    revert_sale_stock,
    sync_inventory_quantity_from_moves,
    apply_wac_inbound,
)
from Services.sale_helpers import (
    deduct_inventory_for_sale,
    fetch_product_for_checkout,
    insert_pos_sale_item,
    snapshot_item_hkd_sector,
    table_has_column,
)
from Services.sme.sale_journal import sync_sale_journals
from Services.accounting_queue import enqueue_accounting_job
from Services.sme.hkd_side_effects import write_hkd_cash_vouchers
from Services.tenant_profile import get_current_tenant_profile
from Services.pos_offline_schema import ensure_pos_offline_schema, find_sale_by_client_uuid
from Services.pos_catalog import fetch_pos_catalog


def _apply_sale_business_line(cursor, update_sql, update_params, ref_doc):
    """Chèn business_line='pos' đúng vị trí — tránh lệch cột budget/passport."""
    if not table_has_column(cursor, 'sale', 'business_line'):
        return update_sql, update_params
    update_sql = update_sql.replace('sale_no = ?', 'sale_no = ?, business_line = ?')
    idx = update_params.index(ref_doc) + 1
    update_params.insert(idx, 'pos')
    return update_sql, update_params


def ensure_customer(cursor, name, company_name, phone, address, tax_code, email,
                    budget_unit_code=None, passport_no=None):
    """Đảm bảo khách hàng tồn tại trong database."""
    if is_retail_buyer_name(name):
        return None

    name = name.strip()
    company_name = (company_name or "").strip()
    phone = (phone or "").strip() or None
    address = (address or "").strip() or None
    tax_code = (tax_code or "").strip() or None
    email = (email or "").strip() or None
    budget_unit_code = (budget_unit_code or "").strip() or None
    passport_no = (passport_no or "").strip() or None

    customer_id = None

    if tax_code:
        cursor.execute("SELECT id FROM customers WHERE tax_code = ?", (tax_code,))
        row = cursor.fetchone()
        if row:
            customer_id = row["id"]

    if not customer_id:
        cursor.execute(
            """
            SELECT id FROM customers
            WHERE name = ?
              AND (phone = ? OR (phone IS NULL AND ? IS NULL))
        """,
            (name, phone, phone),
        )
        row = cursor.fetchone()
        if row:
            customer_id = row["id"]

    if customer_id:
        cursor.execute(
            """
            UPDATE customers
            SET name = ?,
                company_name = ?,
                phone = ?,
                address = ?,
                tax_code = ?,
                email = ?,
                budget_unit_code = ?,
                passport_no = ?
            WHERE id = ?
        """,
            (name, company_name, phone, address, tax_code, email,
             budget_unit_code, passport_no, customer_id),
        )
        return customer_id

    cursor.execute(
        """
        INSERT INTO customers (name, company_name, phone, address, tax_code, email,
                               budget_unit_code, passport_no)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (name, company_name, phone, address, tax_code, email,
         budget_unit_code, passport_no),
    )
    return cursor.lastrowid


def get_sale_details(order_id, db_session):
    """Lấy thông tin đơn hàng từ bảng sale + sale_items + products."""
    try:
        cur = db_session.cursor()
        cur.execute(
            """
            SELECT
                id, customer_name, company_name, tax_code,
                address, invoice_number, discount_amount, total_amount, date, status
            FROM sale
            WHERE id = ?
        """,
            (order_id,),
        )
        sale = cur.fetchone()
        if not sale:
            print(f"[INFO] Không tìm thấy đơn hàng ID = {order_id}")
            return None

        cur.execute(
            """
            SELECT
                si.product_id,
                p.product_code,
                p.name AS product_name,
                si.quantity,
                si.price,
                si.UseSaleUnit
            FROM sale_items si
            JOIN products p ON si.product_id = p.id
            WHERE si.sale_id = ?
        """,
            (order_id,),
        )
        items = cur.fetchall()
        sale_dict = dict(sale)
        sale_dict["items"] = [dict(item) for item in items]
        if sale_dict.get("date"):
            sale_dict["date"] = sale_dict["date"][:10]
        print(f"[OK] Tải thành công đơn hàng ĐH{order_id:06d} – {len(items)} sản phẩm")
        return sale_dict
    except Exception as e:
        print(f"[DB ERROR] get_sale_details({order_id}): {e}")
        traceback.print_exc()
        return None


def _calc_sale_line_amounts(quantity, unit_price, discount_pct, tax_pct, line_total_raw=None):
    """Tính chiết khấu, thuế, tổng thanh toán cho một dòng sale_items."""
    qty = float(quantity or 0)
    price = float(unit_price or 0)
    disc_pct = float(discount_pct or 0)
    tax_pct = float(tax_pct or 0)
    line_sub = round(qty * price)
    discount_amount = round(line_sub * disc_pct / 100)
    after_discount = line_sub - discount_amount
    tax_amount = round(after_discount * tax_pct / 100)
    if line_total_raw is not None and float(line_total_raw or 0) > 0:
        line_total = round(float(line_total_raw))
    else:
        line_total = after_discount + tax_amount
    return {
        "discount_amount": discount_amount,
        "tax_amount": tax_amount,
        "line_total": line_total,
    }


def _format_sale_date_display(raw_date):
    if not raw_date:
        return ""
    text = str(raw_date)
    try:
        return datetime.strptime(text[:19], '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')
    except ValueError:
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
        except ValueError:
            return text[:10]


def fetch_sale_items_detail_report(cursor, start_date, end_date, search_query=None, branch_code=None):
    """Lấy chi tiết hàng bán từ sale_items trong khoảng ngày."""
    sql = """
        SELECT
            s.id AS sale_id,
            s.date AS sale_date,
            COALESCE(s.sale_no, 'ĐH' || printf('%06d', s.id)) AS sale_no,
            COALESCE(s.invoice_number, '') AS invoice_number,
            COALESCE(s.invoice_pdf_url, '') AS invoice_pdf_url,
            COALESCE(p.product_code, m.item_code, '') AS product_code,
            COALESCE(si.product_name, p.name, m.name, '—') AS product_name,
            COALESCE(si.unit, p.unit, m.unit, 'Cái') AS unit,
            si.quantity,
            si.price AS unit_price,
            COALESCE(si.discount_pct, s.discount_pct, 0) AS discount_pct,
            COALESCE(si.tax_pct, s.tax_pct, 0) AS tax_pct,
            si.line_total
        FROM sale_items si
        INNER JOIN sale s ON s.id = si.sale_id
        LEFT JOIN products p ON p.id = si.product_id
        LEFT JOIN menu m ON m.id = si.menu_id
        WHERE s.status = 'completed'
          AND si.quantity > 0
          AND date(s.date) >= date(?)
          AND date(s.date) <= date(?)
    """
    params = [start_date, end_date]
    if search_query:
        sql += """
          AND (
                COALESCE(p.product_code, m.item_code, '') LIKE ?
             OR COALESCE(si.product_name, p.name, m.name, '') LIKE ?
             OR COALESCE(s.sale_no, 'ĐH' || printf('%06d', s.id), '') LIKE ?
             OR COALESCE(s.invoice_number, '') LIKE ?
          )
        """
        like = f"%{search_query}%"
        params.extend([like, like, like, like])

    if branch_code is not None:
        try:
            from Services.sme.branches import sale_branch_filter_sql
            bf, bp = sale_branch_filter_sql(cursor.connection, branch_code, alias='s')
            sql += bf
            params.extend(bp)
        except Exception:
            pass

    sql += " ORDER BY s.date DESC, s.id DESC, si.rowid"
    cursor.execute(sql, params)

    rows = []
    summary = {
        "total_quantity": 0.0,
        "total_discount": 0.0,
        "total_tax": 0.0,
        "total_payment": 0.0,
        "line_count": 0,
    }

    for r in cursor.fetchall():
        row = dict(r)
        amounts = _calc_sale_line_amounts(
            row["quantity"],
            row["unit_price"],
            row["discount_pct"],
            row["tax_pct"],
            row.get("line_total"),
        )
        qty = float(row["quantity"] or 0)
        item = {
            "sale_id": row["sale_id"],
            "sale_date": (row["sale_date"] or "")[:10],
            "sale_date_display": _format_sale_date_display(row["sale_date"]),
            "sale_no": row["sale_no"],
            "invoice_number": row["invoice_number"] or "",
            "invoice_pdf_url": row["invoice_pdf_url"] or "",
            "product_code": row["product_code"] or "",
            "product_name": row["product_name"],
            "unit": row["unit"],
            "quantity": qty,
            "unit_price": float(row["unit_price"] or 0),
            "discount_pct": float(row["discount_pct"] or 0),
            "tax_pct": float(row["tax_pct"] or 0),
            "discount_amount": amounts["discount_amount"],
            "tax_amount": amounts["tax_amount"],
            "line_total": amounts["line_total"],
        }
        rows.append(item)
        summary["total_quantity"] += qty
        summary["total_discount"] += amounts["discount_amount"]
        summary["total_tax"] += amounts["tax_amount"]
        summary["total_payment"] += amounts["line_total"]
        summary["line_count"] += 1

    return rows, summary


def _delete_sale_child_rows(cursor, sale_id: int) -> None:
    """Xóa bảng con trước khi DELETE sale (tránh FOREIGN KEY constraint failed)."""
    sid = int(sale_id)
    # Thứ tự: chứng từ / journal → kho → dòng hàng → audit
    for sql, params in (
        ("DELETE FROM phieu_xuat_kho WHERE sale_id = ?", (sid,)),
        ("DELETE FROM phieu_thu WHERE sale_id = ?", (sid,)),
        ("DELETE FROM cong_no WHERE sale_id = ?", (sid,)),
        (
            """
            DELETE FROM sme_vouchers
            WHERE source_id = ? AND COALESCE(source_type,'') IN (
                'sale', 'export_ar_settle', 'pos', 'pos_sale', ''
            )
            """,
            (sid,),
        ),
        (
            """
            DELETE FROM sme_journal_lines WHERE entry_id IN (
                SELECT id FROM sme_journal_entries
                WHERE document_id = ? AND UPPER(COALESCE(document_type,'')) IN (
                    'SALE', 'SALE_REVENUE', 'SALE_COGS', 'EXPORT_SHIP',
                    'EXPORT_REVENUE', 'EXPORT_COGS', 'EXPORT_TAX', 'PT', 'EXPORT_SETTLE'
                )
            )
            """,
            (sid,),
        ),
        (
            """
            DELETE FROM sme_journal_entries
            WHERE document_id = ? AND UPPER(COALESCE(document_type,'')) IN (
                'SALE', 'SALE_REVENUE', 'SALE_COGS', 'EXPORT_SHIP',
                'EXPORT_REVENUE', 'EXPORT_COGS', 'EXPORT_TAX', 'PT', 'EXPORT_SETTLE'
            )
            """,
            (sid,),
        ),
        (
            """
            DELETE FROM stock_moves
            WHERE ref_id = ? AND UPPER(COALESCE(type,'')) IN ('SALE', 'EXPORT_SHIP', 'EXPORT')
            """,
            (sid,),
        ),
        ("DELETE FROM inventory_transactions WHERE sale_id = ?", (sid,)),
        ("DELETE FROM sale_items WHERE sale_id = ?", (sid,)),
        ("DELETE FROM sale_audit_log WHERE sale_id = ?", (sid,)),
        ("DELETE FROM return_sales WHERE sale_id = ?", (sid,)),
    ):
        try:
            cursor.execute(sql, params)
        except sqlite3.OperationalError:
            # Bảng/cột chưa có trên DB cũ
            pass


def complete_pos_bank_payment(sale_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        begin_immediate(conn, label='complete_pos_bank_payment')
        sale = cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            return {"success": False, "error": "Không tìm thấy hóa đơn"}
        if sale['status'] == 'completed':
            conn.commit()
            return {"success": True, "already_completed": True}
        if sale['status'] != 'pending':
            return {"success": False, "error": "Đơn không ở trạng thái chờ thanh toán"}

        rows = cursor.execute("""
            SELECT si.product_id, si.quantity, si.price, si.UseSaleUnit, si.unit_ratio,
                   si.discount_pct, si.tax_pct,
                   COALESCE(p.product_type, 'goods') AS product_type,
                   COALESCE(
                       (SELECT SUM(sm.quantity) FROM stock_moves sm WHERE sm.product_id = si.product_id),
                       i.quantity,
                       0
                   ) AS stock, COALESCE(i.avg_cost, 0) AS avg_cost
            FROM sale_items si
            LEFT JOIN products p ON p.id = si.product_id
            LEFT JOIN inventory i ON si.product_id = i.product_id
            WHERE si.sale_id = ?
        """, (sale_id,)).fetchall()
        if not rows:
            return {"success": False, "error": "Đơn hàng không có sản phẩm"}

        sale_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ref_doc = sale['sale_no'] or f"ĐH{str(sale_id).zfill(6)}"
        customer_name = sale['customer_name'] or DEFAULT_RETAIL_BUYER_NAME
        company_name = sale['company_name'] or ''
        address = sale['address'] or ''
        tax_code = sale['tax_code'] or ''
        payment_method = sale['payment_method'] or '112'
        total_amount = float(sale['total_amount'] or 0)

        merged = defaultdict(lambda: {"deduct": 0.0, "details": [], "product_type": "goods"})
        for row in rows:
            pid = int(row['product_id'])
            qty_input = float(row['quantity'] or 0)
            price = float(row['price'] or 0)
            use_unit1 = bool(row['UseSaleUnit'])
            ratio = float(row['unit_ratio'] or 1) or 1.0
            deduct_qty = qty_input * ratio if use_unit1 else qty_input
            product_type = row['product_type'] or 'goods'
            stock = float(row['stock'] or 0)
            if requires_stock_check(product_type) and stock < deduct_qty:
                return {"success": False, "error": f"Không đủ hàng cho sản phẩm ID {pid}"}
            if requires_stock_check(product_type):
                merged[pid]["deduct"] += deduct_qty
            merged[pid]["product_type"] = product_type
            merged[pid]["details"].append({
                "qty_input": qty_input,
                "price": price,
                "use_unit1": use_unit1,
                "ratio": ratio,
                "avg_cost": float(row['avg_cost'] or 0),
                "discount_pct": float(row['discount_pct'] or 0),
                "tax_pct": float(row['tax_pct'] or 0),
            })

        px_items = []
        for pid, info in merged.items():
            if not requires_stock_check(info.get("product_type")):
                continue
            total_deduct = round(info["deduct"], 6)
            if total_deduct <= 0:
                continue
            avg_cost = info["details"][0]["avg_cost"]
            deduct_inventory_for_sale(
                cursor, pid, total_deduct, avg_cost, sale_id, sale_date, ref_doc,
            )
            p_info = cursor.execute("SELECT name, unit, unit1 FROM products WHERE id = ?", (pid,)).fetchone()
            for d in info["details"]:
                px_items.append({
                    "product_id": pid,
                    "product_name": p_info['name'],
                    "unit": p_info['unit1'] if d['use_unit1'] else p_info['unit'],
                    "quantity": d['qty_input'],
                    "price": d['price'],
                    "amount": d['qty_input'] * d['price']
                })

        if px_items:
            last_px = cursor.execute(
                "SELECT voucher_no FROM phieu_xuat_kho WHERE voucher_no LIKE 'PX%' ORDER BY id DESC LIMIT 1").fetchone()
            px_num = (int(last_px['voucher_no'][2:]) + 1) if last_px else 1
            px_voucher_no = f"PX{px_num:06d}"
            cursor.execute("""
                INSERT INTO phieu_xuat_kho (voucher_no, date, customer_name, items_json, total_amount, sale_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (px_voucher_no, sale_date, customer_name, json.dumps(px_items, ensure_ascii=False), total_amount, sale_id))

        if payment_method in ["111", "112"] and write_hkd_cash_vouchers(profile=get_current_tenant_profile()):
            last_pt = cursor.execute(
                "SELECT voucher_no FROM phieu_thu WHERE voucher_no LIKE 'PT%' ORDER BY id DESC LIMIT 1").fetchone()
            pt_num = (int(last_pt['voucher_no'][2:]) + 1) if last_pt else 1
            pt_vno = f"PT{pt_num:06d}"
            reason = f"Thu tiền bán hàng chuyển khoản - {ref_doc}"
            cursor.execute("""
                INSERT INTO phieu_thu
                (voucher_no, payer_name, address, tax_code, amount, debit_account, credit_account, reason, reference_document, sale_id, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pt_vno, customer_name, address, tax_code, total_amount, payment_method, '511', reason, ref_doc, sale_id, sale_date))
        elif payment_method == "131":
            cursor.execute("""
                INSERT INTO cong_no
                (customer_name, company_name, address, tax_code, debit_account, credit_account, date_of_debt, unpaid_amount, sale_id, sale_no)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (customer_name, company_name, address, tax_code, '131', '511', sale_date, total_amount, sale_id, ref_doc))

        cursor.execute("UPDATE sale SET status = 'completed', date = ? WHERE id = ?", (sale_date, sale_id))
        conn.commit()
        enqueue_accounting_job(
            conn,
            sale_id,
            accounting_regime=get_current_tenant_profile().get('accounting_regime'),
            features=get_current_tenant_profile().get('features'),
            created_by=session.get('user_name'),
        )
        return {"success": True, "sale_id": sale_id}
    except Exception as e:
        if conn:
            rollback_quietly(conn)
        logging.error("complete_pos_bank_payment: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def register_sale_routes(app):

    @app.route('/api/customers/search')
    @login_required
    def search_customers():
        q = request.args.get('q', '').strip()
        field = (request.args.get('field') or '').strip().lower()
        if not q:
            return jsonify([])

        digits = normalize_tax_code_digits(q)
        if field in ('phone', 'tax_code'):
            if len(digits) < 3:
                return jsonify([])
        elif len(q) < 2:
            return jsonify([])

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        tax_norm = (
            "REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(tax_code,''), '-', ''), ' ', ''), '.', ''), "
            "'/', '')"
        )
        phone_norm = (
            "REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(phone,''), '-', ''), ' ', ''), '.', ''), "
            "'/', '')"
        )

        if field == 'tax_code' and digits:
            cursor.execute(
                f"""
                SELECT * FROM customers
                WHERE COALESCE(tax_code, '') != ''
                  AND {tax_norm} LIKE ?
                ORDER BY
                  CASE WHEN {tax_norm} = ? THEN 0 ELSE 1 END,
                  LENGTH({tax_norm}) ASC
                LIMIT 10
                """,
                (f'{digits}%', digits),
            )
        elif field == 'phone' and digits:
            cursor.execute(
                f"""
                SELECT * FROM customers
                WHERE COALESCE(phone, '') != ''
                  AND {phone_norm} LIKE ?
                ORDER BY
                  CASE WHEN {phone_norm} = ? THEN 0
                       WHEN {phone_norm} LIKE ? THEN 1
                       ELSE 2 END,
                  LENGTH({phone_norm}) ASC
                LIMIT 10
                """,
                (f'{digits}%', digits, f'%{digits}'),
            )
        else:
            cursor.execute(
                f"""
                SELECT * FROM customers
                WHERE name LIKE ?
                   OR {tax_norm} LIKE ?
                   OR {phone_norm} LIKE ?
                ORDER BY
                  CASE WHEN {tax_norm} = ? OR {phone_norm} = ? THEN 0 ELSE 1 END
                LIMIT 10
                """,
                (f'%{q}%', f'%{digits}%' if digits else f'%{q}%',
                 f'%{digits}%' if digits else f'%{q}%', digits, digits),
            )

        rows = cursor.fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    #===API POS - TRANG BÁN HÀNG===#
    @app.route('/api/cart/checkout', methods=['POST'])
    @login_required
    def api_checkout():
        data = request.get_json()
        client_uuid = (data.get('client_uuid') or '').strip()
        sale_id = data.get('order_id') or data.get('sale_id')
        items = data.get('items', [])
        status = data.get('status', 'completed').strip().lower()

        if not items:
            return jsonify({"success": False, "error": "Giỏ hàng trống."}), 400
        if status not in ['draft', 'completed', 'pending']:
            return jsonify({"success": False, "error": "Trạng thái không hợp lệ."}), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            ensure_pos_offline_schema(conn, commit=False)
            if client_uuid and not sale_id:
                existing = find_sale_by_client_uuid(conn, client_uuid)
                if existing:
                    return jsonify({
                        "success": True,
                        "sale_id": existing['id'],
                        "status": existing.get('status') or status,
                        "deduped": True,
                    }), 200
        except Exception:
            pass
        finally:
            conn.close()

        # Tính total_amount chính xác từ từng dòng
        total_amount = 0.0
        for item in items:
            qty = float(item.get('quantity', 0))
            price = float(item.get('price', 0))
            discount_pct = float(item.get('discount_pct', 0))
            tax_pct = float(item.get('tax_pct', 0))

            line_sub = qty * price
            line_disc_amt = round(line_sub * (discount_pct / 100))
            line_taxable = line_sub - line_disc_amt
            line_tax_amt = round(line_taxable * (tax_pct / 100))
            total_amount += (line_taxable + line_tax_amt)

        # Các trường khác
        payment_method = data.get('payment_method', '111')
        customer_name = normalize_retail_buyer_name(data.get('customer_name'))
        company_name = data.get('company_name', '')
        email = data.get('email', '')
        address = data.get('address', '')
        tax_code = (data.get('tax_code') or '').strip()
        tax_err = tax_code_validation_message(tax_code)
        if tax_err:
            return jsonify({"success": False, "error": tax_err}), 400
        budget_unit_code = (data.get('budget_unit_code') or '').strip()
        passport_no = (data.get('passport_no') or '').strip()
        customer_phone = data.get('customer_phone', '')
        note = data.get('note', '')

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            ensure_pos_offline_schema(conn, commit=False)
            begin_immediate(conn, label='api_checkout')
            sale_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            old_status = 'draft'
            if sale_id:
                prev = cursor.execute(
                    "SELECT status FROM sale WHERE id = ?", (sale_id,),
                ).fetchone()
                if not prev:
                    raise Exception("Không tìm thấy đơn hàng.")
                old_status = str(prev['status'] or 'draft').lower()

            merged = defaultdict(lambda: {"deduct": 0.0, "details": [], "hkd_sector": "G1", "product_type": "goods"})

            from Services.user_branch import get_current_user_warehouse_codes
            _pos_wh_codes = get_current_user_warehouse_codes()

            for item in items:
                pid = int(item['product_id'])
                qty_input = float(item['quantity'])
                price = float(item['price'])
                use_unit1 = bool(item.get('UseSaleUnit', item.get('use_unit1', False)))
                discount_pct = float(item.get('discount_pct', 0))
                tax_pct = float(item.get('tax_pct', 0))

                row = fetch_product_for_checkout(cursor, pid, warehouse_codes=_pos_wh_codes)
                if not row:
                    raise Exception(f"Sản phẩm ID {pid} không tồn tại.")

                product_type = row['product_type'] or 'goods'
                ratio = float(row['unit_ratio'] or 1)
                deduct_qty = qty_input * ratio if use_unit1 else qty_input

                if status == 'completed' and requires_stock_check(product_type):
                    if float(row['stock']) < deduct_qty:
                        raise Exception(
                            f"Không đủ hàng cho sản phẩm ID {pid} "
                            f"(cần {deduct_qty:.2f}, còn {row['stock']:.2f})"
                        )

                if requires_stock_check(product_type):
                    merged[pid]["deduct"] += deduct_qty
                merged[pid]["product_type"] = product_type
                merged[pid]["hkd_sector"] = snapshot_item_hkd_sector(
                    product_type, row['hkd_sector_code'], 'pos',
                )
                merged[pid]["details"].append({
                    "qty_input": qty_input,
                    "price": price,
                    "use_unit1": use_unit1,
                    "ratio": ratio,
                    "avg_cost": float(row['avg_cost']),
                    "discount_pct": discount_pct,
                    "tax_pct": tax_pct
                })

            # Tạo hoặc cập nhật bảng sale
            ref_doc = None
            if not sale_id:
                ensure_customer(
                    cursor, customer_name, company_name, customer_phone, address,
                    tax_code, email, budget_unit_code, passport_no,
                )

                if table_has_column(cursor, 'sale', 'business_line'):
                    cursor.execute("""
                        INSERT INTO sale 
                        (date, total_amount, payment_method, customer_name, company_name, tax_code, 
                         customer_phone, address, note, status, email, business_line,
                         budget_unit_code, passport_no, client_uuid)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pos', ?, ?, ?)
                    """, (sale_date, total_amount, payment_method, customer_name, company_name,
                          tax_code, customer_phone, address, note, status, email,
                          budget_unit_code or None, passport_no or None,
                          client_uuid or None))
                else:
                    ins_cols = [
                        'date', 'total_amount', 'payment_method', 'customer_name', 'company_name', 'tax_code',
                        'customer_phone', 'address', 'note', 'status', 'email',
                        'budget_unit_code', 'passport_no',
                    ]
                    ins_vals = [
                        sale_date, total_amount, payment_method, customer_name, company_name,
                        tax_code, customer_phone, address, note, status, email,
                        budget_unit_code or None, passport_no or None,
                    ]
                    if table_has_column(cursor, 'sale', 'client_uuid'):
                        ins_cols.append('client_uuid')
                        ins_vals.append(client_uuid or None)
                    placeholders = ', '.join(['?'] * len(ins_cols))
                    cursor.execute(
                        f"INSERT INTO sale ({', '.join(ins_cols)}) VALUES ({placeholders})",
                        ins_vals,
                    )

                sale_id = cursor.lastrowid
                ref_doc = f"ĐH{str(sale_id).zfill(6)}"
                cursor.execute("UPDATE sale SET sale_no = ? WHERE id = ?", (ref_doc, sale_id))
            else:
                ref_doc = f"ĐH{str(sale_id).zfill(6)}"
                update_sql = """
                    UPDATE sale SET
                        date = ?, total_amount = ?, payment_method = ?, customer_name = ?, 
                        company_name = ?, tax_code = ?, customer_phone = ?, address = ?, 
                        note = ?, status = ?, email = ?, sale_no = ?,
                        budget_unit_code = ?, passport_no = ?
                """
                update_params = [
                    sale_date, total_amount, payment_method, customer_name, company_name,
                    tax_code, customer_phone, address, note, status, email, ref_doc,
                    budget_unit_code or None, passport_no or None,
                ]
                update_sql, update_params = _apply_sale_business_line(
                    cursor, update_sql, update_params, ref_doc,
                )
                update_sql += " WHERE id = ?"
                update_params.append(sale_id)
                cursor.execute(update_sql, update_params)

            # Hoàn kho đơn cũ nếu đã completed (trước khi xóa sale_items)
            if sale_id and old_status == 'completed':
                cursor.execute("DELETE FROM phieu_xuat_kho WHERE sale_id = ?", (sale_id,))
                cursor.execute("DELETE FROM phieu_thu WHERE sale_id = ?", (sale_id,))
                cursor.execute("DELETE FROM cong_no WHERE sale_id = ?", (sale_id,))
                revert_sale_stock(cursor, sale_id)

            # Xóa và ghi lại sale_items
            cursor.execute("DELETE FROM sale_items WHERE sale_id = ?", (sale_id,))
            for pid, info in merged.items():
                for d in info["details"]:
                    insert_pos_sale_item(
                        cursor, sale_id, pid, d, info.get("hkd_sector"),
                    )

            # Xử lý khi status = 'completed'
            if status == 'completed':
                px_items = []
                for pid, info in merged.items():
                    if not requires_stock_check(info.get("product_type")):
                        continue
                    total_deduct = round(info["deduct"], 6)
                    if total_deduct <= 0:
                        continue
                    avg_cost = info["details"][0]["avg_cost"]
                    deduct_inventory_for_sale(
                        cursor, pid, total_deduct, avg_cost, sale_id, sale_date, ref_doc,
                    )
                    p_info = cursor.execute("SELECT name, unit, unit1 FROM products WHERE id = ?", (pid,)).fetchone()
                    for d in info["details"]:
                        px_items.append({
                            "product_id": pid,
                            "product_name": p_info['name'],
                            "unit": p_info['unit1'] if d['use_unit1'] else p_info['unit'],
                            "quantity": d['qty_input'],
                            "price": d['price'],
                            "amount": d['qty_input'] * d['price']
                        })

                # Tạo phiếu xuất kho (chỉ hàng có trừ kho)
                if px_items:
                    last_px = cursor.execute("SELECT voucher_no FROM phieu_xuat_kho WHERE voucher_no LIKE 'PX%' ORDER BY id DESC LIMIT 1").fetchone()
                    px_num = (int(last_px['voucher_no'][2:]) + 1) if last_px else 1
                    px_voucher_no = f"PX{px_num:06d}"

                    cursor.execute("""
                        INSERT INTO phieu_xuat_kho (voucher_no, date, customer_name, items_json, total_amount, sale_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (px_voucher_no, sale_date, customer_name, json.dumps(px_items, ensure_ascii=False), total_amount, sale_id))

                # Tạo chứng từ tài chính (chỉ HKD — SME dùng sme_journal / sme_vouchers)
                if payment_method in ["111", "112"] and write_hkd_cash_vouchers(profile=get_current_tenant_profile()):
                    last_pt = cursor.execute("SELECT voucher_no FROM phieu_thu WHERE voucher_no LIKE 'PT%' ORDER BY id DESC LIMIT 1").fetchone()
                    pt_num = (int(last_pt['voucher_no'][2:]) + 1) if last_pt else 1
                    pt_vno = f"PT{pt_num:06d}"
                    reason = f"Thu tiền bán hàng {'tiền mặt' if payment_method=='111' else 'chuyển khoản'} - {ref_doc}"

                    cursor.execute("""
                        INSERT INTO phieu_thu
                        (voucher_no, payer_name, address, tax_code, amount, debit_account, credit_account, reason, reference_document, sale_id, date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (pt_vno, customer_name, address, tax_code, total_amount, payment_method, '511', reason, ref_doc, sale_id, sale_date))

                elif payment_method == "131":
                    cursor.execute("""
                        INSERT INTO cong_no
                        (customer_name, company_name, address, tax_code, debit_account, credit_account, date_of_debt, unpaid_amount, sale_id, sale_no)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (customer_name, company_name, address, tax_code, '131', '511', sale_date, total_amount, sale_id, ref_doc))

            conn.commit()
            if status == 'completed' or old_status == 'completed':
                enqueue_accounting_job(
                    conn,
                    sale_id,
                    accounting_regime=get_current_tenant_profile().get('accounting_regime'),
                    features=get_current_tenant_profile().get('features'),
                    created_by=session.get('user_name'),
                    replace_existing=old_status == 'completed',
                )
            return jsonify({"success": True, "sale_id": sale_id, "status": status}), 200

        except Exception as e:
            if conn:
                rollback_quietly(conn)
            logging.error(f"Lỗi api_checkout: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn: conn.close()

    @app.route('/api/sale/delete_pending/<int:sale_id>', methods=['POST'])
    @login_required
    def delete_pending_sale(sale_id):
        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            try:
                from Services.sme.branches import active_report_branch_filter, assert_sale_in_branch
                if active_report_branch_filter() is not None:
                    assert_sale_in_branch(conn, sale_id)
            except ValueError as ve:
                return jsonify({'success': False, 'error': str(ve)}), 403

            begin_immediate(conn, label='delete_pending_sale')

            # Kiểm tra đơn hàng có tồn tại và đang pending không
            cursor.execute("SELECT id, status FROM sale WHERE id = ?", (sale_id,))
            sale = cursor.fetchone()

            if not sale:
                rollback_quietly(conn)
                return jsonify({'success': False, 'error': f'Không tìm thấy đơn hàng #{sale_id}'}), 404

            if sale['status'] != 'pending':
                rollback_quietly(conn)
                return jsonify({'success': False, 'error': 'Chỉ được xóa đơn đang ở trạng thái pending'}), 400

            # Xóa bảng con trước — tránh FOREIGN KEY constraint failed
            _delete_sale_child_rows(cursor, sale_id)
            cursor.execute("DELETE FROM sale WHERE id = ?", (sale_id,))

            try:
                cursor.execute("""
                    UPDATE sqlite_sequence
                    SET seq = (SELECT IFNULL(MAX(id),0) FROM sale)
                    WHERE name = 'sale'
                """)
            except sqlite3.OperationalError:
                pass

            conn.commit()

            return jsonify({
                'success': True,
                'message': f'Đã xóa đơn pending #{sale_id}'
            })

        except sqlite3.IntegrityError as e:
            if conn:
                rollback_quietly(conn)
            logging.error("delete_pending IntegrityError: %s", e, exc_info=True)
            return jsonify({
                'success': False,
                'error': f'Không xóa được do ràng buộc dữ liệu: {e}',
            }), 500

        except sqlite3.OperationalError as e:
            if conn:
                rollback_quietly(conn)
            error_str = str(e).lower()
            if "no such table" in error_str:
                msg = f"LỖI: Bảng 'sale' không tồn tại! {e}"
            elif "no such column" in error_str:
                msg = f"LỖI: Bảng sale không có cột 'status'! {e}"
            elif _is_locked_error(e):
                msg = 'Database đang bận (locked). Thử lại sau vài giây.'
            else:
                msg = str(e)

            logging.error("delete_pending OperationalError: %s", msg)
            return jsonify({'success': False, 'error': msg}), 500

        except Exception as e:
            if conn:
                rollback_quietly(conn)
            logging.error("delete_pending: %s", e, exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

        finally:
            if conn:
                conn.close()

    #==== API cập nhật chi tiết sản phẩm khi sửa đơn hàng bán====#
    @app.route('/api/sale/update_item/<int:sale_id>', methods=['PUT'])
    @login_required
    def api_update_sale_item(sale_id):
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Dữ liệu không hợp lệ."}), 400
        client_uuid = (data.get('client_uuid') or '').strip()
        try:
            from Services.sme.branches import active_report_branch_filter, assert_sale_in_branch
            if active_report_branch_filter() is not None:
                _conn = get_db_connection()
                try:
                    assert_sale_in_branch(_conn, sale_id)
                finally:
                    _conn.close()
        except ValueError as ve:
            return jsonify({"success": False, "error": str(ve)}), 403

        conn_pre = get_db_connection()
        try:
            ensure_pos_offline_schema(conn_pre, commit=False)
            if client_uuid:
                existing = find_sale_by_client_uuid(conn_pre, client_uuid)
                if existing and int(existing['id']) != int(sale_id):
                    return jsonify({
                        "success": True,
                        "sale_id": existing['id'],
                        "status": existing.get('status') or data.get('status', 'draft'),
                        "deduped": True,
                    }), 200
        except Exception:
            pass
        finally:
            conn_pre.close()

        items = data.get('items', [])
        if not items:
            return jsonify({"success": False, "error": "Giỏ hàng trống."}), 400
        customer_name = normalize_retail_buyer_name(data.get('customer_name'))
        company_name = data.get('company_name', '')
        email = data.get('email', '')
        address = data.get('address', '')
        tax_code = (data.get('tax_code') or '').strip()
        tax_err = tax_code_validation_message(tax_code)
        if tax_err:
            return jsonify({"success": False, "error": tax_err}), 400
        budget_unit_code = (data.get('budget_unit_code') or '').strip()
        passport_no = (data.get('passport_no') or '').strip()
        customer_phone = data.get('customer_phone', '')
        note = data.get('note', '')
        payment_method = data.get('payment_method', '111')
        new_status = data.get('status', 'completed').strip().lower()
        # Tính total_amount từ từng dòng
        total_amount = 0.0
        for item in items:
            qty = float(item.get('quantity', 0))
            price = float(item.get('price', 0))
            discount_pct = float(item.get('discount_pct', 0))
            tax_pct = float(item.get('tax_pct', 0))
            line_sub = qty * price
            line_disc_amt = round(line_sub * (discount_pct / 100))
            line_taxable = line_sub - line_disc_amt
            line_tax_amt = round(line_taxable * (tax_pct / 100))
            total_amount += (line_taxable + line_tax_amt)
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            begin_immediate(conn, label='api_update_sale_item')
            sale_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ref_doc = f"ĐH{str(sale_id).zfill(6)}"
            # Kiểm tra đơn hàng
            cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,))
            row = cursor.fetchone()
            if not row:
                raise Exception("Không tìm thấy đơn hàng.")
            sale_old = dict(row)
            inv_status = str(sale_old.get('invoice_status') or '').lower()
            inv_no = str(sale_old.get('invoice_number') or '').strip()
            is_draft_inv = inv_status == 'draft' or (inv_no in ('0', '00000000') and sale_old.get('invoice_id'))
            if sale_old.get('invoice_number') and not is_draft_inv:
                raise Exception("Đơn hàng đã xuất hóa đơn điện tử, không được phép chỉnh sửa.")
            old_status = str(sale_old.get('status', 'draft')).lower()
            cursor.execute(
                "SELECT DISTINCT product_id FROM sale_items WHERE sale_id = ?",
                (sale_id,),
            )
            affected_product_ids = [r[0] for r in cursor.fetchall()]
            if old_status != 'draft':
                cursor.execute("DELETE FROM phieu_xuat_kho WHERE sale_id = ?", (sale_id,))
                cursor.execute("DELETE FROM phieu_thu WHERE sale_id = ?", (sale_id,))
                cursor.execute("DELETE FROM cong_no WHERE sale_id = ?", (sale_id,))
                revert_sale_stock(cursor, sale_id, affected_product_ids)
            cursor.execute("DELETE FROM sale_items WHERE sale_id = ?", (sale_id,))
            # Gộp items và kiểm tra tồn kho
            merged = defaultdict(lambda: {"deduct": 0.0, "details": [], "hkd_sector": "G1", "product_type": "goods"})
            from Services.user_branch import get_current_user_warehouse_codes
            _pos_wh_codes2 = get_current_user_warehouse_codes()
            for item in items:
                pid = int(item['product_id'])
                qty_input = float(item['quantity'])
                price = float(item['price'])
                use_unit1 = bool(item.get('UseSaleUnit', item.get('use_unit1', False)))
                discount_pct = float(item.get('discount_pct', 0))
                tax_pct = float(item.get('tax_pct', 0))
                p_row = fetch_product_for_checkout(cursor, pid, warehouse_codes=_pos_wh_codes2)
                if not p_row:
                    raise Exception(f"Sản phẩm ID {pid} không tồn tại.")
                p = dict(p_row)
                product_type = p['product_type'] or 'goods'
                ratio = float(p['unit_ratio'] or 1)
                deduct_qty = qty_input * ratio if use_unit1 else qty_input
                if new_status == 'completed' and requires_stock_check(product_type):
                    if float(p['stock']) < deduct_qty:
                        raise Exception(f"Không đủ hàng cho sản phẩm {p['name']}")
                if requires_stock_check(product_type):
                    merged[pid]["deduct"] += deduct_qty
                merged[pid]["product_type"] = product_type
                merged[pid]["hkd_sector"] = snapshot_item_hkd_sector(
                    product_type, p.get('hkd_sector_code'), 'pos',
                )
                merged[pid]["details"].append({
                    "qty_input": qty_input,
                    "price": price,
                    "use_unit1": use_unit1,
                    "ratio": ratio,
                    "avg_cost": float(p['avg_cost']),
                    "discount_pct": discount_pct,
                    "tax_pct": tax_pct
                })
            # Cập nhật bảng sale với total_amount mới
            update_sql = """
                UPDATE sale SET
                    date = ?, total_amount = ?, payment_method = ?, customer_name = ?,
                    company_name = ?, tax_code = ?, customer_phone = ?, email = ?,
                    address = ?, note = ?, status = ?, sale_no = ?,
                    budget_unit_code = ?, passport_no = ?
            """
            update_params = [
                sale_date, total_amount, payment_method, customer_name, company_name,
                tax_code, customer_phone, email, address, note, new_status, ref_doc,
                budget_unit_code or None, passport_no or None,
            ]
            update_sql, update_params = _apply_sale_business_line(
                cursor, update_sql, update_params, ref_doc,
            )
            update_sql += " WHERE id = ?"
            update_params.append(sale_id)
            cursor.execute(update_sql, update_params)
            if client_uuid and table_has_column(cursor, 'sale', 'client_uuid'):
                cursor.execute(
                    "UPDATE sale SET client_uuid = COALESCE(client_uuid, ?) WHERE id = ?",
                    (client_uuid, sale_id),
                )
            # Lưu sale_items mới
            for pid, info in merged.items():
                for d in info["details"]:
                    insert_pos_sale_item(
                        cursor, sale_id, pid, d, info.get("hkd_sector"),
                    )
            # Xử lý khi new_status = 'completed'
            if new_status == 'completed':
                px_items = []
                for pid, info in merged.items():
                    if not requires_stock_check(info.get("product_type")):
                        continue
                    total_deduct = round(info["deduct"], 6)
                    if total_deduct <= 0:
                        continue
                    avg_cost = info["details"][0]["avg_cost"]
                    deduct_inventory_for_sale(
                        cursor, pid, total_deduct, avg_cost, sale_id, sale_date, ref_doc,
                    )
                    p_info = cursor.execute("SELECT name, unit, unit1 FROM products WHERE id = ?", (pid,)).fetchone()
                    for d in info["details"]:
                        px_items.append({
                            "product_id": pid,
                            "product_name": p_info['name'],
                            "unit": p_info['unit1'] if d['use_unit1'] else p_info['unit'],
                            "quantity": d['qty_input'],
                            "price": d['price'],
                            "amount": d['qty_input'] * d['price']
                        })
                # Phiếu xuất kho
                if px_items:
                    last_px = cursor.execute("SELECT voucher_no FROM phieu_xuat_kho WHERE voucher_no LIKE 'PX%' ORDER BY id DESC LIMIT 1").fetchone()
                    px_num = (int(last_px['voucher_no'][2:]) + 1) if last_px else 1
                    px_voucher_no = f"PX{px_num:06d}"
                    cursor.execute("""
                        INSERT INTO phieu_xuat_kho (voucher_no, date, customer_name, items_json, total_amount, sale_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (px_voucher_no, sale_date, customer_name, json.dumps(px_items, ensure_ascii=False), total_amount, sale_id))
                # Chứng từ tài chính (chỉ HKD — SME dùng sme_journal / sme_vouchers)
                if payment_method in ["111", "112"] and write_hkd_cash_vouchers(profile=get_current_tenant_profile()):
                    last_pt = cursor.execute("SELECT voucher_no FROM phieu_thu WHERE voucher_no LIKE 'PT%' ORDER BY id DESC LIMIT 1").fetchone()
                    pt_num = (int(last_pt['voucher_no'][2:]) + 1) if last_pt else 1
                    pt_vno = f"PT{pt_num:06d}"
                    reason = f"Thu tiền bán hàng {'tiền mặt' if payment_method == '111' else 'chuyển khoản'} - {ref_doc}"
                    cursor.execute("""
                        INSERT INTO phieu_thu
                        (voucher_no, payer_name, address, tax_code, amount, debit_account, credit_account, reason, reference_document, sale_id, date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (pt_vno, customer_name, address, tax_code, total_amount, payment_method, '511', reason, ref_doc, sale_id, sale_date))
                elif payment_method == "131":
                    cursor.execute("""
                        INSERT INTO cong_no
                        (customer_name, company_name, address, tax_code, debit_account, credit_account,
                         date_of_debt, unpaid_amount, sale_id, sale_no)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (customer_name, company_name, address, tax_code, '131', '511', sale_date, total_amount, sale_id, ref_doc))
            conn.commit()
            enqueue_accounting_job(
                conn,
                sale_id,
                accounting_regime=get_current_tenant_profile().get('accounting_regime'),
                features=get_current_tenant_profile().get('features'),
                created_by=session.get('user_name'),
                replace_existing=old_status != 'draft',
            )
            return jsonify({"success": True, "sale_id": sale_id, "status": new_status}), 200
        except Exception as e:
            conn.rollback()
            import traceback
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

    #===HÀM GỬI EMAIL CHO KHÁCH SAU KHI XUẤT HÓA ĐƠN===#
    import smtplib
    from email.message import EmailMessage

    #SMTP_SERVER = 'smtp.gmail.com'
    #SMTP_PORT = 587
    #SENDER_EMAIL = 'tinkien@gmail.com'
    #APP_PASSWORD = 'cqxj mdfm khuk cuqs'

    # Trang SỬA ĐƠN HÀNG
    @app.route('/sale/edit/<int:order_id>')
    def edit_order(order_id):
        return render_template('edit_order.html')

    @app.route('/qr_payment/<int:sale_id>')
    # @login_required # Mở ra nếu bạn đã cài đặt đăng nhập
    def qr_payment(sale_id):
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
    
        # 1. Lấy thông tin tài khoản và số tiền
        biz = c.execute("SELECT bank_account, bank_code, account_holder FROM business_info LIMIT 1").fetchone()
        sale = c.execute("SELECT total_amount FROM sale WHERE id = ?", (sale_id,)).fetchone()
        conn.close()

        # Kiểm tra dữ liệu đầu vào
        if not sale:
            return "Đơn hàng không tồn tại", 404
        if not biz or not biz['bank_account'] or not biz['bank_code']:
            return "Chưa cấu hình STK hoặc Mã BIN ngân hàng (Sacombank: 970403)", 404

        # 2. Cấu hình tham số VietQR
        bank_bin = biz['bank_code']
        account_no = biz['bank_account']
        amount = int(sale['total_amount'])
        description = f"DH{str(sale_id).zfill(6)}"
    
        # URL API VietQR
        qr_url = f"https://img.vietqr.io/image/{bank_bin}-{account_no}-compact2.jpg"
        params = {
            "amount": amount,
            "addInfo": description,
            "accountName": biz['account_holder'] if biz['account_holder'] else ""
        }

        try:
            # 3. Gọi API bằng thư viện requests
            response = requests.get(qr_url, params=params, timeout=10)
        
            if response.status_code == 200:
                # Trả về nội dung ảnh trực tiếp
                return Response(
                    response.content, 
                    mimetype='image/jpeg',
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
                )
            else:
                return f"VietQR API lỗi (Mã {response.status_code})", 500
            
        except Exception as e:
            # Nếu server không gọi được API (do chặn mạng), dùng cách Redirect dự phòng
            from urllib.parse import quote
            redirect_url = f"{qr_url}?amount={amount}&addInfo={quote(description)}"
            return redirect(redirect_url)

    @app.route('/sale/print/<int:sale_id>')
    @login_required
    def print_sale_bill(sale_id):
        conn = get_db_connection()
        c = conn.cursor()

        try:
            from Services.sme.branches import active_report_branch_filter, assert_sale_in_branch
            if active_report_branch_filter() is not None:
                assert_sale_in_branch(conn, sale_id)
        except ValueError:
            abort(403)

        # 1. ĐỌC GIÁ TRỊ CẤU HÌNH TRỰC TIẾP TỪ BẢNG SETTINGS
        c.execute("SELECT value FROM settings WHERE key = 'auto_print' LIMIT 1")
        setting_row = c.fetchone()
    
        # Ép kiểu dữ liệu về số nguyên, nếu trống thì mặc định là 0 (In thủ công)
        auto_print_setting = int(setting_row['value']) if setting_row and setting_row['value'] is not None else 0
        autoprint_flag = True if auto_print_setting == 1 else False
        if request.args.get('autoprint') == '1':
            autoprint_flag = True

        # 2. LẤY THÔNG TIN ĐƠN HÀNG (Giữ nguyên tax_code là Mã Số Thuế)
        c.execute("""
            SELECT 
                s.id,
                s.date,
                COALESCE(s.customer_name, ?) AS customer_name,
                COALESCE(s.customer_phone, '') AS customer_phone,
                COALESCE(s.address, '') AS address,
                COALESCE(s.tax_code, '') AS tax_code,
                COALESCE(s.total_amount, 0) AS total_amount,
                COALESCE(s.discount_amount, 0) AS discount_amount,
                s.business_line
            FROM sale s 
            WHERE s.id = ?
        """, (DEFAULT_RETAIL_BUYER_NAME, sale_id))
    
        sale_row = c.fetchone()
        if not sale_row:
            conn.close()
            flash('Không tìm thấy đơn hàng!', 'danger')
            return redirect(url_for('order'))

        sale = dict(sale_row)
        sale['sale_no'] = f"ĐH{str(sale['id']).zfill(6)}"

        # FORMAT NGÀY GIỜ ĐƠN HÀNG
        try:
            sale['date_formatted'] = datetime.strptime(sale['date'][:19], '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')
        except:
            sale['date_formatted'] = sale['date']

        # 3. LẤY CHI TIẾT ĐƠN HÀNG (Hàng hóa & Dịch vụ Rental)
        c.execute("""
            SELECT 
                COALESCE(p.name, si.product_name) AS display_name,
                si.quantity,
                si.price AS sold_price,
                COALESCE(si.line_total, (si.quantity * si.price)) AS line_total,
                CASE 
                    WHEN si.unit IS NOT NULL AND si.unit != '' THEN si.unit
                    WHEN COALESCE(si.UseSaleUnit, 0) = 1 AND p.unit1 IS NOT NULL THEN p.unit1
                    ELSE COALESCE(p.unit, 'Cái')
                END AS display_unit
            FROM sale_items si
            LEFT JOIN products p ON si.product_id = p.id
            WHERE si.sale_id = ?
            ORDER BY si.rowid
        """, (sale_id,))

        items = [dict(row) for row in c.fetchall()]

        # 4. TÍNH TOÁN CÁC THÔNG SỐ TÀI CHÍNH
        subtotal = sum(item['line_total'] for item in items)
        tax_amount = sale['total_amount'] - (subtotal - sale['discount_amount'])

        sale.update({
            'subtotal': subtotal,
            'tax_amount': max(tax_amount, 0),
            'final_amount': sale['total_amount']
        })

        conn.close()
    
        # Render ra giao diện và truyền biến cấu hình xuống cho template quyết định cách in
        return render_template('print_bill.html', sale=sale, items=items, autoprint=autoprint_flag, timestamp=int(datetime.now().timestamp()))

    @app.route('/sale/view/<int:sale_id>')
    @login_required
    def view_sale(sale_id):
        conn = get_db_connection()
        c = conn.cursor()
        try:
            try:
                from Services.sme.branches import active_report_branch_filter, assert_sale_in_branch
                if active_report_branch_filter() is not None:
                    assert_sale_in_branch(conn, sale_id)
            except ValueError:
                abort(403)
            # 1. LẤY THÔNG TIN ĐƠN HÀNG
            c.execute("SELECT * FROM sale WHERE id = ?", (sale_id,))
            sale_row = c.fetchone()
        
            if not sale_row:
                abort(404)
            
            sale = dict(sale_row)
            # Lấy phần trăm chiết khấu chung (giữ nguyên logic của bạn)
            discount_pct = float(sale.get('discount_pct') or 0)

            # 2. LẤY CHI TIẾT HÀNG HÓA & DỊCH VỤ
            # Sử dụng COALESCE để nếu p.name NULL (hàng nhập tay/dịch vụ) thì lấy si.product_name
            c.execute("""
                SELECT 
                    si.quantity, 
                    si.price AS sold_price,
                    si.UseSaleUnit,
                    COALESCE(p.name, si.product_name) AS product_name,
                    p.unit AS base_unit,
                    p.unit1 AS wholesale_unit,
                    si.unit AS manual_unit
                FROM sale_items si
                LEFT JOIN products p ON si.product_id = p.id
                WHERE si.sale_id = ?
                ORDER BY si.rowid
            """, (sale_id,))
        
            items = []
            for row in c.fetchall():
                item = dict(row)
            
                # Logic chọn đơn vị hiển thị (Cải tiến để lấy được unit từ rental_service)
                if item.get('manual_unit'):
                    item['display_unit'] = item['manual_unit']
                elif item.get('UseSaleUnit') == 1 and item.get('wholesale_unit'):
                    item['display_unit'] = item['wholesale_unit']
                else:
                    item['display_unit'] = item.get('base_unit') or 'Cái'
            
                # TÍNH TOÁN TÀI CHÍNH TỪNG DÒNG (Giữ đúng logic chiết khấu % của bạn)
                qty = float(item.get('quantity') or 0)
                price = float(item.get('sold_price') or 0)
            
                line_subtotal = qty * price
                line_discount = line_subtotal * (discount_pct / 100)
            
                item['line_subtotal'] = line_subtotal
                item['line_discount'] = line_discount
                item['line_total'] = line_subtotal - line_discount
            
                items.append(item)

            # 3. ĐỊNH DẠNG NGÀY THÁNG
            def safe_date(d):
                if not d: return "—"
                try: 
                    # Thử định dạng đầy đủ nếu có giờ, nếu không thì lấy 10 ký tự đầu
                    return datetime.strptime(d[:19], '%Y-%m-%d %H:%M:%S').strftime('%d/%m/%Y %H:%M')
                except: 
                    try:
                        return datetime.strptime(d[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
                    except:
                        return str(d)

            # 4. GÁN DỮ LIỆU TRẢ VỀ FRONTEND
            sale['formatted_date'] = safe_date(sale.get('date'))
        
            # QUAN TRỌNG: Giữ tên biến 'order_details' để Frontend template (sale_view.html) không bị lỗi
            sale['order_details'] = items

            # Tính thêm các biến tổng để template dễ hiển thị (nếu cần)
            sale['subtotal'] = sum(i['line_subtotal'] for i in items)
        
            return render_template('sale_view.html', sale=sale)

        finally:
            conn.close()

    # LẤY CHI TIẾT ĐƠN HÀNG + SỐ LƯỢNG ĐÃ HOÀN (hợp nhất api_get_sale + api_sale_detail)
    @app.route('/api/sale/<int:sale_id>', methods=['GET'])
    @login_required
    def api_get_sale(sale_id):
        conn = get_db_connection()
        c = conn.cursor()
        try:
            try:
                from Services.sme.branches import active_report_branch_filter, assert_sale_in_branch
                if active_report_branch_filter() is not None:
                    assert_sale_in_branch(conn, sale_id)
            except ValueError as ve:
                return jsonify({"error": str(ve)}), 403
            c.execute("""
                SELECT s.*
                FROM sale s
                WHERE s.id = ?
            """, (sale_id,))
            sale_row = c.fetchone()
            if not sale_row:
                return jsonify({"error": "Không tìm thấy đơn hàng"}), 404

            sale = dict(sale_row)
            customer_name = (sale.get('customer_name') or '').strip()
            if not customer_name:
                customer_name = DEFAULT_RETAIL_BUYER_NAME

            customer_phone = (sale.get('customer_phone') or sale.get('phone') or '').strip()
            total_amount = float(sale.get('total_amount') or 0)
            discount_amount = float(sale.get('discount_amount') or 0)
            tax_pct = float(sale.get('tax_pct') or 0)
            sale_no = (sale.get('sale_no') or '').strip() or f"ĐH{str(sale_id).zfill(6)}"
            sale_date = sale.get('date')
            created_at = sale.get('created_at') or sale_date

            c.execute("""
                SELECT
                    si.product_id,
                    si.product_name AS si_name,
                    si.quantity,
                    si.price AS sold_price,
                    si.discount_pct,
                    si.cost_price,
                    COALESCE(si.UseSaleUnit, 0) AS UseSaleUnit,
                    si.unit AS si_unit,
                    p.name AS product_name,
                    p.product_code,
                    p.barcode,
                    p.barcode1,
                    p.unit AS p_unit,
                    p.unit1 AS p_unit1,
                    COALESCE(p.unit_ratio, 1) AS unit_ratio
                FROM sale_items si
                LEFT JOIN products p ON p.id = si.product_id
                WHERE si.sale_id = ?
                ORDER BY si.rowid
            """, (sale_id,))

            items = []
            for row in c.fetchall():
                row = dict(row)
                use_sale_unit = int(row.get('UseSaleUnit') or 0)
                product_id = row.get('product_id')
                name = (row.get('si_name') or row.get('product_name') or '').strip()
                sold_price = float(row.get('sold_price') or 0)
                quantity = float(row.get('quantity') or 0)
                discount_pct = float(row.get('discount_pct') or 0)
                unit1 = row.get('si_unit1') or row.get('p_unit1') or 'Lốc/Thùng'
                unit = (
                    unit1 if use_sale_unit == 1
                    else (row.get('si_unit') or row.get('p_unit') or 'Cái')
                )

                c.execute("""
                    SELECT COALESCE(SUM(quantity), 0)
                    FROM return_sales
                    WHERE sale_id = ?
                      AND product_id = ?
                      AND COALESCE(UseSaleUnit, 0) = ?
                """, (sale_id, product_id, use_sale_unit))
                returned_qty = float(c.fetchone()[0])
                remaining_qty = quantity

                product_code = row.get('product_code') or (str(product_id) if product_id is not None else '')
                items.append({
                    "product_id": product_id,
                    "name": name,
                    "product_name": name,
                    "product_code": product_code,
                    "quantity": quantity,
                    "sold_price": sold_price,
                    "price": sold_price,
                    "discount_pct": discount_pct,
                    "discount": discount_pct,
                    "tax_pct": tax_pct,
                    "cost_price": float(row.get('cost_price') or 0),
                    "UseSaleUnit": use_sale_unit,
                    "unit": unit,
                    "unit1": unit1,
                    "unit_ratio": float(row.get('unit_ratio') or 1),
                    "barcode": row.get('barcode') or '',
                    "barcode1": row.get('barcode1') or '',
                    "returned_qty": returned_qty,
                    "remaining_qty": remaining_qty,
                    "line_total": quantity * sold_price,
                })

            payload = {
                "id": sale['id'],
                "sale_no": sale_no,
                "date": sale_date,
                "created_at": created_at,
                "status": sale.get('status') or '',
                "customer_name": customer_name,
                "company_name": sale.get('company_name') or '',
                "tax_code": sale.get('tax_code') or '',
                "budget_unit_code": sale.get('budget_unit_code') or '',
                "passport_no": sale.get('passport_no') or '',
                "address": sale.get('address') or '',
                "customer_phone": customer_phone,
                "phone": customer_phone,
                "email": sale.get('email') or '',
                "tax_pct": tax_pct,
                "total_amount": total_amount,
                "discount_amount": discount_amount,
                "note": sale.get('note') or '',
                "items": items,
            }

            # Root flat + nested `data` để tương thích mọi frontend (json.data || json)
            return jsonify({"success": True, **payload, "data": payload}), 200

        except Exception as e:
            print("LỖI API /api/sale/<id>:", e)
            traceback.print_exc()
            return jsonify({"error": "Lỗi server"}), 500
        finally:
            conn.close()

    #=== Endpoint export Excel cho trang Quản Lý Đơn Hàng===#
    @app.route('/api/sale/export', methods=['GET'])  # Giữ nguyên endpoint frontend đang gọi
    def export_sale_excel():
        conn = get_db_connection()
    
        # Lấy tham số từ frontend
        q = request.args.get('q', '').strip()
        start = request.args.get('start')          # YYYY-MM-DD
        end = request.args.get('end')              # YYYY-MM-DD
        status_filter = request.args.get('status', '')  # "" (all), "draft", "pending", "completed"

        # Xây dựng điều kiện WHERE
        where_clauses = ["1=1"]
        params = []

        # Filter ngày
        if start:
            where_clauses.append("date >= ?")
            params.append(f"{start} 00:00:00")

        if end:
            where_clauses.append("date <= ?")
            params.append(f"{end} 23:59:59")

        # Filter trạng thái
        if status_filter:
            if status_filter == "draft":
                where_clauses.append("status = 'draft'")
            elif status_filter == "pending":
                where_clauses.append("status = 'pending'")
            elif status_filter == "completed":
                where_clauses.append("status = 'completed'")
            # Nếu frontend gửi nhiều status (ví dụ "draft,pending"), bạn có thể split và dùng IN
            # else: bỏ qua hoặc xử lý all

        # Tìm kiếm từ khóa (q): mã đơn, tên KH, số HĐ
        if q:
            like = f"%{q}%"
            where_clauses.append("""
                (CAST(id AS TEXT) LIKE ?
                 OR customer_name LIKE ?
                 OR customer_phone LIKE ?
                 OR invoice_number LIKE ?)
            """)
            params.extend([like, like, like, like])

        # Câu SQL chính
        sql = f"""
            SELECT
                id AS 'Mã Đơn',
                date AS 'Ngày Tạo',
                customer_name AS 'Khách Hàng',
                customer_phone AS 'SĐT',
                invoice_number AS 'Số Hóa Đơn',
                total_amount AS 'Tổng Tiền (VNĐ)',
                status AS 'Trạng thái Đơn',
                invoice_status AS 'Trạng thái HĐ'
            FROM sale
            WHERE {' AND '.join(where_clauses)}
            ORDER BY id DESC
        """

        try:
            # Đọc dữ liệu trực tiếp bằng pandas
            df = pd.read_sql_query(sql, conn, params=params)

            # Format lại một số cột cho đẹp
            if not df.empty:
                # Format mã đơn có tiền tố ĐH000001
                df['Mã Đơn'] = df['Mã Đơn'].apply(lambda x: f"ĐH{str(x).zfill(6)}")
            
                # Format ngày giờ đẹp hơn
                df['Ngày Tạo'] = pd.to_datetime(df['Ngày Tạo']).dt.strftime('%d/%m/%Y %H:%M')

                # Đổi trạng thái thành tiếng Việt cho thân thiện
                status_map = {
                    'draft': 'ĐƠN TẠM',
                    'pending': 'CHỜ XỬ LÝ',
                    'completed': 'HOÀN TẤT'
                }
                df['Trạng thái Đơn'] = df['Trạng thái Đơn'].map(status_map).fillna(df['Trạng thái Đơn'])

            else:
                # Không có dữ liệu → thêm dòng thông báo
                df = pd.DataFrame([{'Thông báo': 'Không tìm thấy đơn hàng phù hợp với bộ lọc'}])

            # Tạo file Excel trong memory
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='DanhSachDonHang')

                worksheet = writer.sheets['DanhSachDonHang']

                # Auto-fit độ rộng cột
                for idx, col in enumerate(df.columns):
                    max_len = max(
                        df[col].astype(str).map(len).max(),
                        len(str(col))
                    ) + 4  # padding
                    worksheet.column_dimensions[chr(65 + idx)].width = max_len

                # Format cột Tổng tiền (VNĐ)
                total_col = df.columns.get_loc('Tổng Tiền (VNĐ)') + 1  # 1-based
                for row in range(2, len(df) + 2):
                    cell = worksheet.cell(row=row, column=total_col)
                    cell.number_format = '#,##0 ₫'

                # Freeze header row
                worksheet.freeze_panes = 'A2'

            output.seek(0)

            # Tên file động theo thời gian
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"Bao_cao_don_hang_{timestamp}.xlsx"

            return send_file(
                output,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True,
                download_name=filename
            )

        except Exception as e:
            # Xử lý lỗi (trả về thông báo hoặc log)
            return make_response(f"Lỗi khi xuất Excel: {str(e)}", 500)

        finally:
            conn.close()

    # CẬP NHẬT THÔNG TIN KHÁCH HÀNG TRONG ĐƠN HÀNG
    @app.route('/api/sale/<int:sale_id>/customer', methods=['PUT'])
    def api_update_sale_customer(sale_id):
        data = request.get_json()

        customer_name = data.get('customer_name', '').strip()
        company_name  = data.get('company_name', '').strip() or None
        tax_code      = data.get('tax_code', '').strip() or None
        address       = data.get('address', '').strip() or None

        try:
            conn = get_db_connection()
            c = conn.cursor()

            c.execute("""
                UPDATE sale
                SET customer_name = ?, 
                    company_name  = ?, 
                    tax_code      = ?, 
                    address       = ?
                WHERE id = ?
            """, (customer_name, company_name, tax_code, address, sale_id))

            conn.commit()
            return jsonify({"success": True}), 200
    
        except Exception as e:
            print("Lỗi cập nhật khách hàng:", e)
            return jsonify({"success": False, "message": "Lỗi server"}), 500

        finally:
            conn.close()

    @app.route('/api/sale')
    def api_sale():
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, date, customer_name, total_amount, payment_method FROM sale ORDER BY id DESC")
        rows = c.fetchall()
        conn.close()

        data = []
        for r in rows:
            data.append({
                "id": r[0],
                "date": r[1],
                "customer": r[2],
                "total": r[3],
                "payment_method": r[4]
            })

        return jsonify({"status": "success", "data": data})

    # LẤY DANH SÁCH ĐƠN HÀNG (dùng cho select box)
    @app.route('/api/sale/list', methods=['GET'])
    def api_sale_list():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row # Đảm bảo truy cập bằng tên cột
        c = conn.cursor()

        # 1. LẤY THÔNG SỐ TỪ REQUEST
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        q = request.args.get('q', '').strip()
        start = request.args.get('start')
        end = request.args.get('end')
        status = request.args.get('status')
        offset = (page - 1) * limit

        # Khởi tạo câu lệnh WHERE nguyên bản
        where = ["1=1"]
        params = []

        # 2. XỬ LÝ BỘ LỌC (Giữ nguyên logic của bạn)
        if start:
            start_dt = start if len(start) > 10 else f"{start} 00:00:00"
            where.append("s.date >= ?")
            params.append(start_dt)

        if end:
            end_dt = end if len(end) > 10 else f"{end} 23:59:59"
            where.append("s.date <= ?")
            params.append(end_dt)

        if q:
            like_pattern = f"%{q}%"
            where.append("(CAST(s.id AS TEXT) LIKE ? OR COALESCE(s.customer_name,'') LIKE ? OR COALESCE(s.customer_phone,'') LIKE ? OR COALESCE(s.invoice_number,'') LIKE ?)")
            params.extend([like_pattern, like_pattern, like_pattern, like_pattern])

        # Fix logic lọc trạng thái để Tab "Chờ xuất" hiện đúng đơn
        if status == "draft":
            where.append("s.status = 'draft'")
        elif status == "pending":
            # CHỜ XUẤT: Trạng thái completed + invoice_number trống (NULL hoặc '')
            where.append("s.status = 'completed' AND (s.invoice_number IS NULL OR s.invoice_number = '')")
        elif status == "completed":
            # HOÀN TẤT: Thường bao gồm cả đã xuất và chưa xuất nhưng đã thanh toán
            where.append("s.status = 'completed'")

        where_sql = " AND ".join(where)
        params_list = list(params)

        # SME multi-branch: lọc theo CN đang chọn
        try:
            from Services.sme.branches import active_report_branch_filter, sale_branch_filter_sql
            br = active_report_branch_filter()
            if br is not None:
                bf, bp = sale_branch_filter_sql(conn, br, alias='s')
                where_sql = where_sql + bf
                params_list.extend(bp)
        except Exception:
            pass

        # 3. LẤY TỔNG SỐ BẢN GHI (Cho phân trang)
        c.execute(f"SELECT COUNT(*) FROM sale s WHERE {where_sql}", params_list)
        total_records = c.fetchone()[0]

        # 4. LẤY DANH SÁCH DỮ LIỆU (Giữ nguyên cấu trúc trả về)
        sql_list = f"""
            SELECT s.*, COALESCE(s.invoice_status, 'none') as inv_status, tax_authority_status
            FROM sale s WHERE {where_sql}
            ORDER BY s.date DESC, s.id DESC
            LIMIT ? OFFSET ?
        """
        c.execute(sql_list, params_list + [limit, offset])
        rows = c.fetchall()

        orders = []
        for r in rows:
            d = dict(r)
            d["sale_no"] = f"ĐH{str(d['id']).zfill(6)}"
            # Đảm bảo frontend nhận biết được đã có số HĐ hay chưa
            d["has_invoice"] = True if (d.get('invoice_number') and d['invoice_number'].strip() != '') else False
            orders.append(d)

        # 5. TỔNG HỢP THỐNG KÊ (Fix để đếm đúng dựa trên ảnh DB)
        # Lấy điều kiện thời gian cho thống kê (không lọc theo status tab)
        stats_where = ["1=1"]
        stats_params = []
        if start:
            stats_where.append("s.date >= ?")
            stats_params.append(start if len(start) > 10 else f"{start} 00:00:00")
        if end:
            stats_where.append("s.date <= ?")
            stats_params.append(end if len(end) > 10 else f"{end} 23:59:59")

        stats_sql = " AND ".join(stats_where)
        try:
            from Services.sme.branches import active_report_branch_filter, sale_branch_filter_sql
            br = active_report_branch_filter()
            if br is not None:
                bf, bp = sale_branch_filter_sql(conn, br, alias='s')
                stats_sql = stats_sql + bf
                stats_params = stats_params + list(bp)
        except Exception:
            pass

        # Doanh thu (Chỉ tính đơn đã xong)
        c.execute(f"SELECT SUM(s.total_amount) FROM sale s WHERE s.status = 'completed' AND {stats_sql}", stats_params)
        revenue = c.fetchone()[0] or 0

        # TỔNG CHỜ XUẤT (Đã xong nhưng invoice_number trống)
        c.execute(f"SELECT COUNT(*) FROM sale s WHERE s.status = 'completed' AND (s.invoice_number IS NULL OR s.invoice_number = '') AND {stats_sql}", stats_params)
        pending_invoice = c.fetchone()[0]

        # TỔNG ĐÃ XUẤT
        c.execute(f"SELECT COUNT(*) FROM sale s WHERE (s.invoice_number IS NOT NULL AND s.invoice_number != '') AND {stats_sql}", stats_params)
        issued_invoice = c.fetchone()[0]

        conn.close()

        return jsonify({
            "orders": orders,
            "total": total_records,
            "stats": {
                "revenue": float(revenue),
                "pending_invoice": int(pending_invoice),
                "issued_invoice": int(issued_invoice)
            }
        }), 200

    #==== API TẠO ĐƠN HÀNG VỚI KHÁCH HÀNG TẠI ORDER.HTML ====#
    @app.route('/api/sale/create_with_customer', methods=['POST'])
    def api_create_sale_with_customer():
        data = request.get_json()
    
        customer_name = normalize_retail_buyer_name(data.get('customer_name') or DEFAULT_RETAIL_BUYER_NAME)
        company_name  = data.get('company_name', '').strip()
        tax_code      = data.get('tax_code', '').strip()
        address       = data.get('address', '').strip()

        try:
            # 1. Tạo đơn bán hàng mới (bảng sale)
            cursor.execute("""
                INSERT INTO sale (date, total_amount, customer_name, company_name, tax_code, address, status)
                VALUES (CURDATE(), 0, ?, ?, ?, ?, 'completed')
            """, (customer_name, company_name or None, tax_code or None, address or None))
            sale_id = cursor.lastrowid
            db.commit()

            return jsonify({
                "success": True,
                "order_id": sale_id,  # vẫn trả về để hiển thị ĐH000123
                "message": "Tạo đơn bán hàng thành công!"
            })
        except Exception as e:
            db.rollback()
            return jsonify({"success": False, "message": str(e)}), 500

    #==== API XÓA ĐƠN HÀNG BÁN ====#
    @app.route('/api/sale/delete/<int:sale_id>', methods=['DELETE'])
    def delete_sale(sale_id):
        conn = get_db_connection()
        c = conn.cursor()
    
        try:
            begin_immediate(conn, label='delete_sale')
            try:
                from Services.sme.branches import active_report_branch_filter, assert_sale_in_branch
                if active_report_branch_filter() is not None:
                    assert_sale_in_branch(conn, sale_id)
            except ValueError as ve:
                rollback_quietly(conn)
                return jsonify({"success": False, "error": str(ve)}), 403

            # 1. Kiểm tra đơn hàng tồn tại và chưa xuất hóa đơn
            c.execute("""
                SELECT id, invoice_number, status 
                FROM sale 
                WHERE id = ?
            """, (sale_id,))
            sale_row = c.fetchone()
        
            if not sale_row:
                rollback_quietly(conn)
                return jsonify({"success": False, "error": "Đơn hàng không tồn tại"}), 404
        
            if sale_row["invoice_number"]:
                rollback_quietly(conn)
                return jsonify({"success": False, "error": "Không thể xóa đơn hàng đã xuất hóa đơn"}), 403
        
            # (Tùy chọn) Kiểm tra thêm trạng thái nếu bạn có cột status
            # if sale_row["status"] in ["completed", "locked"]: ...

            sale_id_db = sale_row["id"]

            # 2. Lấy chi tiết đơn hàng để hoàn kho
            c.execute("""
                SELECT 
                    si.product_id,
                    si.quantity,
                    si.cost_price,
                    si.UseSaleUnit,
                    COALESCE(p.unit_ratio, 1) AS unit_ratio
                FROM sale_items si
                LEFT JOIN products p ON p.id = si.product_id
                WHERE si.sale_id = ?
            """, (sale_id_db,))
            items = c.fetchall()
            product_ids = [item["product_id"] for item in items]

            # 3. Hoàn sổ cái + sync inventory từ stock_moves (không cộng tay inventory)
            revert_sale_stock(c, sale_id_db, product_ids)

            # 4. Xóa dữ liệu liên quan (bảng con trước — tránh FK)
            _delete_sale_child_rows(c, sale_id_db)
            c.execute("DELETE FROM sale WHERE id = ?", (sale_id_db,))

            conn.commit()
        
            from Services.audit_log import write_audit
            write_audit(
                'delete', 'sale',
                f"Xóa đơn bán #{sale_id_db}",
                entity_type='sale', entity_id=sale_id_db,
                old_data={'sale_id': sale_id_db, 'items_count': len(items)},
            )

            return jsonify({
                "success": True, 
                "message": "Đã xóa đơn hàng và hoàn kho thành công!"
            })

        except Exception as e:
            rollback_quietly(conn)
            print(f"Lỗi khi xóa đơn hàng {sale_id}: {str(e)}")
            import traceback
            traceback.print_exc()
            return jsonify({
                "success": False, 
                "error": "Lỗi hệ thống khi xóa đơn hàng. Vui lòng thử lại sau."
            }), 500
        
        finally:
            conn.close()

    #=== KHÁCH TRẢ HÀNG ====#
    @app.route('/api/return/sale', methods=['POST'])
    @login_required
    def api_return_sale():
        data = request.get_json() or {}
        required = ['sale_id', 'product_id', 'quantity', 'UseSaleUnit']
        for k in required:
            if k not in data:
                return jsonify({"error": f"Thiếu trường: {k}"}), 400

        try:
            sale_id = int(data['sale_id'])
            product_id = int(data['product_id'])
            use_unit = int(data['UseSaleUnit']) 
            qty_return = float(data['quantity'])
            if qty_return <= 0:
                return jsonify({"error": "Số lượng phải > 0"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Dữ liệu không hợp lệ"}), 400

        reason = (data.get("reason") or "").strip() or "Khách trả hàng"
        return_date = data.get("date") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db_connection()
        c = conn.cursor()
        try:
            begin_immediate(conn, label='api_return_sale')

            # === 1. Kiểm tra bảng sale (Dùng id) ===
            c.execute("""
                SELECT sale_no, customer_name, total_amount, status, invoice_status 
                FROM sale WHERE id = ?
            """, (sale_id,))
            sale_master = c.fetchone()
        
            if not sale_master:
                return jsonify({"error": "Không tìm thấy đơn hàng"}), 404
        
            if sale_master['invoice_status'] != 'none':
                return jsonify({"error": "Đơn hàng đã xuất hóa đơn thuế, không thể sửa đổi trực tiếp."}), 403

            # === 2. Lấy thông tin Item từ sale_items (Dùng sale_id & product_id & UseSaleUnit) ===
            c.execute("""
                SELECT si.quantity AS sold_qty, si.price, si.discount_pct, si.tax_pct,
                       si.cost_price AS cost_price_base, 
                       COALESCE(p.unit_ratio, 1) AS unit_ratio,
                       p.name AS product_name, p.unit AS base_unit,
                       p.unit1 AS sale_unit, p.barcode
                FROM sale_items si
                JOIN products p ON p.id = si.product_id
                WHERE si.sale_id = ? AND si.product_id = ? AND si.UseSaleUnit = ?
            """, (sale_id, product_id, use_unit))
            item = c.fetchone()
        
            if not item:
                return jsonify({"error": "Sản phẩm không tồn tại trong đơn hàng này"}), 404

            sold_qty = float(item['sold_qty'])
            if qty_return > sold_qty:
                return jsonify({"error": f"Số lượng trả ({qty_return}) vượt quá số lượng đã bán ({sold_qty})"}), 400

            # === 3. Xác định Trả Toàn Bộ hay Trả Một Phần ===
            # Kiểm tra tổng số lượng tất cả các mặt hàng khác trong đơn hàng
            c.execute("""
                SELECT SUM(quantity) FROM sale_items 
                WHERE sale_id = ? AND NOT (product_id = ? AND UseSaleUnit = ?)
            """, (sale_id, product_id, use_unit))
            other_items_qty = float(c.fetchone()[0] or 0)
        
            # Điều kiện trả 100% đơn: Trả hết dòng hiện tại VÀ các dòng khác đều bằng 0
            is_full_return = (qty_return == sold_qty and other_items_qty == 0)

            # === 4. Cập nhật SALE & SALE_ITEMS ===
            if is_full_return:
                # TRẢ TOÀN BỘ: hủy đơn + doanh thu = 0 (tránh phình P&L nếu quên lọc status)
                new_status = 'cancelled'
                new_total_amount = 0
                c.execute("""
                    UPDATE sale_items SET quantity = 0
                    WHERE sale_id = ?
                """, (sale_id,))
            else:
                # TRẢ MỘT PHẦN: Cập nhật giảm số lượng trong sale_items
                new_status = sale_master['status']
                new_qty = max(0, sold_qty - qty_return)
                c.execute("""
                    UPDATE sale_items SET quantity = ? 
                    WHERE sale_id = ? AND product_id = ? AND UseSaleUnit = ?
                """, (new_qty, sale_id, product_id, use_unit))
            
                # Tính lại đúng như checkout: sau chiết khấu + VAT từng dòng.
                remaining_rows = c.execute("""
                    SELECT quantity, price, discount_pct, tax_pct
                    FROM sale_items WHERE sale_id = ?
                """, (sale_id,)).fetchall()
                new_total_amount = sum(
                    _calc_sale_line_amounts(
                        row['quantity'], row['price'], row['discount_pct'], row['tax_pct'],
                    )['line_total']
                    for row in remaining_rows
                )

            c.execute("UPDATE sale SET total_amount = ?, status = ?, updated_at = ? WHERE id = ?", 
                     (new_total_amount, new_status, return_date, sale_id))

            # === 5. Hoàn Kho & WAC (sổ cái trước, sync snapshot sau) ===
            cost_price_base = float(item['cost_price_base'])
            unit_ratio = float(item['unit_ratio'])
            real_qty_in = qty_return * unit_ratio if use_unit == 1 else qty_return

            apply_wac_inbound(
                c, product_id, real_qty_in, real_qty_in * cost_price_base,
            )

            c.execute("""
                INSERT INTO inventory_transactions (product_id, type, type1, quantity, cost_price, reference_id, reference_type, note, created_at)
                VALUES (?, 'import', 'Khách Trả', ?, ?, ?, 'sale_return', ?, ?)
            """, (product_id, real_qty_in, cost_price_base, sale_id, f"Hoàn hàng đơn {sale_master['sale_no']}", return_date))

            c.execute("""
                INSERT INTO stock_moves (product_id, date, type, ref_id, quantity, cost_price, note, ref_document, ref_type, type1)
                VALUES (?, ?, 'RETURN_SALE', ?, ?, ?, ?, ?, 'import', 'Khách Trả')
            """, (product_id, return_date, sale_id, real_qty_in, cost_price_base,
                  f"Trả đơn {sale_master['sale_no']} - {reason}", sale_master['sale_no']))

            sync_inventory_quantity_from_moves(c, product_id)

            # === 6. Ghi return_sales ===
            c.execute(
                "INSERT INTO return_sales (date, sale_id, product_id, quantity, UseSaleUnit, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (return_date, sale_id, product_id, qty_return, use_unit, reason),
            )
            return_sales_id = c.lastrowid

            # === 7. Tạo Chứng từ Nhập Kho ===
            c.execute("SELECT import_no FROM phieu_nhap_kho WHERE import_no LIKE 'PN%' ORDER BY id DESC LIMIT 1")
            last_pn = c.fetchone()
            pn_num = (int(last_pn['import_no'][2:]) + 1) if (last_pn and last_pn['import_no'][2:].isdigit()) else 1
            import_no = f"PN{pn_num:06d}"

            refund_amount = _calc_sale_line_amounts(
                qty_return,
                item['price'],
                item['discount_pct'],
                item['tax_pct'],
            )['line_total']
            items_json = [{
                "product_name": item['product_name'], "unit": item['sale_unit'] if use_unit == 1 else item['base_unit'],
                "qty": qty_return, "import_price": cost_price_base * (unit_ratio if use_unit == 1 else 1),
                "tax_pct": 0, "discount_pct": 0, "barcode": item['barcode']
            }]

            c.execute("""
                INSERT INTO phieu_nhap_kho (import_no, date, supplier_name, items_json, total_amount, import_id, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (import_no, return_date, sale_master['customer_name'], json.dumps(items_json), refund_amount, sale_id, f"Nhập từ đơn {sale_master['sale_no']}"))
        
            new_pn_id = c.lastrowid
            c.execute("""
                INSERT INTO chi_tiet_phieu_nhap_kho (import_id, product_id, quantity, cost_price, unit_type, note, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (new_pn_id, product_id, qty_return, cost_price_base, use_unit, f"Hoàn hàng {sale_master['sale_no']}", return_date))

            # === 8. Phiếu chi & Công nợ ===
            voucher_no = None
            if refund_amount > 0:
                # 1. Tìm số lớn nhất bằng cách cắt chuỗi 'PC' và chuyển sang kiểu số (Integer)
                c.execute("SELECT MAX(CAST(SUBSTR(voucher_no, 3) AS INTEGER)) FROM Phieu_chi WHERE voucher_no LIKE 'PC%'")
                row = c.fetchone()
                # 2. Nếu tìm thấy (không NULL) thì cộng 1, ngược lại (bảng trống) thì bắt đầu từ 1
                max_num = row[0] if (row and row[0] is not None) else 0
                pc_num = max_num + 1
                voucher_no = f"PC{pc_num:06d}"

                c.execute("""
                    INSERT INTO Phieu_chi (voucher_no, receiver_name, amount, debit_account, credit_account, reason,
                                          source_type, reference_document, source_id, preparer, date, expense_type)
                    VALUES (?, ?, ?, '511', '111', ?, 'return_sale', ?, ?, ?, ?, 'Trả Khách')
                """, (voucher_no, sale_master['customer_name'], refund_amount, f"Hoàn tiền đơn {sale_master['sale_no']}", 
                      sale_master['sale_no'], sale_id, session.get('user_name', 'Admin'), return_date))
            
                c.execute("UPDATE cong_no SET unpaid_amount = MAX(0, unpaid_amount - ?) WHERE sale_no = ?", 
                         (refund_amount, sale_master['sale_no']))

            enqueue_accounting_job(
                conn,
                sale_id,
                accounting_regime=get_current_tenant_profile().get('accounting_regime'),
                features=get_current_tenant_profile().get('features'),
                created_by=session.get('user_name'),
                replace_existing=True,
            )
            try:
                from Services.sme.return_sale_journal import sync_return_sale_journals
                sync_return_sale_journals(
                    conn,
                    int(return_sales_id),
                    sale_id=sale_id,
                    product_id=product_id,
                    quantity=qty_return,
                    unit_price=float(item['price']) * (1 - float(item['discount_pct'] or 0) / 100.0),
                    tax_pct=float(item['tax_pct'] or 0),
                    cost_price=cost_price_base,
                    posting_date=str(return_date)[:10],
                    sale_no=sale_master['sale_no'],
                    customer_name=sale_master['customer_name'],
                    created_by=session.get('user_name'),
                )
            except Exception:
                pass
            conn.commit()
            return jsonify({
                "success": True,
                "import_no": import_no,
                "return_id": return_sales_id,
                "is_full_return": is_full_return,
                "new_status": new_status,
            })

        except Exception as e:
            conn.rollback()
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()

    # === API CẬP NHẬT ĐƠN HÀNG ===
    @app.route('/api/sale/update/<int:order_id>', methods=['PUT'])
    def api_sale_update(order_id):
        data = request.get_json()
        db = get_db_connection()
    
        try:
            cur = db.cursor()
            cur.execute("""
                UPDATE sale SET
                    customer_name = ?,
                    company_name = ?,
                    tax_code = ?,
                    address = ?
                WHERE id = ?
            """, (
                data.get('customer_name', '').strip(),
                data.get('company_name', '').strip() or None,
                data.get('tax_code', '').strip() or None,
                data.get('address', '').strip() or None,
                order_id
            ))
            db.commit()
            return jsonify({"success": True, "message": "Cập nhật thành công"})
        except Exception as e:
            db.rollback()
            return jsonify({"error": str(e)}), 500

    @app.route('/sale')
    @login_required
    def sale():
        return render_template('sale.html')

    @app.route('/api/pos/catalog', methods=['GET'])
    @login_required
    def api_pos_catalog():
        include_menu = request.args.get('include_menu', '0') == '1'
        conn = get_db_connection()
        try:
            ensure_pos_offline_schema(conn, commit=False)
            from Services.user_branch import get_current_user_warehouse_codes
            wh_codes = get_current_user_warehouse_codes()
            payload = fetch_pos_catalog(conn, include_menu=include_menu, warehouse_codes=wh_codes)
            return jsonify({'success': True, **payload})
        except Exception as e:
            logging.exception('api_pos_catalog')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sale/<int:sale_id>/accounting-status')
    @login_required
    def api_sale_accounting_status(sale_id):
        from Services.accounting_queue import get_sale_accounting_status
        conn = get_db_connection()
        try:
            conn.row_factory = sqlite3.Row
            status = get_sale_accounting_status(conn, sale_id)
            return jsonify({'success': True, **status})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/sw-pos.js')
    def sw_pos():
        from flask import send_from_directory
        import os
        return send_from_directory(
            os.path.join(app.root_path, 'static'),
            'sw-pos.js',
            mimetype='application/javascript',
            max_age=3600,
        )

    @app.route('/return/sale')
    @login_required
    def return_sale_page():
        return render_template('return_sale.html')
    @app.route('/sale/invoice/<int:order_id>')
    def invoice_order(order_id):
        return render_template('invoice_einvoice.html')

    @app.route('/sale_details')
    @login_required
    def sale_details_page():
        return render_template('sale_details.html')

    @app.route('/api/sale/details-report', methods=['POST'])
    @login_required
    def api_sale_details_report():
        data = request.get_json(silent=True) or {}
        start_date = (data.get('start_date') or '').strip()
        end_date = (data.get('end_date') or '').strip()
        search_query = (data.get('q') or '').strip() or None

        if not start_date or not end_date:
            return jsonify({"success": False, "error": "Thiếu tham số ngày bắt đầu/kết thúc"}), 400

        try:
            datetime.strptime(start_date, '%Y-%m-%d')
            datetime.strptime(end_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({"success": False, "error": "Định dạng ngày không hợp lệ (YYYY-MM-DD)"}), 400

        if start_date > end_date:
            return jsonify({"success": False, "error": "Ngày bắt đầu phải nhỏ hơn hoặc bằng ngày kết thúc"}), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.branches import active_report_branch_filter
            cursor = conn.cursor()
            rows, summary = fetch_sale_items_detail_report(
                cursor, start_date, end_date, search_query,
                branch_code=active_report_branch_filter(),
            )
            return jsonify({
                "success": True,
                "data": rows,
                "summary": summary,
                "start_date": start_date,
                "end_date": end_date,
            })
        except Exception as e:
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

