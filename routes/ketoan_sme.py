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

    #============================================================================== Start of SME Accounting=========================================================================#
    @app.route('/SME_dashboard')
    def SME_dashboard():
        return render_template('KeToanSME/main_dashboard.html')

    @app.route('/SME_order')
    def SME_order():
        return render_template('KeToanSME/sme_order.html')

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
    def SME_purchase_order_create():
        return render_template('KeToanSME/purchase_order_create.html')

    @app.route('/SME_purchase_order_list')
    @login_required
    def SME_purchase_order_list():
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
    def SME_PhaiThuCongNhanVien():
        return render_template('KeToanSME/PhaiThuCongNhanVien.html')

    @app.route('/SME_PhaiTraCongNhanVien')
    @login_required
    def SME_PhaiTraCongNhanVien():
        return render_template('KeToanSME/PhaiTraCongNhanVien.html')

    @app.route('/SME_dashboard_HRSalary')
    @login_required
    def SME_dashboard_HRSalary():
        return render_template('KeToanSME/dashboard_HRSalary.html')

    @app.route('/SME_SoSachKeToan')
    @login_required
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
    def SME_BCTC():
        return render_template('KeToanSME/dashboard_BCTC.html')

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

            posting_dt = datetime.strptime(import_date, "%Y-%m-%d") if import_date else datetime.now()
            fiscal_year = posting_dt.year
            period = posting_dt.month
        
            accounting_tx_ids = []
            items_for_json = []

            # --- 6. DUYỆT TỪNG NHÓM NGHIỆP VỤ ĐỂ TIẾN HÀNH ĐỊNH KHOẢN ---
            for b_type, business_items in items_by_business.items():
                if not business_items:
                    continue
                
                # Lấy cấu hình quy tắc định khoản tự động
                c.execute("""
                    SELECT * FROM accounting_rule 
                    WHERE business_type = ? AND payment_method = ? AND active = 1
                """, (b_type, payment_method))
                acc_rule = c.fetchone()
            
                if not acc_rule:
                    raise ValueError(f"Chưa cấu hình tài khoản định khoản cho nghiệp vụ {b_type} qua {payment_method}")

                tx_uuid = str(uuid.uuid4())
                desc_text = "Nhập kho hàng hóa" if b_type == 'NHAP_KHO_HANG_HOA' else "Nhập kho nguyên vật liệu"
            
                # Ghi nhận chứng từ Kế toán Tổng hợp (Transaction)
                c.execute("""
                    INSERT INTO accounting_transaction (
                        transaction_uuid, company_id, fiscal_year, period, posting_date, document_date, 
                        document_type, document_no, document_id, business_type, event_type, 
                        currency, exchange_rate, description, reference_document, status
                    ) VALUES (?, 0, ?, ?, ?, ?, 'PNK', ?, ?, ?, 'RECEIVE_GOODS', ?, ?, ?, ?, 'Posted')
                """, (tx_uuid, fiscal_year, period, import_date, bill_date or import_date, import_no, import_id, b_type, currency, float(exchange_rate), f"{desc_text} theo HĐ/Tờ khai số {bill_no}", bill_no))
            
                accounting_tx_id = c.lastrowid
                accounting_tx_ids.append(accounting_tx_id)
            
                tx_subtotal_payable_vnd = Decimal('0.00')
                tx_total_vat_vnd = Decimal('0.00')
                tx_total_import_tax_vnd = Decimal('0.00')
                sequence = 1

                for item in business_items:
                    pid = item.get('product_id')
                    p_info = p_map.get(pid)
                    if not p_info: continue

                    qty_in = round_money(item.get('qty', 0))
                    if qty_in <= 0: continue

                    # Thông số tài chính gốc từ Client
                    price_original = round_money(item.get('buyprice', 0))
                    tax_p = Decimal(str(item.get('tax_pct', 0) or 0))
                    import_tax_p = Decimal(str(item.get('import_tax_pct', 0) or 0)) if import_type == 'IMPORT' else Decimal('0.00')
                    disc_p = Decimal(str(item.get('discount_pct', 0) or 0))
                    unit_in = str(item.get('unit', '')).strip().lower()

                    product_name = p_info['name']
                    retail_unit = str(p_info['unit'] or "Cái").strip()
                    wholesale_unit = str(p_info['unit1'] or "").strip().lower()
                    ratio = Decimal(str(p_info['unit_ratio'] or 1))

                    # Quy đổi đơn vị tính vật lý về đơn vị nhỏ nhất (Đơn vị lẻ)
                    is_wholesale = wholesale_unit and unit_in == wholesale_unit
                    qty_retail = qty_in * ratio if is_wholesale else qty_in

                    # --- ĐỒNG BỘ TÍNH TOÁN QUY ĐỔI GIÁ VỐN (VND) KHÔNG CHỨA THUẾ GTGT ---
                    price_vnd = round_money(price_original * exchange_rate)
                    line_subtotal_vnd = round_money(qty_in * price_vnd)
                    line_disc_vnd = round_money(line_subtotal_vnd * (disc_p / Decimal('100.00')))
                    line_net_vnd = line_subtotal_vnd - line_disc_vnd

                    # Tính Thuế Nhập Khẩu (Nếu có -> Cộng trực tiếp vào nguyên giá kho)
                    line_import_tax_vnd = Decimal('0.00')
                    if import_type == 'IMPORT' and import_tax_p > 0:
                        line_import_tax_vnd = round_money(line_net_vnd * (import_tax_p / Decimal('100.00')))
                        tx_total_import_tax_vnd += line_import_tax_vnd

                    # Phân bổ chi phí thu mua (Phí vận chuyển, bốc dỡ...)
                    line_extra_vnd = round_money((line_subtotal_vnd / total_base_safe) * extra_cost)

                    # NGUYÊN GIÁ NHẬP KHO CHUẨN (Không bao gồm Thuế GTGT)
                    line_inventory_value_vnd = line_net_vnd + line_import_tax_vnd + line_extra_vnd
                    cost_per_retail_vnd = round_money(line_inventory_value_vnd / qty_retail) if qty_retail > 0 else Decimal('0.00')

                    # Tính Thuế GTGT (Nếu là hàng nhập khẩu, thuế GTGT tính trên cả gốc + thuế NK)
                    tax_base_vnd = line_net_vnd + line_import_tax_vnd if import_type == 'IMPORT' else line_net_vnd
                    line_vat_vnd = round_money(tax_base_vnd * (tax_p / Decimal('100.00')))
                    tx_total_vat_vnd += line_vat_vnd

                    # Tổng giá trị thanh toán cuối của dòng hàng
                    line_total_payment_vnd = line_net_vnd + line_vat_vnd + line_extra_vnd
                    tx_subtotal_payable_vnd += line_total_payment_vnd

                    # Gom cấu trúc JSON để hiển thị phiếu in công khai
                    items_for_json.append({
                        "product_id": pid, "product_name": product_name, "barcode": p_info['barcode'] or "",
                        "unit": item.get('unit'), "qty": float(qty_in), "buyprice": float(price_original),
                        "discount_pct": float(disc_p), "tax_pct": float(tax_p), "import_tax_pct": float(import_tax_p),
                        "line_total": float(line_total_payment_vnd), "invoice_product_type": frontend_type
                    })

                    # Ghi nhận chi tiết chứng từ kho vật lý (Sử dụng giá vốn lẻ chuẩn không thuế)
                    params_detail = (
                        import_id, pid, float(qty_in), float(price_original), float(line_subtotal_vnd), 
                        float(line_disc_vnd), float(line_vat_vnd), float(cost_per_retail_vnd), 
                        1 if is_wholesale else 0, float(tax_p), float(disc_p)
                    )
                    c.execute("INSERT INTO import_details (import_id, product_id, qty, buyprice, subtotal, discount, tax, cost_price, unit_type, tax_pct, discount_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)", params_detail)
                    c.execute("INSERT INTO chi_tiet_phieu_nhap_kho (import_id, product_id, quantity, buyprice, subtotal, discount_amount, tax_amount, cost_price, unit_type, tax_pct, discount_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)", params_detail)

                    # --- HẠCH TOÁN NỢ TÀI KHOẢN KHO (156 / 152) VỚI NGUYÊN GIÁ KHÔNG THUẾ GTGT ---
                    c.execute("""
                        INSERT INTO accounting_transaction_detail (
                            transaction_id, sequence, account_code, debit, credit, currency, exchange_rate,
                            debit_fc, credit_fc, partner_id, warehouse_id, inventory_item_id, cost_price,
                            tax_code, tax_rate, vat_invoice_no, description
                        ) VALUES (?, ?, ?, ?, 0.00, 'VND', 1.000000, ?, 0.00, ?, 1, ?, ?, ?, ?, ?, ?)
                    """, (
                        accounting_tx_id, sequence, acc_rule['debit_account_code'], float(line_inventory_value_vnd),
                        float(line_inventory_value_vnd), supplier_id, pid, float(cost_per_retail_vnd),
                        tax_code, float(tax_p), bill_no, f"Nhập kho [{desc_text} - Giá vốn ko thuế]: {product_name}"
                    ))
                    sequence += 1

                    # CẬP NHẬT GIÁ VỐN BÌNH QUÂN GIA QUYỀN DI ĐỘNG KHO VẬT LÝ (AVG_COST) KHÔNG THUẾ GTGT
                    c.execute("SELECT quantity, avg_cost FROM inventory WHERE product_id = ?", (pid,))
                    inv = c.fetchone()
                    old_q = Decimal(str(inv['quantity'] if inv else 0))
                    old_c = Decimal(str(inv['avg_cost'] if inv else 0))
                    new_q = old_q + qty_retail
                    new_avg = round_money(((old_q * old_c) + line_inventory_value_vnd) / new_q) if new_q > 0 else cost_per_retail_vnd

                    c.execute("""
                        INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?, ?, ?)
                        ON CONFLICT(product_id) DO UPDATE SET quantity=excluded.quantity, avg_cost=excluded.avg_cost
                    """, (pid, float(new_q), float(new_avg)))

                    # Ghi nhận lịch sử dịch chuyển kho vật lý
                    move_note = f"Nhập kho từ {supplier_name} ({desc_text})"
                    c.execute("""
                        INSERT INTO stock_moves (product_id, date, type, ref_id, quantity, cost_price, note, ref_document, ref_type, type1, unit, unit1, unit_ratio)
                        VALUES (?, ?, 'import', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (pid, import_date, import_id, float(qty_retail), float(cost_per_retail_vnd), move_note, import_no, 'import', 'Nhập', retail_unit, wholesale_unit, float(ratio)))

                # --- HẠCH TOÁN THUẾ GTGT ĐẦU VÀO ĐƯỢC KHẤU TRỪ (TÀI KHOẢN 133) ---
                if acc_rule['is_vat_applicable'] and tx_total_vat_vnd > 0:
                    # Nếu là hàng nhập khẩu, tách riêng tiểu khoản thuế GTGT hàng nhập khẩu 13312 / 33312
                    acc_vat = '13312' if import_type == 'IMPORT' else acc_rule['vat_account_code']
                    c.execute("""
                        INSERT INTO accounting_transaction_detail (
                            transaction_id, sequence, account_code, debit, credit, currency, exchange_rate,
                            debit_fc, credit_fc, partner_id, tax_code, vat_invoice_no, description
                        ) VALUES (?, ?, ?, ?, 0.00, 'VND', 1.000000, ?, 0.00, ?, ?, ?, ?)
                    """, (
                        accounting_tx_id, sequence, acc_vat, float(tx_total_vat_vnd),
                        float(tx_total_vat_vnd), supplier_id, tax_code, bill_no, f"Thuế GTGT hàng mua của nghiệp vụ {b_type}"
                    ))
                    sequence += 1

                # --- HẠCH TOÁN THUẾ NHẬP KHẨU PHẢI NỘP NHÀ NƯỚC (3333) NẾU CÓ ---
                if import_type == 'IMPORT' and tx_total_import_tax_vnd > 0:
                    c.execute("""
                        INSERT INTO accounting_transaction_detail (
                            transaction_id, sequence, account_code, debit, credit, currency, exchange_rate,
                            debit_fc, credit_fc, partner_id, description
                        ) VALUES (?, ?, '3333', 0.00, ?, 'VND', 1.000000, 0.00, ?, ?, ?)
                    """, (
                        accounting_tx_id, sequence, float(tx_total_import_tax_vnd),
                        float(tx_total_import_tax_vnd), supplier_id, f"Thuế nhập khẩu phải nộp cấu thành nguyên giá"
                    ))
                    sequence += 1

                # --- HẠCH TOÁN BÚT TOÁN CÓ ĐỐI ỨNG (1111 / 1121 / 331) ---
                c.execute("""
                    INSERT INTO accounting_transaction_detail (
                        transaction_id, sequence, account_code, debit, credit, currency, exchange_rate,
                        debit_fc, credit_fc, partner_id, description
                    ) VALUES (?, ?, ?, 0.00, ?, 'VND', 1.000000, 0.00, ?, ?, ?)
                """, (
                    accounting_tx_id, sequence, acc_rule['credit_account_code'], float(tx_subtotal_payable_vnd),
                    float(tx_subtotal_payable_vnd), supplier_id, f"Thanh toán đối ứng tổng hợp nghiệp vụ {b_type}"
                ))

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

            conn.commit()
            return jsonify({
                "success": True, 
                "import_id": import_id, 
                "voucher_no": import_no, 
                "total_payment_vnd": total_final_float,
                "accounting_tx_ids": accounting_tx_ids
            })

        except Exception as e:
            conn.rollback()
            logging.error(f"LỖI HỆ THỐNG KẾ TOÁN VÀ GIÁ VỐN: {str(e)}", exc_info=True)
            return jsonify({"error": f"Lỗi xử lý: {str(e)}"}), 500
        finally:
            conn.close()
