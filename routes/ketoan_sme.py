"""Routes kế toán SME — tách từ app.py."""
import json
import logging
import os
import re
import sqlite3
import traceback
import uuid
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

import pandas as pd
import requests
from flask import (
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from db_utils import BASE_DIR, MAIN_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)



def round_money(val):
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def register_ketoan_sme_routes(app):
    """Đăng ký route KeToan SME (giữ nguyên URL/endpoint)."""
    from auth import login_required
    from helpers import parse_date
    from Services.tenant_profile import require_sme_regime

    def _bootstrap_sme_db():
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile()
            return ensure_sme_accounting_ready(
                conn,
                accounting_regime=profile.get('accounting_regime'),
            )
        finally:
            conn.close()

    #============================================================================== Start of SME Accounting=========================================================================#
    @app.route('/SME_dashboard')
    @login_required
    @require_sme_regime
    def SME_dashboard():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on dashboard')
        return render_template('KeToanSME/main_dashboard.html')

    @app.route('/SME_order')
    @login_required
    def SME_order():
        return redirect(url_for('order'))

    @app.route('/SME_purchasing')
    @login_required
    def SME_purchasing():
        return render_template('KeToanSME/dashboard_purchasing.html')

    @app.route('/SME_dashboard_sale')
    @login_required
    def SME_dashboard_sale():
        return render_template('KeToanSME/dashboard_sale.html')

    @app.route('/SME_sale_details')
    @login_required
    def SME_sale_details():
        return render_template('sale_details.html')

    @app.route('/SME_purchase_order_create')
    @login_required
    @require_sme_regime
    def SME_purchase_order_create():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/purchase_order_create.html')

    @app.route('/SME_purchase_order_list')
    @login_required
    @require_sme_regime
    def SME_purchase_order_list():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/purchase_order_list.html')

    @app.route('/SME_inward_invoice')
    @login_required
    def SME_inward_invoice():
        return render_template('KeToanSME/inward_invoice.html')

    @app.route('/SME_import')
    @login_required
    def SME_import():
        return render_template('KeToanSME/import_sme.html')

    @app.route('/SME_import_list')
    @login_required
    def SME_DanhSachPhieuNhapKho():
        return render_template('KeToanSME/import_list.html')

    @app.route('/SME_return_supplier')
    @login_required
    def SME_return_supplier():
        return render_template('KeToanSME/return_supplier.html')

    @app.route('/SME_SoCongNoPhaiTra')
    @login_required
    def SME_SoCongNoPhaiTra():
        return render_template('KeToanSME/SoCongNoPhaiTra.html')

    @app.route('/SME_dashboard_warehouse')
    @login_required
    def SME_dashboard_warehouse():
        return render_template('KeToanSME/dashboard_warehouse.html')

    @app.route('/SME_dashboard_debt')
    @login_required
    def SME_dashboard_debt():
        return render_template('KeToanSME/dashboard_debt.html')

    @app.route('/SME_PhaiThuCongNhanVien')
    @login_required
    @require_sme_regime
    def SME_PhaiThuCongNhanVien():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/employee_receivable.html')

    @app.route('/SME_PhaiTraCongNhanVien')
    @login_required
    def SME_PhaiTraCongNhanVien():
        return redirect(url_for('SoCongNoPhaiTraNhanVien'))

    @app.route('/SME_dashboard_HRSalary')
    @login_required
    def SME_dashboard_HRSalary():
        return render_template('KeToanSME/dashboard_HRSalary.html')

    @app.route('/SME_SoSachKeToan')
    @login_required
    @require_sme_regime
    def SME_SoSachKeToan():
        return render_template('KeToanSME/dashboard_sosachketoan.html')

    @app.route('/SME_TSCD')
    @login_required
    def SME_TSCD():
        return render_template('KeToanSME/dashboard_TSCD.html')

    @app.route('/SME_CCDC')
    @login_required
    def SME_CCDC():
        return render_template('KeToanSME/dashboard_CCDC.html')

    @app.route('/SME_BCTC')
    @login_required
    @require_sme_regime
    def SME_BCTC():
        return render_template('KeToanSME/dashboard_BCTC.html')

    @app.route('/SME_BCTC/reports')
    @login_required
    @require_sme_regime
    def SME_BCTC_reports():
        return render_template('KeToanSME/bctc_reports.html')

    @app.route('/SME_vat_declaration')
    @login_required
    @require_sme_regime
    def SME_vat_declaration():
        return render_template('KeToanSME/vat_declaration.html')

    @app.route('/SME_form_01_bh')
    @login_required
    @require_sme_regime
    def SME_form_01_bh():
        return render_template('KeToanSME/form_01_bh.html')

    @app.route('/SME_form_02_bh')
    @login_required
    @require_sme_regime
    def SME_form_02_bh():
        return render_template('KeToanSME/form_02_bh.html')

    @app.route('/SME_SoQuyTienMat')
    def SME_SoQuyTienMat():
        selected_year = request.args.get('year', default=datetime.today().year, type=int)
        current_year_today = datetime.today().year
        ngay_in = datetime.today().strftime('%d/%m/%Y')

        conn = get_db_connection()
        info = dict(conn.execute("SELECT * FROM business_info LIMIT 1").fetchone() or {})
    
        # Lấy toàn bộ dữ liệu 2 bảng
        rows_thu = [dict(row) for row in conn.execute("SELECT * FROM phieu_thu").fetchall()]
        rows_chi = [dict(row) for row in conn.execute("SELECT * FROM phieu_chi").fetchall()]
        conn.close()

        ds_phat_sinh = []
        tong_thu_truoc = 0
        tong_chi_truoc = 0

        # 2. XỬ LÝ PHIẾU THU (Dòng này quan trọng)
        for r in rows_thu:
            # Kiểm tra CẢ cột debit_account VÀ credit_account để tránh nhập nhầm
            # Dùng .startswith('111') để lấy cả 111, 1111, 1112...
            acc_debit = str(r.get('debit_account') or '').strip()
            acc_credit = str(r.get('credit_account') or '').strip() # Kiểm tra thêm cột này
        
            dt = parse_date(r.get('date'))
        
            # Nếu bất kỳ cột nào chứa 111 thì coi như có phát sinh tiền mặt
            if (acc_debit.startswith('111') or acc_credit.startswith('111')) and dt:
                amt = float(r.get('amount') or 0)
                if dt.year < selected_year:
                    tong_thu_truoc += amt
                elif dt.year == selected_year:
                    ds_phat_sinh.append({
                        'date': dt.strftime('%Y-%m-%d'),
                        'date_obj': dt,
                        'so_hieu': r.get('voucher_no') or r.get('so_phieu') or 'PT',
                        'dien_giai': r.get('reason') or r.get('description') or 'Thu tiền mặt',
                        'thu': amt,
                        'chi': 0
                    })

        # 3. XỬ LÝ PHIẾU CHI
        for r in rows_chi:
            acc_credit = str(r.get('credit_account') or '').strip()
            acc_debit = str(r.get('debit_account') or '').strip()
        
            dt = parse_date(r.get('date'))
        
            if (acc_credit.startswith('111') or acc_debit.startswith('111')) and dt:
                amt = float(r.get('amount') or 0)
                if dt.year < selected_year:
                    tong_chi_truoc += amt
                elif dt.year == selected_year:
                    ds_phat_sinh.append({
                        'date': dt.strftime('%Y-%m-%d'),
                        'date_obj': dt,
                        'so_hieu': r.get('voucher_no') or r.get('so_phieu') or 'PC',
                        'dien_giai': r.get('reason') or r.get('description') or 'Chi tiền mặt',
                        'thu': 0,
                        'chi': amt
                    })

        # 4. TÍNH TOÁN & SẮP XẾP
        so_du_dau_ky = tong_thu_truoc - tong_chi_truoc
        ds_phat_sinh.sort(key=lambda x: (x['date_obj'], x['chi']))

        tong_thu_trong_ky = 0
        tong_chi_trong_ky = 0
        current_ton = so_du_dau_ky
    
        for item in ds_phat_sinh:
            tong_thu_trong_ky += item['thu']
            tong_chi_trong_ky += item['chi']
            current_ton += (item['thu'] - item['chi'])
            item['ton'] = current_ton

        return render_template(
            'KeToanSME/SME_SoQuyTienMat.html',
            year=selected_year,
            info=info,
            so_du_dau_ky=so_du_dau_ky,
            ds_phat_sinh=ds_phat_sinh,
            tong_thu_trong_ky=tong_thu_trong_ky,
            tong_chi_trong_ky=tong_chi_trong_ky,
            so_du_cuoi_ky=current_ton,
            ngay_in=ngay_in,
            current_year_today=current_year_today
        )

    @app.route('/SME_SoTienGuiNganHang')
    @login_required
    def SME_SoTienGuiNganHang():
        selected_year = request.args.get('year', default=datetime.today().year, type=int)
        current_year_today = datetime.today().year
        ngay_in = datetime.today().strftime('%d/%m/%Y')

        conn = get_db_connection()
        info = dict(conn.execute("SELECT * FROM business_info LIMIT 1").fetchone() or {})
    
        rows_thu = [dict(row) for row in conn.execute("SELECT * FROM phieu_thu").fetchall()]
        rows_chi = [dict(row) for row in conn.execute("SELECT * FROM phieu_chi").fetchall()]
        conn.close()

        ds_phat_sinh = []
        tong_thu_truoc = 0
        tong_chi_truoc = 0

        # XỬ LÝ PHIẾU THU (Tiền gửi vào - Nợ 112)
        for r in rows_thu:
            acc_debit = str(r.get('debit_account') or '').strip()
            dt = parse_date(r.get('date'))
            if acc_debit.startswith('112') and dt:
                amt = float(r.get('amount') or 0)
                if dt.year < selected_year:
                    tong_thu_truoc += amt
                elif dt.year == selected_year:
                    ds_phat_sinh.append({
                        'date': dt.strftime('%Y-%m-%d'),
                        'date_obj': dt,
                        'so_hieu': r.get('voucher_no') or r.get('so_phieu') or 'PT',
                        'dien_giai': r.get('reason') or r.get('description') or 'Thu chuyển khoản',
                        'thu': amt,
                        'chi': 0
                    })

        # XỬ LÝ PHIẾU CHI (Rút tiền/Chuyển khoản đi - Có 112)
        for r in rows_chi:
            acc_credit = str(r.get('credit_account') or '').strip()
            dt = parse_date(r.get('date'))
            if acc_credit.startswith('112') and dt:
                amt = float(r.get('amount') or 0)
                if dt.year < selected_year:
                    tong_chi_truoc += amt
                elif dt.year == selected_year:
                    ds_phat_sinh.append({
                        'date': dt.strftime('%Y-%m-%d'),
                        'date_obj': dt,
                        'so_hieu': r.get('voucher_no') or r.get('so_phieu') or 'PC',
                        'dien_giai': r.get('reason') or r.get('description') or 'Chi chuyển khoản',
                        'thu': 0,
                        'chi': amt
                    })

        # TÍNH TOÁN SỐ DƯ
        so_du_dau_ky = tong_thu_truoc - tong_chi_truoc
        ds_phat_sinh.sort(key=lambda x: x['date_obj'])

        tong_thu_trong_ky = 0
        tong_chi_trong_ky = 0
        luy_ke = so_du_dau_ky
    
        for item in ds_phat_sinh:
            tong_thu_trong_ky += item['thu']
            tong_chi_trong_ky += item['chi']
            luy_ke = round(luy_ke + item['thu'] - item['chi'], 2)
            item['ton'] = luy_ke

        return render_template(
            'KeToanSME/SME_SoTienGuiNganHang.html',
            year=selected_year,
            info=info,
            so_du_dau_ky=so_du_dau_ky,
            ds_phat_sinh=ds_phat_sinh,
            tong_thu_trong_ky=tong_thu_trong_ky,
            tong_chi_trong_ky=tong_chi_trong_ky,
            so_du_cuoi_ky=luy_ke,
            ngay_in=ngay_in,
            current_year_today=current_year_today
        )


        if val is None:
            return Decimal('0.00')
        return Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @app.route('/api/import_sme', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_fb_import_post():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
    
        try:
            data = request.get_json()
            if not data:
                return jsonify({"error": "Dữ liệu gửi lên không hợp lệ"}), 400

            # --- 1. ĐỌC DỮ LIỆU ĐẦU VÀO CƠ BẢN VÀ THÔNG SỐ TỶ GIÁ ---
            import_type = data.get('import_type', 'DOMESTIC')  # DOMESTIC (Trong nước) hoặc IMPORT (Nhập khẩu)
            currency = data.get('currency', 'VND').strip().upper()
            exchange_rate = Decimal(str(data.get('exchange_rate', 1.0))) if import_type == 'IMPORT' else Decimal('1.0')
        
            items = data.get('items', [])
            supplier_id = data.get('supplier_id')
            import_date = data.get('date')
            bill_date = data.get('bill_date')
            import_no = data.get('import_no')
            bill_no = data.get('bill_no')
            tax_code = data.get('tax_code')  
            note = data.get('note')
            extra_cost = round_money(data.get('extra_cost', 0)) # Chi phí thu mua (VND)
            payment_status_input = data.get('payment_status', 'Chưa thanh toán')
        
            payment_method_raw = str(data.get('payment_method', 'cash')).strip().upper()
            if payment_status_input == 'Chưa thanh toán':
                payment_method = 'CREDIT'
            else:
                payment_method = 'CASH' if payment_method_raw == 'CASH' else 'BANK_TRANSFER'

            po_id = data.get('po_id')
            try:
                po_id = int(po_id) if po_id not in (None, '', 0, '0') else None
            except (TypeError, ValueError):
                po_id = None

            # Truy vấn thông tin Nhà cung cấp
            c.execute("SELECT name, address FROM suppliers WHERE id = ?", (supplier_id,))
            sup_row = c.fetchone()
            supplier_name = sup_row['name'] if sup_row else f"NCC ID {supplier_id}"
            supplier_address = sup_row['address'] if sup_row and sup_row['address'] else ""

            # --- 2. TÍNH TỔNG GIÁ TRỊ GỐC (QUY ĐỔI VND) ĐỂ PHÂN BỔ CHI PHÍ BIẾN ĐỔI ---
            total_base_vnd = Decimal('0.00')
            for i in items:
                qty = round_money(i.get('qty', 0))
                price_original = round_money(i.get('buyprice', 0))
                price_vnd = round_money(price_original * exchange_rate)
                total_base_vnd += round_money(qty * price_vnd)
            
            total_base_safe = total_base_vnd if total_base_vnd > 0 else Decimal('1.00')

            # --- 3. TẠO PHIẾU NHẬP KHO VẬT LÝ ---
            c.execute("""
                INSERT INTO import (date, supplier_id, import_no, bill_no, bill_date, note, payment_status, extra_cost, total_value, paid_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """, (import_date, supplier_id, import_no, bill_no, bill_date, note, payment_status_input, float(extra_cost)))
            import_id = c.lastrowid

            # --- 4. TRUY VẤN THÔNG TIN SẢN PHẨM HÀNG LOẠT ---
            p_ids = [i.get('product_id') for i in items if i.get('product_id')]
            p_map = {}
            if p_ids:
                placeholders = ','.join(['?'] * len(p_ids))
                c.execute(f"SELECT id, name, unit, unit1, unit_ratio, barcode FROM products WHERE id IN ({placeholders})", p_ids)
                p_map = {row['id']: row for row in c.fetchall()}

            # --- 5. PHÂN LOẠI NGHIỆP VỤ THEO THÔNG TƯ 99/2026 ---
            items_by_business = {'NHAP_KHO_HANG_HOA': [], 'NHAP_KHO_NVL': []}
            for item in items:
                frontend_type = item.get('invoice_product_type', 'ready_made')
                b_type = 'NHAP_KHO_NVL' if frontend_type == 'raw_materials' else 'NHAP_KHO_HANG_HOA'
                items_by_business[b_type].append(item)

            accounting_tx_ids = []
            items_for_json = []

            # --- 6. DUYỆT TỪNG NHÓM NGHIỆP VỤ: KHO VẬT LÝ + BÚT TOÁN SME ---
            from Services.sme.journal_engine import (
                build_import_stock_lines,
                ensure_sme_journal_ready,
                post_journal_entry,
            )
            ensure_sme_journal_ready(conn, commit=False)

            for b_type, business_items in items_by_business.items():
                if not business_items:
                    continue

                desc_text = "Nhập kho hàng hóa" if b_type == 'NHAP_KHO_HANG_HOA' else "Nhập kho nguyên vật liệu"
                tx_subtotal_payable_vnd = Decimal('0.00')
                tx_total_vat_vnd = Decimal('0.00')
                tx_total_import_tax_vnd = Decimal('0.00')
                journal_inventory_lines = []

                for item in business_items:
                    pid = item.get('product_id')
                    p_info = p_map.get(pid)
                    if not p_info:
                        continue

                    qty_in = round_money(item.get('qty', 0))
                    if qty_in <= 0:
                        continue

                    price_original = round_money(item.get('buyprice', 0))
                    tax_p = Decimal(str(item.get('tax_pct', 0) or 0))
                    import_tax_p = Decimal(str(item.get('import_tax_pct', 0) or 0)) if import_type == 'IMPORT' else Decimal('0.00')
                    disc_p = Decimal(str(item.get('discount_pct', 0) or 0))
                    unit_in = str(item.get('unit', '')).strip().lower()
                    frontend_type = item.get('invoice_product_type', 'ready_made')

                    product_name = p_info['name']
                    retail_unit = str(p_info['unit'] or "Cái").strip()
                    wholesale_unit = str(p_info['unit1'] or "").strip().lower()
                    ratio = Decimal(str(p_info['unit_ratio'] or 1))

                    is_wholesale = wholesale_unit and unit_in == wholesale_unit
                    qty_retail = qty_in * ratio if is_wholesale else qty_in

                    price_vnd = round_money(price_original * exchange_rate)
                    line_subtotal_vnd = round_money(qty_in * price_vnd)
                    line_disc_vnd = round_money(line_subtotal_vnd * (disc_p / Decimal('100.00')))
                    line_net_vnd = line_subtotal_vnd - line_disc_vnd

                    line_import_tax_vnd = Decimal('0.00')
                    if import_type == 'IMPORT' and import_tax_p > 0:
                        line_import_tax_vnd = round_money(line_net_vnd * (import_tax_p / Decimal('100.00')))
                        tx_total_import_tax_vnd += line_import_tax_vnd

                    line_extra_vnd = round_money((line_subtotal_vnd / total_base_safe) * extra_cost)
                    line_inventory_value_vnd = line_net_vnd + line_import_tax_vnd + line_extra_vnd
                    cost_per_retail_vnd = round_money(line_inventory_value_vnd / qty_retail) if qty_retail > 0 else Decimal('0.00')

                    tax_base_vnd = line_net_vnd + line_import_tax_vnd if import_type == 'IMPORT' else line_net_vnd
                    line_vat_vnd = round_money(tax_base_vnd * (tax_p / Decimal('100.00')))
                    tx_total_vat_vnd += line_vat_vnd

                    line_total_payment_vnd = line_net_vnd + line_vat_vnd + line_extra_vnd
                    tx_subtotal_payable_vnd += line_total_payment_vnd

                    items_for_json.append({
                        "product_id": pid, "product_name": product_name, "barcode": p_info['barcode'] or "",
                        "unit": item.get('unit'), "qty": float(qty_in), "buyprice": float(price_original),
                        "discount_pct": float(disc_p), "tax_pct": float(tax_p), "import_tax_pct": float(import_tax_p),
                        "line_total": float(line_total_payment_vnd), "invoice_product_type": frontend_type
                    })

                    params_detail = (
                        import_id, pid, float(qty_in), float(price_original), float(line_subtotal_vnd),
                        float(line_disc_vnd), float(line_vat_vnd), float(cost_per_retail_vnd),
                        1 if is_wholesale else 0, float(tax_p), float(disc_p)
                    )
                    c.execute(
                        "INSERT INTO import_details (import_id, product_id, qty, buyprice, subtotal, discount, tax, cost_price, unit_type, tax_pct, discount_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        params_detail,
                    )
                    c.execute(
                        "INSERT INTO chi_tiet_phieu_nhap_kho (import_id, product_id, quantity, buyprice, subtotal, discount_amount, tax_amount, cost_price, unit_type, tax_pct, discount_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        params_detail,
                    )

                    c.execute("SELECT quantity, avg_cost FROM inventory WHERE product_id = ?", (pid,))
                    inv = c.fetchone()
                    old_q = Decimal(str(inv['quantity'] if inv else 0))
                    old_c = Decimal(str(inv['avg_cost'] if inv else 0))
                    new_q = old_q + qty_retail
                    new_avg = round_money(((old_q * old_c) + line_inventory_value_vnd) / new_q) if new_q > 0 else cost_per_retail_vnd
                    c.execute(
                        """
                        INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?, ?, ?)
                        ON CONFLICT(product_id) DO UPDATE SET quantity=excluded.quantity, avg_cost=excluded.avg_cost
                        """,
                        (pid, float(new_q), float(new_avg)),
                    )
                    move_note = f"Nhập kho từ {supplier_name} ({desc_text})"
                    c.execute(
                        """
                        INSERT INTO stock_moves (product_id, date, type, ref_id, quantity, cost_price, note, ref_document, ref_type, type1, unit, unit1, unit_ratio)
                        VALUES (?, ?, 'import', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pid, import_date, import_id, float(qty_retail), float(cost_per_retail_vnd),
                            move_note, import_no, 'import', 'Nhập', retail_unit, wholesale_unit, float(ratio),
                        ),
                    )

                    journal_inventory_lines.append({
                        'product_id': pid,
                        'product_name': product_name,
                        'amount': line_inventory_value_vnd,
                        'tax_pct': float(tax_p),
                        'warehouse_code': (item.get('warehouse_code') or 'KHO_001'),
                        'description': f"Nhập kho [{desc_text}]: {product_name}",
                    })

                if not journal_inventory_lines:
                    continue

                _rule, journal_lines = build_import_stock_lines(
                    conn,
                    business_type=b_type,
                    payment_method=payment_method,
                    inventory_lines=journal_inventory_lines,
                    vat_amount=tx_total_vat_vnd,
                    import_tax_amount=tx_total_import_tax_vnd,
                    payable_amount=tx_subtotal_payable_vnd,
                    supplier_id=supplier_id,
                    bill_no=bill_no,
                    tax_code=tax_code,
                    import_type=import_type,
                    description=f"{desc_text} HĐ {bill_no or import_no}",
                )
                posted = post_journal_entry(
                    conn,
                    posting_date=import_date,
                    document_date=bill_date or import_date,
                    document_type='PNK',
                    document_no=import_no,
                    document_id=import_id,
                    business_type=b_type,
                    currency=currency,
                    exchange_rate=exchange_rate,
                    description=f"{desc_text} theo HĐ/Tờ khai số {bill_no or import_no}",
                    reference_document=bill_no,
                    created_by=(session.get('user') or {}).get('username'),
                    lines=journal_lines,
                )
                accounting_tx_ids.append(posted['id'])

            # --- 7. ĐỒNG BỘ PHIẾU IN HOÀN THIỆN ---
            total_overall_payment_vnd = sum(round_money(x['line_total'] or 0) for x in items_for_json)
            total_final_float = float(total_overall_payment_vnd)
            final_paid = total_final_float if payment_status_input == 'Đã thanh toán' else 0.0

            c.execute("UPDATE import SET total_value = ?, paid_amount = ? WHERE id = ?", (total_final_float, final_paid, import_id))

            items_json_str = json.dumps(items_for_json, ensure_ascii=False)
            c.execute("""
                INSERT INTO phieu_nhap_kho (import_no, date, bill_no, bill_date, supplier_name, supplier_id, items_json, total_amount, import_id, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (import_no, import_date, bill_no, bill_date, supplier_name, supplier_id, items_json_str, total_final_float, import_id, note))

            po_result = None
            if po_id:
                from Services.sme.purchase_order import apply_po_receipt
                receipt_lines = []
                for i in items:
                    receipt_lines.append({
                        'product_id': i.get('product_id'),
                        'product_name': i.get('name') or i.get('invoice_name'),
                        'qty': i.get('qty'),
                    })
                po_note = (note or '')
                if f'ĐĐH' not in po_note and data.get('po_no'):
                    # giữ note phiếu; apply_po_receipt gắn PNK#
                    pass
                po_result = apply_po_receipt(
                    conn, po_id, receipt_lines, import_id=import_id,
                )

            conn.commit()
            return jsonify({
                "success": True,
                "import_id": import_id,
                "voucher_no": import_no,
                "total_payment_vnd": total_final_float,
                "journal_entry_ids": accounting_tx_ids,
                "accounting_tx_ids": accounting_tx_ids,
                "purchase_order": po_result,
            })

        except Exception as e:
            conn.rollback()
            logging.error(f"LỖI HỆ THỐNG KẾ TOÁN VÀ GIÁ VỐN: {str(e)}", exc_info=True)
            return jsonify({"error": f"Lỗi xử lý: {str(e)}"}), 500
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Danh mục tài khoản SME (TT99) — tách biệt HKD
    # --------------------------------------------------------------------------
    @app.route('/SME_chart_of_accounts')
    @login_required
    @require_sme_regime
    def SME_chart_of_accounts():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on COA page')
        return render_template('KeToanSME/chart_of_accounts.html')

    @app.route('/SME_journal')
    @login_required
    @require_sme_regime
    def SME_journal():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on journal page')
        return render_template('KeToanSME/journal.html')

    @app.route('/SME_general_ledger')
    @login_required
    @require_sme_regime
    def SME_general_ledger():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on ledger page')
        return render_template('KeToanSME/general_ledger.html')

    @app.route('/api/sme/coa', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_coa_list():
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import account_tree, ensure_sme_coa_ready
            meta = ensure_sme_coa_ready(conn)
            q = (request.args.get('q') or '').strip() or None
            active = request.args.get('active', '1') != '0'
            if q:
                from Services.sme.coa_service import list_accounts
                rows = list_accounts(conn, active_only=active, q=q)
            else:
                rows = account_tree(conn, active_only=active)
            return jsonify({
                'success': True,
                'data': rows,
                'meta': meta,
            })
        except Exception as e:
            logger.exception('api_sme_coa_list')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/<code>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_coa_get(code):
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import get_account, list_children, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            acc = get_account(conn, code)
            if not acc:
                return jsonify({'success': False, 'error': 'Không tìm thấy tài khoản'}), 404
            return jsonify({
                'success': True,
                'data': acc,
                'children': list_children(conn, code),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/<parent_code>/suggest-child', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_coa_suggest_child(parent_code):
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import suggest_next_child_code, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            return jsonify({
                'success': True,
                'next_code': suggest_next_child_code(conn, parent_code),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/coa/children', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_coa_create_child():
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import create_child_account, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            created = create_child_account(
                conn,
                parent_code=(payload.get('parent_code') or '').strip(),
                code=(payload.get('code') or '').strip() or None,
                name=(payload.get('name') or '').strip(),
                custom_reason=(payload.get('custom_reason') or '').strip(),
                tracks=payload.get('tracks') or None,
                bctc_line_code=payload.get('bctc_line_code'),
                description=(payload.get('description') or '').strip(),
            )
            return jsonify({'success': True, 'data': created})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            logger.exception('api_sme_coa_create_child')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/<code>', methods=['PUT'])
    @login_required
    @require_sme_regime
    def api_sme_coa_update(code):
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import update_account_meta, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            updated = update_account_meta(
                conn,
                code,
                name=payload.get('name'),
                description=payload.get('description'),
                bctc_line_code=payload.get('bctc_line_code'),
                tracks=payload.get('tracks'),
            )
            return jsonify({'success': True, 'data': updated})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/<code>/deactivate', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_coa_deactivate(code):
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import deactivate_account, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            data = deactivate_account(conn, code)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/reseed', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_coa_reseed():
        """Nạp lại seed hệ thống (giữ tài khoản custom của DN)."""
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import ensure_sme_coa_ready
            meta = ensure_sme_coa_ready(conn, force_reseed=True)
            return jsonify({'success': True, 'meta': meta})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Nhật ký / bút toán SME
    # --------------------------------------------------------------------------
    @app.route('/api/sme/journal/ready', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_journal_ready():
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            meta = ensure_sme_accounting_ready(conn)
            return jsonify({'success': True, **meta})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/journal', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_journal_list():
        conn = get_db_connection()
        try:
            from Services.sme.journal_engine import list_journal_entries
            doc_type = (request.args.get('document_type') or '').strip() or None
            doc_id = request.args.get('document_id', type=int)
            status = (request.args.get('status') or '').strip() or None
            date_from = (request.args.get('date_from') or '').strip() or None
            date_to = (request.args.get('date_to') or '').strip() or None
            q = (request.args.get('q') or '').strip() or None
            limit = request.args.get('limit', default=50, type=int) or 50
            offset = request.args.get('offset', default=0, type=int) or 0
            rows = list_journal_entries(
                conn,
                document_type=doc_type,
                document_id=doc_id,
                status=status,
                date_from=date_from,
                date_to=date_to,
                q=q,
                limit=min(max(limit, 1), 200),
                offset=max(offset, 0),
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/journal/<int:entry_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_journal_get(entry_id):
        conn = get_db_connection()
        try:
            from Services.sme.journal_engine import get_journal_entry
            data = get_journal_entry(conn, entry_id)
            if not data:
                return jsonify({'success': False, 'error': 'Không tìm thấy bút toán'}), 404
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/journal/<int:entry_id>/reverse', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_journal_reverse(entry_id):
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.journal_engine import reverse_journal_entry
            rev = reverse_journal_entry(
                conn,
                entry_id,
                posting_date=(payload.get('posting_date') or None),
                created_by=(session.get('user') or {}).get('username') or session.get('user_name'),
                reason=(payload.get('reason') or 'Đảo bút toán'),
            )
            conn.commit()
            return jsonify({'success': True, 'data': rev})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/posting-rules', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_posting_rules():
        conn = get_db_connection()
        try:
            from Services.sme.journal_engine import ensure_sme_journal_ready
            ensure_sme_journal_ready(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM sme_posting_rules WHERE active = 1 ORDER BY business_type, payment_method"
            ).fetchall()
            return jsonify({'success': True, 'data': [dict(r) for r in rows]})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Sổ cái / cân đối phát sinh
    # --------------------------------------------------------------------------
    @app.route('/api/sme/trial-balance', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_trial_balance():
        conn = get_db_connection()
        try:
            from Services.sme.general_ledger import trial_balance
            year = request.args.get('year', type=int) or datetime.now().year
            period_from = request.args.get('period_from', type=int) or 1
            period_to = request.args.get('period_to', type=int) or period_from
            include_zero = request.args.get('include_zero', '0') in ('1', 'true', 'True')
            postable_only = request.args.get('postable_only', '1') not in ('0', 'false', 'False')
            data = trial_balance(
                conn,
                fiscal_year=year,
                period_from=period_from,
                period_to=period_to,
                postable_only=postable_only,
                include_zero=include_zero,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/ledger/<account_code>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_account_ledger(account_code):
        conn = get_db_connection()
        try:
            from Services.sme.general_ledger import account_ledger, period_bounds
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int)
            date_from = (request.args.get('date_from') or '').strip()
            date_to = (request.args.get('date_to') or '').strip()
            if not date_from or not date_to:
                if period:
                    date_from, date_to = period_bounds(year, period)
                else:
                    date_from, date_to = f'{year}-01-01', f'{year}-12-31'
            data = account_ledger(conn, account_code, date_from=date_from, date_to=date_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # BCTC (B01 / B02)
    # --------------------------------------------------------------------------
    @app.route('/api/sme/bctc/b01', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_b01():
        conn = get_db_connection()
        try:
            from Services.sme.bctc_report import balance_sheet
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            include_profit = request.args.get('include_current_profit', '1') not in ('0', 'false', 'False')
            data = balance_sheet(
                conn,
                fiscal_year=year,
                period_to=period_to,
                include_current_profit=include_profit,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/b02', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_b02():
        conn = get_db_connection()
        try:
            from Services.sme.bctc_report import income_statement
            year = request.args.get('year', type=int) or datetime.now().year
            period_from = request.args.get('period_from', type=int) or 1
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = income_statement(
                conn,
                fiscal_year=year,
                period_from=period_from,
                period_to=period_to,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/b03', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_b03():
        conn = get_db_connection()
        try:
            from Services.sme.bctc_report import cash_flow_statement
            year = request.args.get('year', type=int) or datetime.now().year
            period_from = request.args.get('period_from', type=int) or 1
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = cash_flow_statement(
                conn,
                fiscal_year=year,
                period_from=period_from,
                period_to=period_to,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/b09', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_b09():
        conn = get_db_connection()
        try:
            from Services.sme.b09_notes import notes_to_financial_statements
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = notes_to_financial_statements(
                conn,
                fiscal_year=year,
                period_to=period_to,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/SME_auto_posting')
    @login_required
    @require_sme_regime
    def SME_auto_posting():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on auto posting page')
        return render_template('KeToanSME/auto_posting.html')

    @app.route('/SME_tax_nsnn')
    @login_required
    @require_sme_regime
    def SME_tax_nsnn():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap tax page')
        return render_template('KeToanSME/tax_nsnn.html')

    @app.route('/SME_mgmt_report')
    @login_required
    @require_sme_regime
    def SME_mgmt_report():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/mgmt_report.html')

    @app.route('/SME_costing')
    @login_required
    @require_sme_regime
    def SME_costing():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/costing.html')

    @app.route('/SME_utilities')
    @login_required
    @require_sme_regime
    def SME_utilities():
        return render_template('KeToanSME/utilities.html')

    @app.route('/api/sme/dashboard-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_dashboard_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import dashboard_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = dashboard_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/tax-nsnn', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_tax_nsnn():
        conn = get_db_connection()
        try:
            from Services.sme.tax_nsnn import tax_nsnn_summary
            from Services.tenant_profile import get_current_tenant_profile, normalize_vat_filing_period
            profile = get_current_tenant_profile()
            features = profile.get('features') or {}
            default_mode = normalize_vat_filing_period(
                request.args.get('filing_mode')
                or profile.get('vat_filing_period')
                or features.get('vat_filing_period')
                or features.get('filing_period'),
                default='monthly' if features.get('monthly_vat_filing') else 'quarterly',
            )
            year = request.args.get('year', type=int) or datetime.now().year
            now = datetime.now()
            period = request.args.get('period', type=int)
            quarter = request.args.get('quarter', type=int)
            if default_mode == 'quarterly' and quarter is None and period is None:
                quarter = (now.month - 1) // 3 + 1
            elif default_mode == 'monthly' and period is None:
                period = now.month
            data = tax_nsnn_summary(
                conn,
                fiscal_year=year,
                period=period,
                quarter=quarter,
                filing_mode=default_mode,
            )
            data['configured_vat_filing_period'] = normalize_vat_filing_period(
                profile.get('vat_filing_period')
                or features.get('vat_filing_period')
                or features.get('filing_period'),
                default=default_mode,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/settings/vat-filing-period', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_vat_filing_period():
        from Services.tenant_profile import (
            get_current_tenant_profile,
            normalize_vat_filing_period,
            default_vat_filing_period_for_regime,
            update_registry_settings,
        )
        profile = get_current_tenant_profile()
        features = profile.get('features') or {}
        current = normalize_vat_filing_period(
            profile.get('vat_filing_period')
            or features.get('vat_filing_period')
            or features.get('filing_period'),
            default=default_vat_filing_period_for_regime(profile.get('accounting_regime')),
        )
        if request.method == 'GET':
            return jsonify({
                'success': True,
                'data': {
                    'vat_filing_period': current,
                    'monthly_vat_filing': current == 'monthly',
                    'accounting_regime': profile.get('accounting_regime'),
                    'default_for_regime': default_vat_filing_period_for_regime(
                        profile.get('accounting_regime'),
                    ),
                },
            })

        payload = request.get_json(silent=True) or {}
        period = normalize_vat_filing_period(
            payload.get('vat_filing_period') or payload.get('filing_period'),
            default=current,
        )
        tenant_id = (
            getattr(g, 'tenant_id', None)
            or session.get('last_tenant_id')
            or profile.get('tenant_id')
        )
        if not tenant_id:
            return jsonify({'success': False, 'error': 'Không xác định được tenant'}), 400
        ok = update_registry_settings(tenant_id, {
            'vat_filing_period': period,
            'filing_period': period,
        })
        if not ok:
            return jsonify({'success': False, 'error': 'Không lưu được cấu hình'}), 500
        try:
            from Services.tenant_profile import load_tenant_profile
            g.tenant_profile = load_tenant_profile(tenant_id)
        except Exception:
            pass
        return jsonify({
            'success': True,
            'data': {
                'vat_filing_period': period,
                'monthly_vat_filing': period == 'monthly',
                'message': 'Đã lưu kỳ kê khai GTGT',
            },
        })

    @app.route('/api/sme/mgmt-report', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_mgmt_report():
        conn = get_db_connection()
        try:
            from Services.sme.mgmt_report import management_report
            year = request.args.get('year', type=int) or datetime.now().year
            period_from = request.args.get('period_from', type=int) or 1
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = management_report(
                conn, fiscal_year=year, period_from=period_from, period_to=period_to,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_costing():
        conn = get_db_connection()
        try:
            from Services.sme.costing import costing_summary
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int) or datetime.now().month
            data = costing_summary(conn, fiscal_year=year, period=period)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/b09/narrative', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_b09_narrative_save():
        conn = get_db_connection()
        try:
            from Services.sme.b09_notes import save_b09_narrative_items
            payload = request.get_json(silent=True) or {}
            items = payload.get('items') or []
            n = save_b09_narrative_items(
                conn,
                items,
                updated_by=session.get('user_name') or session.get('username'),
            )
            conn.commit()
            return jsonify({'success': True, 'saved': n})
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_po_list():
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import list_purchase_orders
            rows = list_purchase_orders(
                conn,
                status=request.args.get('status') or None,
                keyword=request.args.get('keyword') or None,
                date_from=request.args.get('date_from') or None,
                date_to=request.args.get('date_to') or None,
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_po_create():
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import create_purchase_order
            payload = request.get_json(silent=True) or {}
            data = create_purchase_order(
                conn,
                po_date=payload.get('po_date') or datetime.now().strftime('%Y-%m-%d'),
                expected_date=payload.get('expected_date'),
                supplier_name=payload.get('supplier_name') or '',
                supplier_id=payload.get('supplier_id'),
                supplier_code=payload.get('supplier_code'),
                supplier_tax_code=payload.get('supplier_tax_code'),
                note=payload.get('note'),
                lines=payload.get('lines') or [],
                created_by=session.get('user_name') or session.get('username'),
            )
            conn.commit()
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders/<int:po_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_po_get(po_id):
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import get_purchase_order
            data = get_purchase_order(conn, po_id)
            if not data:
                return jsonify({'success': False, 'error': 'Không tìm thấy đơn'}), 404
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders/<int:po_id>', methods=['PUT'])
    @login_required
    @require_sme_regime
    def api_sme_po_update(po_id):
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import update_purchase_order
            payload = request.get_json(silent=True) or {}
            data = update_purchase_order(
                conn,
                po_id,
                po_date=payload.get('po_date'),
                expected_date=payload.get('expected_date'),
                supplier_name=payload.get('supplier_name'),
                supplier_id=payload.get('supplier_id'),
                supplier_code=payload.get('supplier_code'),
                supplier_tax_code=payload.get('supplier_tax_code'),
                note=payload.get('note'),
                status=payload.get('status'),
                lines=payload.get('lines'),
            )
            conn.commit()
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders/<int:po_id>/status', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_po_status(po_id):
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import set_purchase_order_status
            payload = request.get_json(silent=True) or {}
            data = set_purchase_order_status(conn, po_id, payload.get('status') or '')
            conn.commit()
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders/<int:po_id>/import-draft', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_po_import_draft(po_id):
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import build_import_draft_from_po
            data = build_import_draft_from_po(conn, po_id)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchasing-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_purchasing_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import purchasing_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = purchasing_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/debt-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_debt_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import debt_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = debt_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/warehouse-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_warehouse_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import warehouse_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = warehouse_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/fixed-asset-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_fixed_asset_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import fixed_asset_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = fixed_asset_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/tools-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_tools_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import tools_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = tools_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/hr-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_hr_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import hr_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = hr_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sales-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_sales_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import sales_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = sales_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/books-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_books_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import books_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = books_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import bctc_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = bctc_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/vat-declaration', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_vat_declaration():
        conn = get_db_connection()
        try:
            from Services.sme.vat_declaration import vat_declaration_worksheet
            from Services.tenant_profile import get_current_tenant_profile, normalize_vat_filing_period
            profile = get_current_tenant_profile()
            features = profile.get('features') or {}
            default_mode = normalize_vat_filing_period(
                request.args.get('filing_mode')
                or profile.get('vat_filing_period')
                or features.get('vat_filing_period')
                or features.get('filing_period'),
                default='monthly' if features.get('monthly_vat_filing') else 'quarterly',
            )
            year = request.args.get('year', type=int) or datetime.now().year
            now = datetime.now()
            period = request.args.get('period', type=int)
            quarter = request.args.get('quarter', type=int)
            if default_mode == 'quarterly' and quarter is None and period is None:
                quarter = (now.month - 1) // 3 + 1
            elif default_mode == 'monthly' and period is None:
                period = now.month
            data = vat_declaration_worksheet(
                conn,
                fiscal_year=year,
                period=period,
                quarter=quarter,
                filing_mode=default_mode,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/vat-declaration/xml', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_vat_declaration_xml():
        conn = get_db_connection()
        try:
            from Services.sme.vat_xml import generate_sme_vat_xml
            from Services.tenant_profile import get_current_tenant_profile, normalize_vat_filing_period
            profile = get_current_tenant_profile()
            features = profile.get('features') or {}
            default_mode = normalize_vat_filing_period(
                request.args.get('filing_mode')
                or profile.get('vat_filing_period')
                or features.get('vat_filing_period')
                or features.get('filing_period'),
                default='monthly' if features.get('monthly_vat_filing') else 'quarterly',
            )
            year = request.args.get('year', type=int) or datetime.now().year
            now = datetime.now()
            period = request.args.get('period', type=int)
            quarter = request.args.get('quarter', type=int)
            if default_mode == 'quarterly' and quarter is None and period is None:
                quarter = (now.month - 1) // 3 + 1
            elif default_mode == 'monthly' and period is None:
                period = now.month
            result = generate_sme_vat_xml(
                conn,
                fiscal_year=year,
                period=period,
                quarter=quarter,
                filing_mode=default_mode,
                loai_tkhai=request.args.get('loai_tkhai') or 'C',
                so_lan=request.args.get('so_lan') or '1',
            )
            return Response(
                result['xml'],
                mimetype='application/xml; charset=utf-8',
                headers={
                    'Content-Disposition': f'attachment; filename="{result["filename"]}"',
                },
            )
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sale-forms/customers', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_sale_form_customers():
        conn = get_db_connection()
        try:
            from Services.sme.sale_forms import list_sale_customers
            return jsonify({'success': True, 'data': list_sale_customers(conn)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sale-forms/products', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_sale_form_products():
        conn = get_db_connection()
        try:
            from Services.sme.sale_forms import list_products_brief
            return jsonify({'success': True, 'data': list_products_brief(conn)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sale-forms/01-bh', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_form_01_bh():
        conn = get_db_connection()
        try:
            from Services.sme.sale_forms import form_01_bh
            data = form_01_bh(
                conn,
                agent_name=request.args.get('agent_name') or '',
                date_from=request.args.get('date_from') or '',
                date_to=request.args.get('date_to') or '',
                contract_no=request.args.get('contract_no') or '',
                contract_date=request.args.get('contract_date') or '',
                opening_debt=float(request.args.get('opening_debt') or 0),
                commission=float(request.args.get('commission') or 0),
                tax_paid_for=float(request.args.get('tax_paid_for') or 0),
                other_cost=float(request.args.get('other_cost') or 0),
                paid_cash=float(request.args.get('paid_cash') or 0),
                paid_cheque=float(request.args.get('paid_cheque') or 0),
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sale-forms/02-bh', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_form_02_bh():
        conn = get_db_connection()
        try:
            from Services.sme.sale_forms import form_02_bh
            data = form_02_bh(
                conn,
                product_id=request.args.get('product_id', type=int) or 0,
                date_from=request.args.get('date_from') or '',
                date_to=request.args.get('date_to') or '',
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/employee-receivable', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_employee_receivable():
        conn = get_db_connection()
        try:
            from Services.sme.employee_receivable import employee_receivable_summary
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int) or datetime.now().month
            data = employee_receivable_summary(conn, fiscal_year=year, period=period)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/auto/run-period', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_auto_run_period():
        conn = get_db_connection()
        try:
            from Services.sme.auto_posting import run_period_automation
            from Services.tenant_profile import get_current_tenant_profile
            payload = request.get_json(silent=True) or {}
            now = datetime.now()
            year = int(payload.get('year') or now.year)
            period = int(payload.get('period') or now.month)
            replace_existing = bool(payload.get('replace_existing'))
            auto_activate = payload.get('auto_activate', True)
            profile = get_current_tenant_profile()
            result = run_period_automation(
                conn,
                fiscal_year=year,
                period=period,
                accounting_regime=profile.get('accounting_regime'),
                features=profile.get('features'),
                created_by=session.get('user_name') or session.get('username'),
                replace_existing=replace_existing,
                auto_activate=bool(auto_activate),
            )
            conn.commit()
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/auto/period-close', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_auto_period_close():
        """Chỉ chạy kết chuyển KQKD (không KH/PB)."""
        conn = get_db_connection()
        try:
            from Services.sme.period_close import run_period_close
            from Services.tenant_profile import get_current_tenant_profile
            payload = request.get_json(silent=True) or {}
            now = datetime.now()
            year = int(payload.get('year') or now.year)
            period = int(payload.get('period') or now.month)
            profile = get_current_tenant_profile()
            result = run_period_close(
                conn,
                fiscal_year=year,
                period=period,
                accounting_regime=profile.get('accounting_regime'),
                features=profile.get('features'),
                created_by=session.get('user_name') or session.get('username'),
                replace_existing=bool(payload.get('replace_existing')),
            )
            conn.commit()
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/auto/vat-settle', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_auto_vat_settle():
        conn = get_db_connection()
        try:
            from Services.sme.vat_settlement import run_vat_settlement
            from Services.tenant_profile import get_current_tenant_profile
            payload = request.get_json(silent=True) or {}
            now = datetime.now()
            year = int(payload.get('year') or now.year)
            period = int(payload.get('period') or now.month)
            profile = get_current_tenant_profile()
            result = run_vat_settlement(
                conn,
                fiscal_year=year,
                period=period,
                accounting_regime=profile.get('accounting_regime'),
                features=profile.get('features'),
                created_by=session.get('user_name') or session.get('username'),
                replace_existing=bool(payload.get('replace_existing')),
            )
            conn.commit()
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-lock', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_period_lock_list():
        conn = get_db_connection()
        try:
            from Services.sme.period_lock import list_locked_periods
            year = request.args.get('year', type=int)
            rows = list_locked_periods(conn, fiscal_year=year)
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-lock', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_period_lock_set():
        conn = get_db_connection()
        try:
            from Services.sme.period_lock import lock_period, unlock_period
            payload = request.get_json(silent=True) or {}
            year = int(payload.get('year') or datetime.now().year)
            period = int(payload.get('period') or datetime.now().month)
            action = (payload.get('action') or 'lock').strip().lower()
            if action == 'unlock':
                ok = unlock_period(conn, fiscal_year=year, period=period)
                conn.commit()
                return jsonify({'success': True, 'unlocked': ok, 'year': year, 'period': period})
            data = lock_period(
                conn,
                fiscal_year=year,
                period=period,
                locked_by=session.get('user_name') or session.get('username'),
                reason=payload.get('reason') or 'Khóa sổ thủ công',
            )
            conn.commit()
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
