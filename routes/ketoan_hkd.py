"""Routes kế toán HKD (phiếu thu/chi, sổ sách, TSCD…) — tách từ app.py."""
import calendar
import json
import logging
import re
import sqlite3
import traceback
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from flask import (
    Response,
    abort,
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
from flask_login import login_required, current_user

from Services.invoice_buyer import DEFAULT_RETAIL_BUYER_NAME
from Services.profit_report_helpers import compute_cogs, get_days_in_quarter
from db_utils import get_db_connection

logger = logging.getLogger(__name__)
import openpyxl
from openpyxl.styles import Alignment, Border, Font, Side



def register_ketoan_hkd_routes(app):
    """Đăng ký route kế toán HKD (giữ nguyên URL/endpoint)."""
    from auth import admin_or_master_required
    from helpers import parse_date, so_thanh_chu

    @app.context_processor
    def inject_hkd_hub():
        from Services.hkd_menu import (
            HUB_TITLE,
            get_hkd_menu_groups,
            get_hub_group_cards,
            get_hub_dashboard_quick_links,
            get_hub_dashboard_featured_links,
            get_hub_dashboard_soso_links,
            is_hub_endpoint,
            user_can_access_hub,
            user_can_see_sme_nav,
        )
        from Services.tenant_profile import is_sme_regime
        user = session.get('user') or {}
        cu = {
            'role': user.get('role', 'guest'),
            'permissions': user.get('permissions', ''),
        }
        tenant_profile = getattr(g, 'tenant_profile', None) or {}
        hub_current_group = None
        if request.endpoint == 'HKD_hub_group':
            hub_current_group = request.view_args.get('group_id') if request.view_args else None
        sme_tenant = is_sme_regime(tenant_profile.get('accounting_regime'))
        return {
            'hkd_menu_groups': get_hkd_menu_groups(cu, tenant_profile),
            'hub_group_cards': get_hub_group_cards(cu, tenant_profile),
            'hub_dashboard_quick': get_hub_dashboard_quick_links(cu, tenant_profile),
            'hub_dashboard_featured': get_hub_dashboard_featured_links(cu, tenant_profile),
            'hub_dashboard_soso': get_hub_dashboard_soso_links(cu, tenant_profile),
            'hub_title': HUB_TITLE,
            'hub_active': is_hub_endpoint(request.endpoint),
            'hub_current_group': hub_current_group,
            'user_has_hub': user_can_access_hub(cu, tenant_profile),
            'user_has_sme_nav': user_can_see_sme_nav(cu, tenant_profile),
            'tenant_is_sme': sme_tenant,
            'hkd_active': is_hub_endpoint(request.endpoint),
        }

    @app.route('/HKD_dashboard')
    def HKD_dashboard():
        from Services.tenant_profile import get_current_tenant_profile, is_sme_regime, is_master_session
        profile = get_current_tenant_profile()
        if is_sme_regime(profile.get('accounting_regime')) and not is_master_session():
            flash('Tenant đang dùng chế độ Kế toán Doanh nghiệp (SME). Chuyển sang dashboard SME.', 'info')
            return redirect(url_for('SME_dashboard'))
        return render_template('KeToanHKD/main_dashboard.html')

    @app.route('/api/hkd/revenue-tier-warning', methods=['GET'])
    @login_required
    def api_hkd_revenue_tier_warning():
        from Services.tenant_profile import check_revenue_tier_drift, get_current_tenant_profile

        profile = get_current_tenant_profile()
        year = request.args.get('year', type=int)
        conn = get_db_connection()
        try:
            warning = check_revenue_tier_drift(conn.cursor(), profile, year=year)
            return jsonify({'success': True, 'warning': warning})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/hkd/dashboard-metrics', methods=['GET'])
    @login_required
    def api_hkd_dashboard_metrics():
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            from Services.hkd_dashboard_metrics import fetch_hkd_dashboard_metrics
            from Services.hkd_hub_group_metrics import fetch_main_dashboard_charts
            metrics = fetch_hkd_dashboard_metrics(c, year)
            charts = fetch_main_dashboard_charts(c, year)
            conn.close()
            return jsonify({'success': True, **metrics, 'charts': charts})
        except Exception as e:
            logger.error('api_hkd_dashboard_metrics: %s', e, exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/hkd/hub-group-metrics', methods=['GET'])
    @login_required
    def api_hkd_hub_group_metrics():
        from Services.hkd_menu import get_hub_group_by_id
        from Services.hkd_hub_group_metrics import fetch_hub_group_metrics

        group_id = request.args.get('group_id', type=str)
        if not group_id:
            return jsonify({'success': False, 'error': 'Thiếu group_id'}), 400

        user = session.get('user') or {}
        cu = {
            'role': user.get('role', 'guest'),
            'permissions': user.get('permissions', ''),
        }
        tenant_profile = getattr(g, 'tenant_profile', None) or {}
        group = get_hub_group_by_id(group_id, cu, tenant_profile)
        if not group:
            return jsonify({'success': False, 'error': 'Không tìm thấy nhóm menu'}), 404

        year = request.args.get('year', type=int) or datetime.now().year
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            metrics = fetch_hub_group_metrics(conn.cursor(), group, year)
            conn.close()
            return jsonify({'success': True, **metrics})
        except Exception as e:
            logger.error('api_hkd_hub_group_metrics: %s', e, exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/HKD_hub/<group_id>')
    def HKD_hub_group(group_id):
        from Services.hkd_menu import get_hub_group_by_id
        user = session.get('user') or {}
        cu = {
            'role': user.get('role', 'guest'),
            'permissions': user.get('permissions', ''),
        }
        tenant_profile = getattr(g, 'tenant_profile', None) or {}
        group = get_hub_group_by_id(group_id, cu, tenant_profile)
        if not group:
            abort(404)
        return render_template('KeToanHKD/group_dashboard.html', group=group, group_id=group_id)

    @app.route('/hkd_accounting')
    @login_required
    @admin_or_master_required
    def hkd_accounting():
        return redirect(url_for('HKD_dashboard'))
    @app.route('/DanhSachPhieuThu')
    def DanhSachPhieuThu():
        return render_template('KeToanHKD/DanhSachPhieuThu.html')

    #====API LẤY DANH SÁCH PHIẾU THU===#
    @app.route('/api/receipt', methods=['GET'])
    def api_get_receipts():
        start_str = request.args.get('start')
        end_str = request.args.get('end')
        q = request.args.get('q', '')

        # Sửa SQL: Dùng hàm DATE() để chỉ so sánh phần Ngày, bỏ qua phần Giờ
        sql = """
            SELECT 
                pt.id, pt.voucher_no, pt.payer_name, pt.address, pt.tax_code, 
                pt.amount, pt.debit_account, pt.credit_account, pt.reason, 
                pt.reference_document, pt.date, pt.sale_id,
                s.company_name AS company_name
            FROM phieu_thu pt
            LEFT JOIN sale s ON pt.sale_id = s.id
            WHERE 1=1
        """
        params = []

        if start_str:
            # Ép pt.date về định dạng ngày YYYY-MM-DD để so sánh chính xác
            sql += " AND DATE(pt.date) >= DATE(?)"
            params.append(start_str) 

        if end_str:
            # Ép pt.date về định dạng ngày YYYY-MM-DD để bao gồm cả ngày cuối cùng
            sql += " AND DATE(pt.date) <= DATE(?)"
            params.append(end_str)

        if q:
            sql += """ AND (pt.payer_name LIKE ? OR pt.voucher_no LIKE ? 
                      OR pt.reason LIKE ? OR s.company_name LIKE ?) """
            search = f"%{q}%"
            params.extend([search, search, search, search])

        sql += " ORDER BY pt.date DESC, pt.id DESC"

        db = get_db_connection()
        cursor = db.cursor()
        cursor.execute(sql, params)
    
        results = [dict(row) for row in cursor.fetchall()]

        for item in results:
            # Xử lý số tiền
            item['amount'] = int(round(float(item.get('amount') or 0)))
        
            # Xử lý ngày tháng trả về cho Frontend
            if item['date']:
                if isinstance(item['date'], str):
                    item['date'] = item['date'].replace(' ', 'T')
                elif hasattr(item['date'], 'isoformat'):
                    item['date'] = item['date'].isoformat()
        
            # Xử lý các giá trị mặc định
            item['voucher_no'] = item.get('voucher_no') or f"PT{str(item['id']).zfill(6)}"
            item['payer_name'] = item.get('payer_name') or 'Bán cho người tiêu dùng'
            item['company_name'] = item.get('company_name') or '' 
            item['reason'] = item.get('reason') or 'Thu tiền bán hàng'
        
        return jsonify(results)

    # In phiếu thu
    @app.route('/PhieuThu/in/<int:receipt_id>')
    def phieuthu_print(receipt_id):
        """
        In phiếu thu - Join với bảng sale để lấy company_name
        """
        # Kết nối database
        db = get_db_connection()
        db.row_factory = sqlite3.Row # Đảm bảo truy vấn trả về row có thể truy cập bằng tên cột
        cursor = db.cursor()

        # Truy vấn JOIN với bảng sale (s) thông qua sale_id
        cursor.execute("""
            SELECT
                pt.id,
                pt.voucher_no,
                pt.payer_name,
                COALESCE(s.address, pt.address) AS address,
                pt.tax_code,
                pt.amount,
                pt.debit_account,
                pt.credit_account,
                pt.reason,
                pt.reference_document,
                pt.date,
                s.company_name AS company_name
            FROM phieu_thu pt
            LEFT JOIN sale s ON pt.sale_id = s.id
            WHERE pt.id = ?
        """, (receipt_id,))

        row = cursor.fetchone()

        if not row:
            abort(404, description="Không tìm thấy phiếu thu")

        # Chuyển thành dict
        receipt = dict(row)

        # Xử lý ngày tháng an toàn
        date_obj = receipt.get('date')
        if date_obj:
            if isinstance(date_obj, str):
                try:
                    # Xử lý chuỗi date (YYYY-MM-DD hoặc YYYY-MM-DD HH:MM:SS)
                    date_str = date_obj.replace(' ', 'T')
                    dt = datetime.fromisoformat(date_str)
                    receipt['formatted_date'] = dt.strftime('%d/%m/%Y %H:%M')
                except ValueError:
                    receipt['formatted_date'] = date_obj
            elif hasattr(date_obj, 'strftime'):
                receipt['formatted_date'] = date_obj.strftime('%d/%m/%Y %H:%M')
            else:
                receipt['formatted_date'] = ''
        else:
            receipt['formatted_date'] = ''

        # Format số tiền (VND)
        amount = float(receipt.get('amount') or 0)
        receipt['formatted_amount'] = "{:,.0f}".format(round(amount))
    
        # Đọc số thành chữ (Nếu bạn có hàm này thì bổ sung vào đây)
        # receipt['amount_in_words'] = doc_so_tien(amount)

        # Số phiếu hiển thị đẹp
        receipt['voucher_display'] = receipt['voucher_no'] or f"PT{str(receipt_id).zfill(6)}"
    
        # Đảm bảo company_name không bị None khi ra template
        receipt['company_name'] = receipt.get('company_name') or ''

        return render_template(
            'KeToanHKD/PhieuThu_print.html',
            receipt=receipt
        )

    #=== API TẠO PHIẾU THU CHO MODAL PHIẾU THU===#
    @app.route('/api/receipt/create', methods=['POST'])
    def create_receipt():
        db = get_db_connection()
        cursor = db.cursor()
    
        try:
            data = request.get_json(silent=True)
            if not data:
                return jsonify({"success": False, "error": "Dữ liệu không hợp lệ"}), 400

            # Lấy dữ liệu từ frontend
            date_input = data.get('date')
            payer_name = data.get('payer_name')
            address = data.get('address', '').strip()
            tax_code = data.get('tax_code', '').strip()
            amount_str = data.get('amount')
            debit_account = data.get('debit_account')
            credit_account = data.get('credit_account')
            reason = data.get('reason')
            reference_document = data.get('reference_document', '').strip() # Đây là sale_no

            # Kiểm tra bắt buộc
            if not all([date_input, payer_name, amount_str, debit_account, credit_account, reason]):
                return jsonify({"success": False, "error": "Vui lòng điền đầy đủ thông tin"}), 400

            # Validate số tiền
            try:
                amount = float(amount_str)
                if amount <= 0:
                    return jsonify({"success": False, "error": "Số tiền phải lớn hơn 0"}), 400
            except:
                return jsonify({"success": False, "error": "Số tiền không hợp lệ"}), 400

            # 1. Tìm sale_id từ sale_no để lưu vào bảng phieu_thu
            sale_id = None
            if reference_document:
                cursor.execute("SELECT id FROM sale WHERE sale_no = ?", [reference_document])
                sale_row = cursor.fetchone()
                if sale_row:
                    sale_id = sale_row[0]

            # 2. Tạo số phiếu thu tự động PTxxxxxx
            cursor.execute("SELECT voucher_no FROM phieu_thu WHERE voucher_no LIKE 'PT%' ORDER BY voucher_no DESC LIMIT 1")
            last_record = cursor.fetchone()
            new_number = 1
            if last_record and last_record[0]:
                try:
                    new_number = int(last_record[0][2:]) + 1
                except: pass
            new_voucher_no = f"PT{new_number:06d}"

            # 3. Insert vào bảng phieu_thu (Đã fix lỗi thiếu dấu phẩy và khớp tham số)
            sql_insert = """
                INSERT INTO phieu_thu 
                    (voucher_no, payer_name, address, tax_code, amount, 
                     debit_account, credit_account, reason, reference_document, 
                     date, sale_id) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                new_voucher_no,
                payer_name.strip(),
                address,
                tax_code,
                amount,
                debit_account,
                credit_account,
                reason.strip(),
                reference_document, # sale_no
                date_input,
                sale_id
            )
            cursor.execute(sql_insert, params)
            new_id = cursor.lastrowid

            # 4. Cập nhật bảng cong_no
            if reference_document:
                # Cập nhật số tiền đã trả, ngày trả và số phiếu tham chiếu
                cursor.execute("""
                    UPDATE cong_no 
                    SET paid_amount = paid_amount + ?,
                        reference_document = ?,
                        paid_date = ?
                    WHERE sale_no = ?
                """, [amount, new_voucher_no, date_input, reference_document])

            db.commit()
            return jsonify({"success": True, "id": new_id, "voucher_no": new_voucher_no}), 201

        except Exception as e:
            db.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            db.close()

    #=== API LẤY DỮ LIỆU PHIẾU THU ĐỂ SỬA===#
    @app.route('/api/receipt/<int:id>', methods=['GET'])
    def get_receipt_detail(id):
        db = get_db_connection()
    
        row = db.execute("""
            SELECT
                id, voucher_no, date, payer_name, address, tax_code,
                amount, debit_account, credit_account, reason,
                reference_document
            FROM phieu_thu
            WHERE id = ?
        """, (id,)).fetchone()
    
        if not row:
            return jsonify({
                "success": False,
                "error": "Không tìm thấy phiếu thu"
            }), 404
    
        # Chuyển tuple thành dict để jsonify an toàn
        return jsonify(dict(row))

    #=== SỬA VÀ CẬP NHẬT THÔNG TIN PHIẾU THU===#
    @app.route('/api/receipt/<int:id>', methods=['PUT'])
    def update_receipt(id):
        db = get_db_connection()
        cursor = db.cursor()
    
        try:
            data = request.get_json(silent=True)
            if not data:
                return jsonify({"success": False, "error": "Dữ liệu không hợp lệ"}), 400

            required = ['date', 'payer_name', 'amount', 'debit_account', 'credit_account', 'reason']
            missing = [f for f in required if not data.get(f)]
            if missing:
                return jsonify({
                    "success": False,
                    "error": f"Thiếu các trường bắt buộc: {', '.join(missing)}"
                }), 400

            # ========================================================
            # VALIDATE VÀ GIỮ NGUYÊN SỐ LẺ
            # ========================================================
            try:
                # Chuyển đổi sang float để giữ số lẻ (ví dụ: 1000.50)
                amount = float(data['amount'])
                if amount <= 0:
                    return jsonify({"success": False, "error": "Số tiền phải lớn hơn 0"}), 400
            
                # Nếu bạn muốn giới hạn tối đa 2 hoặc 3 chữ số thập phân để tránh sai số máy tính:
                # amount = round(amount, 2) 
            
            except (ValueError, TypeError):
                return jsonify({"success": False, "error": "Số tiền không hợp lệ"}), 400

            # Validate tài khoản kế toán
            if data['debit_account'] not in ['111', '112']:
                return jsonify({
                    "success": False,
                    "error": "Tài khoản Nợ phải là 111 hoặc 112"
                }), 400

            # Cập nhật thêm các TK Có phổ biến nếu cần (ví dụ thêm 511)
            valid_credit_accounts = ['131', '138', '156', '411', '511']
            if data['credit_account'] not in valid_credit_accounts:
                return jsonify({
                    "success": False,
                    "error": f"Tài khoản Có không hợp lệ. Cho phép: {', '.join(valid_credit_accounts)}"
                }), 400

            # Kiểm tra tồn tại phiếu thu
            exists = cursor.execute(
                "SELECT 1 FROM phieu_thu WHERE id = ?",
                (id,)
            ).fetchone()

            if not exists:
                return jsonify({"success": False, "error": "Không tìm thấy phiếu thu"}), 404

            # Cập nhật thông tin vào Database
            cursor.execute("""
                UPDATE phieu_thu
                SET
                    date = ?,
                    payer_name = ?,
                    address = ?,
                    tax_code = ?,
                    amount = ?,
                    debit_account = ?,
                    credit_account = ?,
                    reason = ?,
                    reference_document = ?
                WHERE id = ?
            """, (
                data['date'],
                data['payer_name'].strip(),
                data.get('address', '').strip(),
                data.get('tax_code', '').strip(),
                amount,  # Giá trị float đã bao gồm số lẻ
                data['debit_account'],
                data['credit_account'],
                data['reason'].strip(),
                data.get('reference_document', '').strip(),
                id
            ))

            db.commit()

            return jsonify({
                "success": True,
                "message": "Đã cập nhật phiếu thu thành công",
                "id": id,
                "amount_updated": amount # Trả về để frontend kiểm tra
            })

        except Exception as e:
            db.rollback()
            return jsonify({"success": False, "error": str(e)}), 500

    #=== XÓA PHIẾU THU====#
    @app.route('/api/receipt/delete/<int:id>', methods=['DELETE'])
    def delete_receipt(id):
        db = get_db_connection()
        cursor = db.cursor()
    
        try:
            # Kiểm tra tồn tại phiếu và credit_account hợp lệ
            cursor.execute("""
                SELECT credit_account 
                FROM phieu_thu 
                WHERE id = ?
            """, (id,))
        
            row = cursor.fetchone()
        
            if not row:
                return jsonify({
                    "success": False,
                    "error": "Không tìm thấy phiếu thu"
                }), 404
        
            credit_account = row[0]
        
            # Chỉ cho phép xóa nếu credit_account thuộc {131, 138, 411}
            if credit_account not in ['131', '138', '411']:
                return jsonify({
                    "success": False,
                    "error": "Chỉ được phép xóa phiếu thu có tài khoản Có (credit_account) là 131, 138 hoặc 411"
                }), 403  # 403 Forbidden - không có quyền xóa loại phiếu này
        
            # Thực hiện xóa (hard delete)
            cursor.execute("DELETE FROM phieu_thu WHERE id = ?", (id,))
        
            # Reset sequence (tùy chọn - nếu bạn muốn ID bắt đầu lại từ 1 sau khi xóa)
            # Nếu không muốn reset sequence thì comment 2 dòng dưới
            cursor.execute("""
                DELETE FROM sqlite_sequence 
                WHERE name = 'phieu_thu'
            """)
        
            db.commit()
        
            return jsonify({
                "success": True,
                "message": "Đã xóa phiếu thu thành công"
            })
    
        except Exception as e:
            db.rollback()
            return jsonify({
                "success": False,
                "error": f"Lỗi khi xóa phiếu thu: {str(e)}"
            }), 500

    #=== API ĐÁNH SỐ LẠI PHIẾU THU====#
    @app.route('/api/receipt/renumber', methods=['POST'])
    def renumber_receipts():
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # BƯỚC 1: Xóa voucher_no cũ để tránh lỗi UNIQUE (nếu có constraint)
            cursor.execute("UPDATE phieu_thu SET voucher_no = NULL")

            # BƯỚC 2: Lấy danh sách phiếu thu theo thứ tự thời gian
            # Ưu tiên: ngày thu (date) → id nhỏ trước (lập sớm hơn)
            cursor.execute("""
                SELECT id
                FROM phieu_thu
                ORDER BY date ASC, id ASC
            """)
            rows = cursor.fetchall()

            if not rows:
                conn.close()
                return jsonify({
                    'success': False,
                    'message': 'Không có phiếu thu nào để đánh lại số'
                })

            # BƯỚC 3: Đánh số mới từ PT000001
            for index, row in enumerate(rows, start=1):
                new_voucher_no = f"PT{str(index).zfill(6)}"
                cursor.execute("""
                    UPDATE phieu_thu
                    SET voucher_no = ?
                    WHERE id = ?
                """, (new_voucher_no, row['id']))

            conn.commit()
            count = len(rows)
        
            conn.close()

            return jsonify({
                'success': True,
                'count': count,
                'message': 'Đã đánh lại số phiếu thu thành công theo thứ tự ngày thu!'
            })

        except Exception as e:
            if 'conn' in locals():
                conn.rollback()
                conn.close()
            return jsonify({
                'success': False,
                'message': f'Lỗi: {str(e)}'
            }), 500

    # *** PHẦN PHIẾU CHI *** #
    # --- Route 1: Trang danh sách Phiếu Chi ---
    @app.route('/DanhSachPhieuChi')
    def DanhSachPhieuChi():
        return render_template('KeToanHKD/DanhSachPhieuChi.html')

    # --- Route 2: API lấy dữ liệu Phiếu Chi (Dùng cho AJAX) ---#
    @app.route('/api/expense', methods=['GET'])
    def api_get_expenses():
        start_str = request.args.get('start')
        end_str   = request.args.get('end')
        q = request.args.get('q', '')
        source_type = request.args.get('type', 'all')

        # FIX: Thêm ràng buộc pc.source_type = 'return_sale' vào điều kiện JOIN
        # Sử dụng alias rõ ràng để tránh xung đột
        sql = """
            SELECT 
                pc.*, 
                s.company_name AS sale_company_name
            FROM phieu_chi pc
            LEFT JOIN sale s ON pc.source_id = s.id AND pc.source_type = 'return_sale'
            WHERE 1=1
        """
        params = []
        tz_vn = ZoneInfo("Asia/Ho_Chi_Minh")

        # Lọc theo ngày
        if start_str:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=tz_vn)
            start_utc = start_dt.astimezone(ZoneInfo("UTC"))
            sql += " AND pc.date >= ?"
            params.append(start_utc)

        if end_str:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=tz_vn)
            end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            end_utc = end_dt.astimezone(ZoneInfo("UTC"))
            sql += " AND pc.date <= ?"
            params.append(end_utc)

        # Lọc theo loại nguồn
        if source_type != 'all':
            sql += " AND pc.source_type = ?"
            params.append(source_type)

        # Tìm kiếm (bao gồm cả tên công ty lấy từ bảng sale)
        if q:
            sql += """ AND (
                pc.receiver_name LIKE ? 
                OR pc.voucher_no LIKE ? 
                OR pc.reason LIKE ? 
                OR s.company_name LIKE ?
            )"""
            search = f"%{q}%"
            params.extend([search, search, search, search])

        sql += " ORDER BY pc.date DESC, pc.id DESC"

        db = get_db_connection()
        cursor = db.cursor()
        cursor.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]

        # Hậu xử lý dữ liệu
        for item in results:
            # FIX: Ưu tiên lấy company_name từ kết quả JOIN (return_sale)
            # Nếu không có, lấy trường company_name mặc định trong pc (nếu có) hoặc để rỗng
            sale_company = item.get('sale_company_name')
            pc_company = item.get('company_name')
            item['company_name'] = sale_company or pc_company or ''
        
            # Làm tròn VND
            amount_val = item.get('amount') or 0
            item['amount'] = int(round(float(amount_val)))
            item['formatted_amount'] = "{:,.0f}".format(item['amount']).replace(',', '.')

            # Định dạng ngày ISO
            if item.get('date'):
                if not isinstance(item['date'], str) and hasattr(item['date'], 'isoformat'):
                    item['date'] = item['date'].isoformat()
                elif isinstance(item['date'], str):
                    item['date'] = item['date'].replace(' ', 'T')

            # Giá trị mặc định
            item['receiver_name'] = item.get('receiver_name') or 'Người nhận'
            item['reason'] = item.get('reason') or ''

        return jsonify(results)

    #=== LẤY CHI TIẾT ĐỂ SỬA VÀ CẬP NHẬT PHIẾU CHI===#
    @app.route('/api/expense/<int:expense_id>', methods=['GET', 'PUT'])
    def expense_detail(expense_id):
        db = get_db_connection()
        cursor = db.cursor()

        # --- PHƯƠNG THỨC GET: LẤY CHI TIẾT ---
        if request.method == 'GET':
            cursor.execute("""
                SELECT id, voucher_no, date, receiver_name, address, amount, 
                       credit_account, reason, source_type, expense_type
                FROM phieu_chi 
                WHERE id = ?
            """, (expense_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"success": False, "error": "Không tìm thấy phiếu chi"}), 404
            return jsonify(dict(row))

        # --- PHƯƠNG THỨC PUT: CẬP NHẬT VÀ KIỂM TRA QUỸ ---
        elif request.method == 'PUT':
            data = request.get_json()
            if not data:
                return jsonify({"success": False, "error": "Dữ liệu không hợp lệ"}), 400

            try:
                # 1. Lấy thông tin hiện tại của phiếu trước khi sửa
                cursor.execute("SELECT amount, credit_account, date FROM phieu_chi WHERE id = ?", (expense_id,))
                old_row = cursor.fetchone()
                if not old_row:
                    return jsonify({"success": False, "error": "Phiếu không tồn tại"}), 404
            
                old_amount = float(old_row['amount'])
                old_account = old_row['credit_account']
                old_date = old_row['date']

                # 2. Chuẩn bị dữ liệu mới (nếu không gửi thì giữ nguyên cũ)
                new_amount = float(data.get('amount', old_amount))
                new_date = data.get('date', old_date)
                new_account = old_account
                if 'payment_method' in data:
                    new_account = '111' if data['payment_method'] == 'cash' else '112'

                # 3. LOGIC KIỂM TRA SỐ DƯ TƯƠNG LAI (Window Function)
                # Chúng ta sẽ kiểm tra tài khoản bị tác động (Tài khoản mới)
                # Ngày bắt đầu kiểm tra là ngày nhỏ nhất giữa ngày cũ và ngày mới
                start_check_date = min(old_date, new_date)

                # Câu lệnh SQL này sẽ:
                # - Hợp nhất Thu và Chi (Trừ phiếu hiện tại ra để tính số dư nền)
                # - Tính lũy kế (Running Balance)
                # - Cộng thêm biến động của phiếu đang sửa (new_amount)
            
                sql_check = f"""
                    WITH Biendong AS (
                        -- Lấy số dư đầu kỳ trước ngày start_check_date
                        SELECT NULL as d, (
                            (SELECT COALESCE(SUM(amount), 0) FROM phieu_thu WHERE debit_account LIKE '{new_account}%' AND date < ?) -
                            (SELECT COALESCE(SUM(amount), 0) FROM phieu_chi WHERE credit_account LIKE '{new_account}%' AND date < ? AND id != ?)
                        ) as amt
                    
                        UNION ALL
                    
                        -- Các khoản thu từ ngày start_check_date
                        SELECT date, amount FROM phieu_thu WHERE debit_account LIKE '{new_account}%' AND date >= ?
                    
                        UNION ALL
                    
                        -- Các khoản chi từ ngày start_check_date (trừ phiếu đang sửa)
                        SELECT date, -amount FROM phieu_chi WHERE credit_account LIKE '{new_account}%' AND date >= ? AND id != ?
                    
                        UNION ALL
                    
                        -- Chính phiếu này với giá trị mới
                        SELECT ? as date, -? as amount
                    ),
                    LuyKe AS (
                        SELECT d, SUM(amt) OVER (ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) as so_du_chay
                        FROM Biendong
                    )
                    SELECT d, so_du_chay FROM LuyKe WHERE so_du_chay < 0 LIMIT 1
                """
            
                check_params = (start_check_date, start_check_date, expense_id, 
                                start_check_date, start_check_date, expense_id,
                                new_date, new_amount)
            
                violation = cursor.execute(sql_check, check_params).fetchone()

                if violation:
                    return jsonify({
                        "success": False, 
                        "error": f"Giao dịch làm quỹ bị âm vào ngày {violation['d']}. Số dư còn lại sẽ là: {violation['so_du_chay']:,.0f} ₫"
                    }), 400

                # 4. TIẾN HÀNH CẬP NHẬT NẾU MỌI THỨ HỢP LỆ
                fields = []
                values = []
                allowed_fields = {
                    'date': 'date',
                    'receiver_name': 'receiver_name',
                    'address': 'address',
                    'amount': 'amount',
                    'reason': 'reason',
                }

                for key, db_field in allowed_fields.items():
                    if key in data:
                        fields.append(f"{db_field} = ?")
                        values.append(data[key])

                if 'payment_method' in data:
                    fields.append("credit_account = ?")
                    values.append(new_account)

                if not fields:
                    return jsonify({"success": False, "error": "Không có trường nào thay đổi"}), 400

                values.append(expense_id)
                sql_update = f"UPDATE phieu_chi SET {', '.join(fields)} WHERE id = ?"
            
                cursor.execute(sql_update, values)
                db.commit()
            
                return jsonify({"success": True, "message": "Cập nhật thành công!"})

            except Exception as e:
                db.rollback()
                return jsonify({"success": False, "error": str(e)}), 500
            finally:
                db.close()

    # --- Route 4: Route in Phiếu Chi (Mẫu 02-TT) ---
    @app.route('/PhieuChi/in/<int:expense_id>')
    def expense_print(expense_id):
        db = get_db_connection()
        db.row_factory = sqlite3.Row 
        cursor = db.cursor()
    
        # 1. SQL lấy dữ liệu Phiếu Chi và thông tin liên quan
        sql = """
            SELECT 
                pc.*, 
                s.company_name AS sale_company_name,
                s.address AS sale_address
            FROM phieu_chi pc
            LEFT JOIN sale s ON pc.source_id = s.id AND pc.source_type = 'return_sale'
            WHERE pc.id = ?
        """
    
        cursor.execute(sql, (expense_id,))
        data = cursor.fetchone()
    
        if not data:
            return "Không tìm thấy phiếu chi trong hệ thống", 404
        
        expense_dict = dict(data)
    
        # 2. Đồng bộ hóa dữ liệu Công ty và Địa chỉ
        expense_dict['company_name'] = expense_dict.get('sale_company_name') or expense_dict.get('company_name') or ''
        expense_dict['address'] = expense_dict.get('sale_address') or expense_dict.get('address') or ''

        # 3. Đảm bảo có key 'receiver_name' (Người nhận)
        if not expense_dict.get('receiver_name'):
            expense_dict['receiver_name'] = expense_dict.get('recipient_name') or expense_dict.get('payer_name') or ''
    
        return render_template(
            'KeToanHKD/PhieuChi_print.html', 
            imp=expense_dict
        )

    #=== LẬP PHIẾU CHI NỘP THUẾ====#
    @app.route('/api/expense/create-tax', methods=['POST'])
    def create_tax_expense():
        db = get_db_connection()
        cursor = db.cursor()
        try:
            data = request.get_json(silent=True)
            if not data:
                return jsonify({"success": False, "error": "Dữ liệu không hợp lệ"}), 400

            # Lấy dữ liệu từ frontend
            amount = data.get('amount')
            payment_method = data.get('payment_method')
            expense_type = data.get('expense_type') # Quý 1, Quý 2...
            reason = data.get('reason')             # Chi nộp thuế Quý 1 năm 2026...
            bill_no = data.get('bill_no')           # Số hóa đơn/chứng từ nộp thuế
            date_input = data.get('date')           # Ngày lập phiếu
            receiver = data.get('treasury_name')           # Ngày lập phiếu

            if not amount or not expense_type or not date_input:
                return jsonify({"success": False, "error": "Vui lòng điền đủ thông tin"}), 400

            # Logic kế toán theo yêu cầu
            credit_account = '111' if payment_method == 'cash' else '112'
            debit_account = '333' # Thuế và các khoản nộp nhà nước

            # Tạo Số phiếu chi tự động (PCxxxxxx)
            cursor.execute("SELECT voucher_no FROM phieu_chi WHERE voucher_no LIKE 'PC%' ORDER BY voucher_no DESC LIMIT 1")
            last_record = cursor.fetchone()
            new_number = 1
            if last_record and last_record[0]:
                try:
                    new_number = int(last_record[0][2:]) + 1
                except: pass
            new_voucher_no = f"PC{new_number:06d}"

            # Insert vào bảng phieu_chi
            sql = """
                INSERT INTO phieu_chi 
                    (voucher_no, receiver_name, amount, credit_account, debit_account, expense_type, 
                     reason, source_type, reference_document, preparer, date)
                VALUES 
                    (?, ?, ?, ?, ?, 'CP_THUE', ?, 'tax', ?, 'admin', ?)
            """
            params = (new_voucher_no, receiver, float(amount), credit_account, debit_account, 
                      reason, bill_no, date_input)
        
            cursor.execute(sql, params)
            new_id = cursor.lastrowid
            db.commit()

            return jsonify({"success": True, "id": new_id, "voucher_no": new_voucher_no})
        except Exception as e:
            db.rollback()
            return jsonify({"success": False, "error": str(e)}), 500

    # --- Route: API tạo Phiếu Chi ngoài (Other Expense) ---
    @app.route('/api/expense/create-other', methods=['POST'])
    def create_other_expense():
        from Services.sme.hkd_side_effects import write_hkd_cash_vouchers
        from Services.tenant_profile import get_current_tenant_profile
        if not write_hkd_cash_vouchers(profile=get_current_tenant_profile()):
            return jsonify(
                success=False,
                error='Tenant SME: dùng /api/sme/vouchers/payments (02-TT) thay vì phiếu chi HKD',
            ), 400

        db = get_db_connection()
        cursor = db.cursor()

        try:
            data = request.get_json(silent=True)
            if not data:
                return jsonify(success=False, error="Dữ liệu JSON không hợp lệ"), 400

            # ===== LẤY DỮ LIỆU =====
            receiver = data.get('receiver', '').strip()
            address = data.get('address', '').strip()
            amount = data.get('amount')
            payment_method = data.get('payment_method')
            expense_type = data.get('expense_type')
            reason = data.get('reason', '')
            bill_no = data.get('bill_no')
            date_input = data.get('date') or datetime.now().strftime('%Y-%m-%d')

            # ===== VALIDATE (FIX NHẸ) =====
            if not receiver or not expense_type:
                return jsonify(success=False, error="Thiếu thông tin bắt buộc"), 400

            try:
                amount = float(amount)
                if amount <= 0:
                    raise ValueError
            except:
                return jsonify(success=False, error="Số tiền không hợp lệ"), 400

            if payment_method not in ('cash', 'transfer'):
                return jsonify(success=False, error="Hình thức thanh toán không hợp lệ"), 400

            # ===== HẠCH TOÁN (GIỮ NGUYÊN) =====
            credit_account = '111' if payment_method == 'cash' else '112'
            debit_account = '642'
            source_type = 'other'

            # ===== TẠO SỐ PHIẾU (FIX AN TOÀN) =====
            cursor.execute("""
                SELECT voucher_no 
                FROM phieu_chi 
                WHERE voucher_no LIKE 'PC%' 
                ORDER BY id DESC 
                LIMIT 1
            """)
            last = cursor.fetchone()

            if last and last[0] and last[0].startswith('PC'):
                try:
                    new_number = int(last[0][2:]) + 1
                except:
                    new_number = 1
            else:
                new_number = 1

            voucher_no = f"PC{new_number:06d}"

            # ===== INSERT (GIỮ NGUYÊN) =====
            cursor.execute("""
                INSERT INTO phieu_chi (
                    voucher_no, receiver_name, address, amount,
                    debit_account, credit_account,
                    expense_type, reason,
                    source_type, reference_document,
                    preparer, date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                voucher_no,
                receiver,
                address,
                amount,
                debit_account,
                credit_account,
                expense_type,
                reason,
                source_type,
                bill_no,
                'Admin',
                date_input
            ))

            db.commit()
            new_id = cursor.lastrowid

            return jsonify(
                success=True,
                message="Tạo phiếu chi thành công",
                id=new_id,
                voucher_no=voucher_no,
                credit_account=credit_account,
                expense_type=expense_type,
                date=date_input
            ), 201

        except Exception as e:
            db.rollback()
            return jsonify(success=False, error=str(e)), 500
        finally:
            cursor.close()

    #===API LƯU PHIẾU CHI CHO HÀNG NHẬP/ THANH TOÁN CHO NCC===#
    #=== LẤY DANH SÁCH PHIẾU NHẬP KHO CHƯA THANH TOÁN===#
    @app.route('/api/import/unpaid-list')
    def api_import_unpaid_list():
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row

            query = """
                SELECT
                    i.id,
                    i.import_no,
                    i.date,
                    i.bill_no,
                    i.total_value,
                    COALESCE(i.paid_amount, 0) AS paid_amount,
                    i.total_value - COALESCE(i.paid_amount, 0) AS remaining_amount,
                    s.name AS supplier_name,
                    s.address AS supplier_address,
                    i.payment_status
                FROM import i
                LEFT JOIN suppliers s ON i.supplier_id = s.id
                WHERE i.payment_status != 'Đã thanh toán'
                   OR i.payment_status IS NULL
                ORDER BY i.date DESC
            """

            imports = conn.execute(query).fetchall()
            conn.close()

            result = []
            for row in imports:
                row_dict = dict(row)
                row_dict['supplier_name'] = row_dict.get('supplier_name') or 'Không xác định'
                row_dict['supplier_address'] = row_dict.get('supplier_address') or ''
                row_dict['import_no'] = row_dict.get('import_no') or f"NK{row_dict['id']}"
                # Chỉ hiển thị nếu còn nợ
                if row_dict['remaining_amount'] > 0:
                    result.append(row_dict)

            return jsonify(result)

        except Exception as e:
            print("Error in /api/import/unpaid-list:", e)
            return jsonify({"error": "Lỗi server"}), 500

    #=== API THANH TOÁN PHIẾU NHẬP KHO, LƯU VÀO BẢNG Phieu_Chi VÀ CẬP NHẬT "Đã Thanh Toán" vào BẢNG IMPORT===#
    @app.route('/api/expense/save', methods=['POST'])
    def save_expense():
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "Không nhận được dữ liệu"}), 400

        db = get_db_connection()
        db.row_factory = sqlite3.Row
        cursor = db.cursor()

        try:
            amount = float(data.get('amount', 0))
            if amount <= 0:
                return jsonify({"success": False, "error": "Số tiền phải lớn hơn 0"}), 400

            payment_method = data.get('payment_method', 'cash')
            ref_id = data.get('ref_id')
            date_input = data.get('date') or datetime.now().strftime('%Y-%m-%d')
            is_import = data.get('type') == 'import'

            receiver = (data.get('receiver') or '').strip()
            address = (data.get('address') or '').strip()
            bill_no = (data.get('bill_no') or '').strip()

            if is_import:
                if not ref_id:
                    return jsonify({"success": False, "error": "Thiếu ID phiếu nhập kho"}), 400
                if not receiver:
                    return jsonify({"success": False, "error": "Thiếu tên nhà cung cấp"}), 400

            # ===== LẤY THÔNG TIN PHIẾU NHẬP / HẠCH TOÁN =====
            import_no = None
            is_service_import = False
            if is_import:
                cursor.execute("""
                    SELECT import_no, COALESCE(doc_type, '') AS doc_type
                    FROM import
                    WHERE id = ?
                """, (ref_id,))
                row = cursor.fetchone()
                if not row:
                    return jsonify({"success": False, "error": "Không tìm thấy phiếu nhập kho"}), 400
                import_no = row['import_no']
                is_service_import = (row['doc_type'] or '').strip().lower() == 'service'
                if is_service_import:
                    cursor.execute("""
                        SELECT COUNT(*) FROM phieu_chi
                        WHERE source_id = ? AND source_type = 'import_service'
                    """, (ref_id,))
                    if cursor.fetchone()[0] > 0:
                        return jsonify({
                            "success": False,
                            "error": "Hóa đơn dịch vụ này đã có phiếu chi. Mỗi hóa đơn chỉ được lập một phiếu chi.",
                        }), 400

            # ===== TẠO SỐ PHIẾU CHI =====
            cursor.execute("""
                SELECT voucher_no
                FROM Phieu_chi
                WHERE voucher_no LIKE 'PC%'
                ORDER BY voucher_no DESC
                LIMIT 1
            """)
            last_pc = cursor.fetchone()

            new_number = int(last_pc['voucher_no'][2:]) + 1 if last_pc else 1
            voucher_no = f"PC{new_number:06d}"

            credit_account = '111' if payment_method == 'cash' else '112'
            if is_import and is_service_import:
                debit_account = '642'
                source_type = 'import_service'
                expense_type = 'CP_DV'
                reason = f"Thanh toán tiền mua dịch vụ số {import_no}"
            elif is_import:
                debit_account = '331'
                source_type = 'import'
                expense_type = None
                reason = f"Thanh toán tiền mua hàng phiếu nhập số {import_no}"
            else:
                debit_account = '642'
                source_type = 'other'
                expense_type = None
                reason = data.get('reason', 'Chi phí khác')

            cursor.execute("""
                INSERT INTO Phieu_chi (
                    voucher_no, receiver_name, address, amount,
                    credit_account, debit_account, reason,
                    source_type, reference_document, source_id,
                    expense_type, preparer, date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                voucher_no,
                receiver,
                address,
                amount,
                credit_account,
                debit_account,
                reason,
                source_type,
                bill_no,
                ref_id,
                expense_type,
                session.get('user_name', 'Admin'),
                date_input
            ))

            expense_id = cursor.lastrowid

            # ===== CẬP NHẬT THANH TOÁN NHẬP =====
            if is_import:
                cursor.execute("""
                    SELECT total_value, paid_amount
                    FROM import WHERE id = ?
                """, (ref_id,))
                imp = cursor.fetchone()

                total = float(imp['total_value'])
                paid = float(imp['paid_amount'] or 0)
                new_paid = paid + amount

                status = (
                    'Đã thanh toán' if new_paid >= total
                    else 'Thanh toán một phần' if new_paid > 0
                    else 'Chưa thanh toán'
                )

                cursor.execute("""
                    UPDATE import
                    SET paid_amount = ?, payment_status = ?
                    WHERE id = ?
                """, (new_paid, status, ref_id))

                cursor.execute("""
                    INSERT INTO import_payments
                    (import_id, expense_id, amount, payment_date)
                    VALUES (?, ?, ?, ?)
                """, (ref_id, expense_id, amount, date_input))

            db.commit()
            return jsonify({"success": True, "id": expense_id, "voucher_no": voucher_no})

        except Exception as e:
            db.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            cursor.close()

    @app.route('/api/expense/delete_and_reset/<int:expense_id>', methods=['DELETE'])
    def delete_and_reset(expense_id):
        db = get_db_connection()
        db.row_factory = sqlite3.Row
        cursor = db.cursor()
        try:
            cursor.execute("""
                SELECT source_type, source_id, amount
                FROM phieu_chi
                WHERE id = ?
            """, (expense_id,))
            row = cursor.fetchone()

            if not row:
                return jsonify({"success": False, "error": "Không tìm thấy phiếu chi."}), 404

            source_type = row['source_type']
            source_id = row['source_id']
            deleted_amount = float(row['amount'] or 0)

            if source_type == 'import_service' and source_id:
                cursor.execute("""
                    SELECT id, from_invoice_id, bill_no, supplier_id
                    FROM import
                    WHERE id = ?
                """, (source_id,))
                imp = cursor.fetchone()
                if not imp:
                    cursor.execute("DELETE FROM phieu_chi WHERE id = ?", (expense_id,))
                    message = "Đã xóa phiếu chi (không tìm thấy phiếu hạch toán liên kết)."
                else:
                    from Services.fixed_assets_helpers import (
                        count_active_assets_by_import_id,
                        delete_assets_by_import_id,
                    )
                    fa_act, tools_act = count_active_assets_by_import_id(cursor, source_id)
                    if fa_act or tools_act:
                        return jsonify({
                            "success": False,
                            "error": "Không thể xóa: TSCĐ/CCDC đã đưa vào sử dụng từ phiếu nhập này.",
                        }), 400

                    cursor.execute(
                        "DELETE FROM phieu_chi WHERE source_id = ? AND source_type IN ('import_service', 'import')",
                        (source_id,),
                    )
                    cursor.execute("DELETE FROM import_payments WHERE import_id = ?", (source_id,))
                    delete_assets_by_import_id(cursor, source_id)
                    cursor.execute("DELETE FROM import_details WHERE import_id = ?", (source_id,))
                    cursor.execute('DELETE FROM import WHERE id = ?', (source_id,))

                    from_invoice_id = imp['from_invoice_id']
                    bill_no = (imp['bill_no'] or '').strip()
                    if from_invoice_id:
                        cursor.execute(
                            "UPDATE supplier_invoice SET status = NULL WHERE id = ?",
                            (from_invoice_id,),
                        )
                    elif bill_no:
                        cursor.execute(
                            "UPDATE supplier_invoice SET status = NULL WHERE invoice_no = ?",
                            (bill_no,),
                        )
                    message = "Đã xóa phiếu chi và hủy hạch toán dịch vụ. Hóa đơn có thể hạch toán lại."

            elif source_type == 'import' and source_id:
                cursor.execute("""
                    SELECT paid_amount, total_value, COALESCE(doc_type, '') AS doc_type,
                           from_invoice_id, bill_no
                    FROM import
                    WHERE id = ?
                """, (source_id,))
                result = cursor.fetchone()

                if result and (result['doc_type'] or '').strip().lower() == 'service':
                    from Services.fixed_assets_helpers import (
                        count_active_assets_by_import_id,
                        delete_assets_by_import_id,
                    )
                    fa_act, tools_act = count_active_assets_by_import_id(cursor, source_id)
                    if fa_act or tools_act:
                        return jsonify({
                            "success": False,
                            "error": "Không thể xóa: TSCĐ/CCDC đã đưa vào sử dụng từ phiếu nhập này.",
                        }), 400

                    cursor.execute(
                        "DELETE FROM phieu_chi WHERE source_id = ? AND source_type IN ('import_service', 'import')",
                        (source_id,),
                    )
                    cursor.execute("DELETE FROM import_payments WHERE import_id = ?", (source_id,))
                    delete_assets_by_import_id(cursor, source_id)
                    cursor.execute("DELETE FROM import_details WHERE import_id = ?", (source_id,))
                    cursor.execute('DELETE FROM import WHERE id = ?', (source_id,))
                    from_invoice_id = result['from_invoice_id']
                    bill_no = (result['bill_no'] or '').strip()
                    if from_invoice_id:
                        cursor.execute(
                            "UPDATE supplier_invoice SET status = NULL WHERE id = ?",
                            (from_invoice_id,),
                        )
                    elif bill_no:
                        cursor.execute(
                            "UPDATE supplier_invoice SET status = NULL WHERE invoice_no = ?",
                            (bill_no,),
                        )
                    message = "Đã xóa phiếu chi và hủy hạch toán dịch vụ. Hóa đơn có thể hạch toán lại."

                elif result:
                    old_paid = float(result['paid_amount'] or 0)
                    if old_paid < deleted_amount:
                        return jsonify({
                            "success": False,
                            "error": "Dữ liệu không hợp lệ: số tiền đã trả nhỏ hơn số bị xóa.",
                        }), 400

                    new_paid = old_paid - deleted_amount
                    total_value = float(result['total_value'] or 0)
                    if new_paid <= 0:
                        status = "Chưa thanh toán"
                        new_paid = 0
                    elif new_paid >= total_value:
                        status = "Đã thanh toán"
                    else:
                        status = "Thanh toán một phần"

                    cursor.execute("""
                        UPDATE import
                        SET paid_amount = ?, payment_status = ?
                        WHERE id = ?
                    """, (new_paid, status, source_id))

                    cursor.execute(
                        "DELETE FROM import_payments WHERE expense_id = ?",
                        (expense_id,),
                    )
                    cursor.execute("DELETE FROM phieu_chi WHERE id = ?", (expense_id,))
                    message = "Đã xóa phiếu chi và cập nhật công nợ."
                else:
                    cursor.execute("DELETE FROM phieu_chi WHERE id = ?", (expense_id,))
                    message = "Đã xóa phiếu chi."

            else:
                cursor.execute("DELETE FROM phieu_chi WHERE id = ?", (expense_id,))
                message = "Đã xóa phiếu chi."

            cursor.execute("SELECT MAX(id) FROM phieu_chi")
            max_id = cursor.fetchone()[0]
            if max_id is None:
                cursor.execute("DELETE FROM sqlite_sequence WHERE name = 'phieu_chi'")
            else:
                cursor.execute(
                    "UPDATE sqlite_sequence SET seq = ? WHERE name = 'phieu_chi'",
                    (max_id,),
                )

            for table in ('import', 'import_details'):
                formatted = f'"{table}"' if table == 'import' else table
                cursor.execute(f"""
                    UPDATE sqlite_sequence
                    SET seq = (SELECT COALESCE(MAX(id), 0) FROM {formatted})
                    WHERE name = ?
                """, (table,))

            db.commit()
            return jsonify({"success": True, "message": message})

        except Exception as e:
            db.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            cursor.close()
            db.close()

    #==== Đánh Số Lại Phiếu Chi===#
    from flask import jsonify, current_app
    import sqlite3
    import os

    @app.route('/api/expense/renumber', methods=['POST'])
    def renumber_vouchers():
        try:
            # Lấy đường dẫn database động (linh hoạt mọi máy)
            base_dir = current_app.root_path
            db_path = os.path.join(base_dir, 'database.db')  # Thay tên file nếu khác (ví dụ app.db)

            if not os.path.exists(db_path):
                return jsonify({'success': False, 'message': 'Không tìm thấy file database!'}), 500

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # BƯỚC 1: Xóa voucher_no cũ để tránh lỗi UNIQUE
            cursor.execute("UPDATE phieu_chi SET voucher_no = NULL")

            # BƯỚC 2: Lấy danh sách phiếu chi theo đúng thứ tự thời gian
            # Ưu tiên: ngày chi (date) → id nhỏ trước (id nhỏ = lập sớm hơn)
            cursor.execute("""
                SELECT id 
                FROM phieu_chi 
                ORDER BY date ASC, id ASC
            """)
            rows = cursor.fetchall()

            if not rows:
                conn.close()
                return jsonify({'success': False, 'message': 'Không có phiếu chi nào để đánh lại số'})

            # BƯỚC 3: Đánh lại số từ PC000001 theo đúng thứ tự trên
            for index, row in enumerate(rows, start=1):
                new_voucher_no = f"PC{str(index).zfill(6)}"
                cursor.execute("""
                    UPDATE phieu_chi 
                    SET voucher_no = ? 
                    WHERE id = ?
                """, (new_voucher_no, row['id']))

            conn.commit()
            conn.close()

            return jsonify({
                'success': True,
                'count': len(rows),
                'message': 'Đã đánh lại số phiếu thành công theo thứ tự ngày chi (và giờ lập phiếu qua id)!'
            })

        except Exception as e:
            if 'conn' in locals():
                conn.rollback()
                conn.close()
            return jsonify({
                'success': False,
                'message': f'Lỗi: {str(e)}'
            }), 500


    # --- API Lấy danh sách phiếu nhập kho Kế Toán ---#
    @app.route('/api/stock-moves/imports', methods=['GET'])
    @login_required
    def api_stock_moves_imports():
        q = request.args.get('q', '').strip()
        start = request.args.get('start')
        end = request.args.get('end')

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        try:
            # 1. Lấy thông tin cơ bản
            biz_row = c.execute("SELECT business_name FROM business_info LIMIT 1").fetchone()
            biz_name = biz_row['business_name'] if biz_row else "Cửa hàng"

            # 2. Xây dựng câu SQL Grouped (Nhóm các dòng stock_moves theo phiếu)
            # Sử dụng LEFT JOIN với sale và suppliers để lấy thông tin đối tác
            sql_grouped = f"""
                SELECT 
                    MIN(sm.id) as min_id,
                    sm.ref_id, 
                    sm.type, 
                    sm.ref_type,
                    MAX(sm.date) as date, 
                    sm.note,
                    sm.ref_document,
                    SUM(ABS(sm.quantity * sm.cost_price)) as total_value,
                    -- Lấy thông tin từ nhà cung cấp (nếu là nhập hàng)
                    sup.name AS supplier_name,
                    i.bill_no AS import_bill_no,
                    -- Lấy thông tin từ khách hàng (nếu là trả hàng/xóa đơn)
                    s.customer_name AS sale_customer_name,
                    s.company_name AS sale_company_name
                FROM stock_moves sm
                LEFT JOIN import i ON (sm.ref_id = i.id OR sm.ref_document = i.import_no) AND sm.type = 'import'
                LEFT JOIN suppliers sup ON i.supplier_id = sup.id
                LEFT JOIN sale s ON sm.ref_id = s.id AND sm.type IN ('RETURN_SALE', 'DELETE_SALE')
                WHERE sm.type IN ('import', 'RETURN_SALE', 'DELETE_SALE')
                GROUP BY sm.ref_id, sm.type, sm.ref_document
            """

            # 3. SQL cuối cùng bao gồm tìm kiếm và tính số thứ tự
            final_sql = f"""
                SELECT t.*,
                (SELECT COUNT(DISTINCT COALESCE(sm2.ref_document, sm2.ref_id)) 
                 FROM stock_moves sm2 
                 WHERE sm2.type IN ('import', 'RETURN_SALE', 'DELETE_SALE') AND sm2.id <= t.min_id) as absolute_seq
                FROM ({sql_grouped}) t
                WHERE 1=1
            """
        
            params = []
            if start and end:
                final_sql += " AND date(t.date) BETWEEN ? AND ?"
                params.extend([start, end])
        
            if q:
                final_sql += """ AND (
                    t.ref_document LIKE ? 
                    OR t.sale_customer_name LIKE ? 
                    OR t.sale_company_name LIKE ? 
                    OR t.supplier_name LIKE ? 
                    OR t.note LIKE ?
                )"""
                search = f'%{q}%'
                params.extend([search, search, search, search, search])

            final_sql += " ORDER BY t.date DESC, t.min_id DESC"
        
            c.execute(final_sql, params)
            rows = c.fetchall()

            result = []
            for row in rows:
                data = dict(row)
            
                # --- XÁC ĐỊNH TÊN ĐỐI TÁC VÀ CÔNG TY ---
                partner_name = biz_name
                company_name = ""

                if data['type'] == 'import':
                    partner_name = data['supplier_name'] or "Nhà cung cấp"
                elif data['type'] in ('RETURN_SALE', 'DELETE_SALE'):
                    partner_name = data['sale_customer_name'] or "Khách hàng"
                    # Chỉ lấy company_name nếu khác với tên khách hàng
                    comp = data['sale_company_name']
                    if comp and comp != partner_name:
                        company_name = comp
                elif data['ref_type'] in ('inventory_check', 'initial_import'):
                    partner_name = biz_name

                # Tạo mã phiếu PN
                doc_no = data['ref_document'] if (data['ref_document'] and 'PN' in str(data['ref_document'])) \
                         else f"PN{str(data['absolute_seq']).zfill(6)}"
            
                # Hiển thị số hóa đơn/đơn hàng
                display_hd = f"ĐH{str(data['ref_id']).zfill(6)}" if data['type'] in ('RETURN_SALE', 'DELETE_SALE') \
                             else (data['import_bill_no'] or '—')
            
                result.append({
                    "id": data['ref_id'],
                    "doc_no": doc_no, 
                    "date": str(data['date'])[:10],
                    "partner_name": partner_name,
                    "company_name": company_name,
                    "note": data['note'] or '',
                    "total_value": round(float(data['total_value'] or 0), 2),
                    "move_type": data['type'],
                    "display_hd": display_hd
                })

            return jsonify(result), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

    #==== ROUTE IN PHIẾU NHẬP KHO MẪU 04VT ====#
    @app.route('/in-phieu-nhap/<int:ref_id>')
    @login_required
    def in_phieu_nhap(ref_id):
        move_type = request.args.get('type', 'import').lower()
        target_ref_type = request.args.get('ref_type', '').lower()
    
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        try:
            # 1. Lấy thông tin Hộ kinh doanh
            cur.execute("SELECT * FROM business_info LIMIT 1")
            info_row = cur.fetchone()
            info = dict(info_row) if info_row else {'business_name': 'Hộ Kinh Doanh', 'address': 'Chưa cập nhật'}

            # 2. LẤY ĐẦU PHIẾU
            query = "SELECT * FROM stock_moves WHERE ref_id = ? AND LOWER(type) = ?"
            params = [ref_id, move_type]
        
            if target_ref_type:
                query += " AND LOWER(ref_type) = ?"
                params.append(target_ref_type)
        
            query += " ORDER BY id ASC LIMIT 1"
            cur.execute(query, params)
            move_head = cur.fetchone()

            if not move_head:
                return f"Không tìm thấy dữ liệu cho phiếu #{ref_id} loại {move_type}", 404

            # 3. Tính số thứ tự phiếu PNxxxxxx
            cur.execute("""
                SELECT COUNT(DISTINCT (ref_id || '-' || type || '-' || COALESCE(ref_type, ''))) AS pn_count
                FROM stock_moves
                WHERE LOWER(type) IN ('import', 'return_sale', 'delete_sale')
                AND id <= ?
            """, (move_head['id'],))
            pn_count = cur.fetchone()[0] or 1

            # 4. Format ngày tháng
            raw_date = move_head['date']
            try:
                formatted_date = datetime.strptime(raw_date[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
            except:
                formatted_date = raw_date

            imp = {
                'voucher_no': move_head['ref_document'] if (move_head['ref_document'] and 'PN' in move_head['ref_document']) 
                              else f"PN{pn_count:06d}",
                'date': raw_date,
                'warehouse_location': info.get('warehouse_location') or 'Kho tổng',
                'supplier_name': 'Nguồn khác',
                'company_name': '', # Bổ sung trường này
                'bill_no': '',
                'note': move_head['note'] or "",
                'partner_label': 'Đối tác'
            }

            # 5. XỬ LÝ LOGIC CHI TIẾT
            m_type = move_head['type'].lower()
            r_type = move_head['ref_type'].lower() if move_head['ref_type'] else ''

            # A. Hàng mua từ Nhà cung cấp
            if m_type == 'import' and r_type == 'import':
                cur.execute("""
                    SELECT s.name, i.bill_no, i.bill_date FROM import i
                    JOIN suppliers s ON i.supplier_id = s.id WHERE i.id = ?
                """, (ref_id,))
                sup = cur.fetchone()
                if sup:
                    imp['supplier_name'] = sup['name']
                    imp['bill_no'] = f"Hóa đơn số: {sup['bill_no'] or '...'}"
                    if sup['bill_date']:
                        try:
                            bill_date_vn = datetime.strptime(sup['bill_date'][:10], '%Y-%m-%d').strftime('%d/%m/%Y')
                            imp['bill_no'] += f" ngày {bill_date_vn}"
                        except:
                            imp['bill_no'] += f" ngày {sup['bill_date']}"
                imp['partner_label'] = "Nhà cung cấp"

            # B. FIX: Khách trả hàng - Lấy thêm company_name
            elif m_type in ('return_sale', 'delete_sale'):
                cur.execute("SELECT customer_name, company_name FROM sale WHERE id = ?", (ref_id,))
                sale = cur.fetchone()
                if sale:
                    partner = sale['customer_name'] or "Bán cho người tiêu dùng"
                    company = sale['company_name'] or ""
                
                    imp['supplier_name'] = partner
                    # Nếu có tên công ty và khác với tên khách hàng thì lưu lại
                    if company and company != partner:
                        imp['company_name'] = company
                
                imp['bill_no'] = f"Khách trả hàng theo Đơn Hàng số ĐH{ref_id:06d}"
                imp['partner_label'] = "Khách hàng"

            # C. Nhập tồn kho cũ
            elif r_type == 'initial_import':
                imp['supplier_name'] = info['business_name']
                imp['bill_no'] = f"Nhập kho tồn cũ theo Bảng kê ngày {formatted_date}"
                imp['partner_label'] = "Bên bàn giao"

            # D. Kiểm kê dư
            elif r_type == 'inventory_check':
                imp['supplier_name'] = info['business_name']
                imp['bill_no'] = f"Nhập hàng kiểm kê dư theo Biên bản kiểm kê ngày {formatted_date}"
                imp['partner_label'] = "Lý do nhập"
        
            # 6. LẤY CHI TIẾT MẶT HÀNG
            cur.execute("""
                SELECT p.name AS product_name, p.product_code AS barcode, p.unit AS base_unit,
                       ABS(sm.quantity) AS quantity, sm.cost_price, 
                       ABS(sm.quantity * sm.cost_price) AS total_amount
                FROM stock_moves sm
                JOIN products p ON sm.product_id = p.id
                WHERE sm.ref_id = ? AND LOWER(sm.type) = ? AND LOWER(COALESCE(sm.ref_type, '')) = ?
                ORDER BY sm.id ASC
            """, (ref_id, m_type, r_type))
            items = [dict(row) for row in cur.fetchall()]
        
            total_val = sum(item['total_amount'] for item in items)
            imp['total_amount'] = total_val
            try:
                imp['total_str'] = so_thanh_chu(round(total_val))
            except Exception:
                imp['total_str'] = "........................"

            return render_template('KeToanHKD/PhieuNhapKho_print.html', imp=imp, items=items, info=info)

        finally:
            cur.close()
            conn.close()

    #==== API Lấy Danh Sách Phiếu Xuất Kho===#
    @app.route('/api/export-vouchers/list')
    def api_export_vouchers_list():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        q = request.args.get('q', '').strip()
        start = request.args.get('start_date')
        end = request.args.get('end_date')

        conditions = []
        params = []
        if start:
            conditions.append("px.date >= ?")
            params.append(f"{start} 00:00:00")
        if end:
            conditions.append("px.date <= ?")
            params.append(f"{end} 23:59:59")
        if q:
            like_q = f"%{q}%"
            conditions.append("(px.voucher_no LIKE ? OR px.customer_name LIKE ?)")
            params.extend([like_q, like_q])

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        # SQL mới: Sử dụng UNION để tính tổng tiền cho cả phiếu bán hàng và phiếu xuất kho lẻ
        query = f"""
            SELECT 
                px.id, 
                px.voucher_no, 
                px.date, 
                px.customer_name, 
                px.note,
                (
                    SELECT SUM(COALESCE(inv.avg_cost, 0) * items.qty)
                    FROM (
                        -- Trường hợp 1: Lấy từ sale_items nếu có sale_id
                        SELECT si.product_id, si.quantity as qty
                        FROM sale_items si 
                        WHERE px.sale_id IS NOT NULL AND si.sale_id = px.sale_id
                    
                        UNION ALL
                    
                        -- Trường hợp 2: Lấy từ stock_moves nếu sale_id là NULL (trả hàng/xuất lẻ)
                        SELECT sm.product_id, ABS(sm.quantity) as qty
                        FROM stock_moves sm
                        WHERE px.sale_id IS NULL AND sm.ref_id = px.id AND sm.type IN ('SALE', 'RETURN_IMPORT')
                    ) items
                    LEFT JOIN inventory inv ON items.product_id = inv.product_id
                ) as total_avg_cost
            FROM phieu_xuat_kho px
            {where_clause}
            ORDER BY px.date DESC
        """

        try:
            cur.execute(query, params)
            rows = cur.fetchall()
            result = []
            for r in rows:
                result.append({
                    "id": r['id'],
                    "voucher_no": r['voucher_no'],
                    "date": r['date'],
                    "customer_name": r['customer_name'] or "Bán cho người tiêu dùng",
                    "total_amount": round(float(r['total_avg_cost'] or 0), 0),
                    "note": r['note'] or "Xuất kho"
                })
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()



    #=== IN PHIẾU XUẤT KHO NỘI BỘ (04-VT)===#
    @app.route('/DanhSachPhieuXuatKho')
    def DanhSachPhieuXuatKho():
        return render_template('KeToanHKD/DanhSachPhieuXuatKho.html')

    #===API Lấy Danh Sách Phiếu Xuất Kho===#
    @app.route('/api/stock-moves/exports', methods=['GET'])
    @login_required
    def api_stock_moves_exports():
        q = request.args.get('q', '').strip()
        start = request.args.get('start')
        end = request.args.get('end')

        conn = get_db_connection() 
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        try:
            # 1. Lấy tên doanh nghiệp
            c.execute("SELECT business_name FROM business_info LIMIT 1")
            biz_row = c.fetchone()
            business_name = biz_row['business_name'] if biz_row else "Cửa hàng"

            # 2. Định nghĩa các loại chứng từ xuất
            types_str = "('SALE', 'RETURN_IMPORT', 'DELETE_IMPORT', 'export')"
            safe_biz_name = business_name.replace("'", "''")

            # 3. SQL chính - Sử dụng LEFT JOIN để lấy company_name hiệu quả hơn
            # Chúng ta JOIN với sale dựa trên ref_id khi type là SALE
            sql_main = f"""
                SELECT 
                    sm.ref_id, 
                    sm.type, 
                    MAX(sm.date) as date, 
                    sm.note, 
                    sm.ref_document,
                    SUM(ABS(sm.quantity * sm.cost_price)) as total_value,
                
                    -- Tính số thứ tự phiếu PX
                    (SELECT COUNT(DISTINCT COALESCE(sm2.ref_document, sm2.ref_id)) 
                     FROM stock_moves sm2 
                     WHERE sm2.type IN {types_str} 
                     AND sm2.id <= MIN(sm.id)) as absolute_seq,

                    -- Lấy tên đối tác
                    CASE 
                        WHEN sm.type = 'SALE' THEN s.customer_name
                        WHEN sm.type IN ('RETURN_IMPORT', 'DELETE_IMPORT') THEN 
                            (SELECT sup.name FROM suppliers sup 
                             JOIN import i ON i.supplier_id = sup.id 
                             WHERE i.id = sm.ref_id LIMIT 1)
                        ELSE '{safe_biz_name}'
                    END AS partner_name,

                    -- Lấy tên công ty (chỉ dành cho SALE)
                    CASE 
                        WHEN sm.type = 'SALE' THEN s.company_name
                        ELSE ''
                    END AS company_name
                FROM stock_moves sm
                LEFT JOIN sale s ON sm.ref_id = s.id AND sm.type = 'SALE'
                WHERE sm.type IN {types_str}
            """
        
            params = []
            if start and end:
                sql_main += " AND date(sm.date) BETWEEN ? AND ?"
                params.extend([start, end])
        
            sql_main += " GROUP BY COALESCE(sm.ref_document, sm.ref_id), sm.type"

            # 4. Bọc Subquery để search theo cả tên công ty
            if q:
                final_sql = f"""
                    SELECT * FROM ({sql_main}) 
                    WHERE partner_name LIKE ? 
                    OR company_name LIKE ? 
                    OR note LIKE ? 
                    OR ref_document LIKE ?
                """
                search_q = f'%{q}%'
                params.extend([search_q, search_q, search_q, search_q])
            else:
                final_sql = sql_main
            
            final_sql += " ORDER BY date DESC, ref_id DESC"
        
            c.execute(final_sql, params)
            rows = c.fetchall()

            result = []
            for row in rows:
                data = dict(row)
            
                # Logic hiển thị mã tham chiếu
                display_hd = f"ĐH{str(data['ref_id']).zfill(6)}" if data['type'] == 'SALE' else (data['ref_document'] or '—')
            
                # Mã phiếu xuất (PX...)
                doc_no = data['ref_document'] if (data['ref_document'] and data['ref_document'].startswith('PX')) \
                         else f"PX{str(data['absolute_seq']).zfill(6)}"
            
                # Xử lý logic company_name để frontend dễ hiển thị
                p_name = data['partner_name'] or "Nội bộ"
                c_name = data['company_name'] or ""

                result.append({
                    "id": data['ref_id'],
                    "doc_no": doc_no, 
                    "date": str(data['date'])[:10],
                    "partner_name": p_name,
                    "company_name": c_name if (c_name and c_name != p_name) else "",
                    "note": data['note'] or '',
                    "total_value": round(float(data['total_value'] or 0), 2),
                    "move_type": data['type'],
                    "display_hd": display_hd
                })

            return jsonify(result), 200

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

    #=== PHIẾU XUẤT KHO NỘI BỘ (04-VT)====
    @app.route('/in-phieu-xuat/<int:ref_id>')
    @login_required
    def print_export_voucher(ref_id):
        move_type = request.args.get('type', 'SALE')
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        try:
            info = c.execute("SELECT * FROM business_info LIMIT 1").fetchone()
        
            current_move = c.execute("""
                SELECT id, note, date, ref_document 
                FROM stock_moves WHERE ref_id = ? AND type = ? LIMIT 1
            """, (ref_id, move_type)).fetchone()

            if not current_move:
                return "Không tìm thấy dữ liệu", 404

            iso_date = format_date_for_frontend(current_move['date'])
            vn_date = format_vn_date(current_move['date'])

            display_data = {
                "title": "PHIẾU XUẤT KHO", 
                "partner_label": "Họ tên người nhận", # Đổi nhãn cho khớp yêu cầu
                "customer_name": "", 
                "company_name": "",
                "address": ""
            }
            bill_date_iso = None
            raw_note = current_move['note'] or ""

            if move_type == 'SALE':
                sale = c.execute("SELECT customer_name, company_name, address FROM sale WHERE id = ?", (ref_id,)).fetchone()
                if sale:
                    # Tách biệt rõ ràng 2 trường
                    display_data["customer_name"] = sale['customer_name'] or "Bán cho người tiêu dùng"
                    display_data["company_name"] = sale['company_name'] or ""
                    display_data["address"] = sale['address'] or ""
                display_data["partner_label"] = "Họ tên người mua hàng"

            elif move_type == 'RETURN_IMPORT':
                imp = c.execute("""
                    SELECT s.name, s.address, i.bill_date 
                    FROM suppliers s JOIN import i ON i.supplier_id = s.id 
                    WHERE i.id = ?
                """, (ref_id,)).fetchone()
                if imp:
                    display_data.update({
                        "title": "PHIẾU XUẤT TRẢ HÀNG", 
                        "partner_label": "Đại diện nhà cung cấp", 
                        "customer_name": imp['name'], 
                        "address": imp['address']
                    })
                    bill_date_iso = format_date_for_frontend(imp['bill_date'])

            # Ghi chú không còn chứa tên khách hàng
            ref_doc = current_move['ref_document'] or ""
            if move_type == 'SALE':
                enhanced_note = f"{raw_note} theo Đơn Hàng: ĐH{str(ref_id).zfill(6)} (Ngày: {vn_date})".strip()
            else:
                enhanced_note = f"{raw_note} (Ngày: {vn_date})".strip()

            # Logic số phiếu PX
            export_types = "('SALE', 'RETURN_IMPORT', 'DELETE_IMPORT', 'export')"
            seq_row = c.execute(f"SELECT COUNT(DISTINCT COALESCE(ref_document, ref_id)) as seq FROM stock_moves WHERE type IN {export_types} AND id <= ?", (current_move['id'],)).fetchone()
            voucher_no = ref_doc if (ref_doc and ref_doc.startswith('PX')) else f"PX{str(seq_row['seq']).zfill(6)}"

            items = [dict(r) for r in c.execute("""
                SELECT p.name as product_name, p.product_code, p.unit, 
                       ABS(sm.quantity) as qty, sm.cost_price as price, 
                       ABS(sm.quantity * sm.cost_price) as amount 
                FROM stock_moves sm LEFT JOIN products p ON sm.product_id = p.id 
                WHERE sm.ref_id = ? AND sm.type = ?
            """, (ref_id, move_type)).fetchall()]

            px = {
                "voucher_no": voucher_no,
                "date": iso_date,
                "bill_date": bill_date_iso,
                "title": display_data['title'],
                "partner_label": display_data['partner_label'],
                "customer_name": display_data['customer_name'],
                "company_name": display_data['company_name'],
                "address": display_data['address'],
                "note": enhanced_note,
                "hang_hoa": items,
                "total_amount": sum(i['amount'] for i in items),
                "total_str": so_thanh_chu(round(sum(i['amount'] for i in items)))
            }

            return render_template('KeToanHKD/PhieuXuatKho_print.html', px=px, info=info)
        finally:
            conn.close()

    #=== IN PHIẾU XUẤT KHO THEO TOA BÁN HÀNG NỘI DUNG GIỐNG TRÊN HÓA ĐƠN===#
    @app.route('/DanhSachPhieuXuatKhoTheoDonBan')
    def DanhSachPhieuXuatKhoTheoDonBan():
        return render_template('KeToanHKD/DanhSachPhieuXuatKhoTheoDonBan.html')

    @app.route('/PhieuXuatKhoTheoDonBan/in/<int:sale_id>')
    def print_delivery_note1(sale_id):
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. LẤY THÔNG TIN HỘ KINH DOANH
        cur.execute("SELECT business_name, address, warehouse_location, representative_name FROM business_info LIMIT 1")
        biz_row = cur.fetchone()
        biz_info = dict(biz_row) if biz_row else {}

        # 2. LẤY THÔNG TIN PHIẾU XUẤT KHO
        cur.execute("SELECT * FROM phieu_xuat_kho WHERE id = ?", (voucher_id,))
        px_row = cur.fetchone()
        if not px_row:
            conn.close()
            abort(404, "Không tìm thấy phiếu xuất kho")
    
        px_raw = dict(px_row)

        # 3. XỬ LÝ DỮ LIỆU HÀNG HÓA (Dữ liệu lưu dạng JSON trong bảng phieu_xuat_kho)
        try:
            danh_sach_hang = json.loads(px_raw['items_json'])
        except:
            danh_sach_hang = []

        conn.close()

        # 4. ĐÓNG GÓI DỮ LIỆU CHO TEMPLATE
        total_amount = px_raw.get("total_amount") or 0
    
        px_data = {
            "id": px_raw["id"],
            "voucher_no": px_raw["voucher_no"], # Sử dụng số phiếu PX000001
            "date": px_raw.get("date"),
            "customer_name": px_raw.get("customer_name") or "Bán cho người tiêu dùng",
            "address": px_raw.get("address") or "",
            "note": px_raw.get("note") or "Xuất kho",
            "warehouse_location": biz_info.get("warehouse_location") or "Kho tổng",
            "representative_name": biz_info.get("representative_name"),
            "total_amount": total_amount,
            "total_str": so_thanh_chu(total_amount).capitalize() + ".",
            "hang_hoa": danh_sach_hang
        }

        config = {
            "HKD_NAME": biz_info.get("business_name") or "TÊN HỘ KINH DOANH",
            "HKD_ADDRESS": biz_info.get("address") or "ĐỊA CHỈ HKD"
        }

        return render_template("KeToanHKD/PhieuXuatKhoTheoDonBan_print.html", px=px_data, config=config)

    #=== Hết Phần Phiếu Xuất Kho===#

    #******HIỂN THỊ TRANG QUẢN LÝ LƯƠNG VÀ CÁC KHOẢN TRÍCH THEO LƯƠNG******#
    #=== Hàm Lấy Các Ngày Làm Việc Trong Tháng/ Trừ ngày Chủ Nhật====#

    def get_working_days_exclude_sun(month, year):
        num_days = calendar.monthrange(year, month)[1]
        # Bây giờ Python đã hiểu 'date' là gì
        count = sum(1 for d in range(1, num_days + 1) if date(year, month, d).weekday() != 6)
        return count

    #=== THÊM NHÂN VIÊN MỚI VÀO DATABASE===#
    @app.route('/api/add_employee', methods=['POST'])
    @login_required
    def add_employee():
        data = request.json
        if not data:
            return jsonify({"success": False, "message": "Không nhận được dữ liệu"}), 400

        fullname = data.get('fullname')
        position = data.get('position')
        phone = data.get('phone')
        join_date = data.get('join_date')  # Lịch Việt Nam gửi lên dạng YYYY-MM-DD
        address = data.get('address')
        id_card = data.get('id_card') or data.get('cccd')
        base_salary = float(data.get('base_salary') or 0)
        salary_rate = data.get('salary_rate')
        salary_rate_val = float(salary_rate) if salary_rate not in (None, '') else None
    
        # Các trường giảm trừ gia cảnh (Theo ảnh Screenshot 437)
        dependents = int(data.get('dependents') or 0)
        self_deduction = float(data.get('self_deduction') or 11000000)
        dependent_deduction = float(data.get('dependent_deduction') or 4400000)
        attendance_code = (data.get('attendance_code') or '').strip() or None
        allowance_fund = float(data.get('allowance_fund') or 0)
        allowance_other = float(data.get('allowance_other') or 0)
        default_bonus = float(data.get('default_bonus') or data.get('bonus') or 0)

        if not fullname or not id_card:
            return jsonify({"success": False, "message": "Họ tên và CCCD là bắt buộc"})

        conn = get_db_connection()
        try:
            from Services.chu_ho_helpers import sync_chu_ho_from_business_info, ensure_is_chu_ho_column
            from Services.employee_payroll_helpers import (
                ensure_employee_allowance_columns,
                normalize_department,
            )
            ensure_is_chu_ho_column(conn)
            ensure_employee_allowance_columns(conn)
            department = normalize_department(data.get('department'))
            conn.execute("""
                INSERT INTO employees (
                    fullname, position, id_card, base_salary, salary_rate,
                    phone, join_date, address, dependents,
                    self_deduction, dependent_deduction, attendance_code,
                    allowance_fund, allowance_other, default_bonus, department,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            """, (fullname, position, id_card, base_salary, salary_rate_val,
                  phone, join_date, address, dependents,
                  self_deduction, dependent_deduction, attendance_code,
                  allowance_fund, allowance_other, default_bonus, department))
            matched_ids, owner_name = sync_chu_ho_from_business_info(conn)
            new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
            return jsonify({
                "success": True,
                "message": "Thêm nhân viên thành công",
                "id": new_id,
                "is_chu_ho": 1 if int(new_id) in matched_ids else 0,
                "owner_representative_name": owner_name,
            })
        except Exception as e:
            return jsonify({"success": False, "message": f"Lỗi: {str(e)}"})
        finally:
            conn.close()

    #=== API CHỈNH SỬA THÔNG TIN NHÂN VIÊN===#
    @app.route('/api/update_employee', methods=['POST'])
    @login_required
    def update_employee():
        data = request.json
        if not data or not data.get('id'):
            return jsonify({"success": False, "message": "Thiếu ID nhân viên"}), 400

        emp_id = data.get('id')
        new_base_salary = float(data.get('base_salary') or 0)
        salary_rate = data.get('salary_rate')
        salary_rate_val = float(salary_rate) if salary_rate not in (None, '') else None

        # Lấy thông tin trạng thái nghỉ việc từ frontend
        status = data.get('status')  # 1: Đang làm, 0: Đã nghỉ việc
        if status is None:
            status = data.get('is_active', 1)  # Hỗ trợ cả hai tên trường

        allowance_fund = float(data.get('allowance_fund') or 0)
        allowance_other = float(data.get('allowance_other') or 0)
        default_bonus = float(data.get('default_bonus') or data.get('bonus') or 0)

        conn = get_db_connection()
        try:
            from Services.chu_ho_helpers import sync_chu_ho_from_business_info, ensure_is_chu_ho_column
            from Services.employee_payroll_helpers import (
                ensure_employee_allowance_columns,
                normalize_department,
            )
            ensure_is_chu_ho_column(conn)
            ensure_employee_allowance_columns(conn)
            department = normalize_department(data.get('department'))

            # Chuẩn bị dữ liệu cập nhật
            fields = (
                data.get('fullname'),
                data.get('position'),
                data.get('id_card'),
                new_base_salary,
                salary_rate_val,
                data.get('phone'),
                data.get('join_date'),
                data.get('address'),
                int(data.get('dependents') or 0),
                float(data.get('self_deduction') or 11000000),
                float(data.get('dependent_deduction') or 4400000),
                (data.get('attendance_code') or '').strip() or None,
                allowance_fund,
                allowance_other,
                default_bonus,
                department,
                int(status),
                emp_id
            )

            # 1. Kiểm tra biến động lương để ghi lịch sử
            old_data = conn.execute("SELECT base_salary FROM employees WHERE id = ?", (emp_id,)).fetchone()

            if old_data and float(old_data['base_salary'] or 0) != new_base_salary:
                conn.execute("""
                    INSERT INTO salary_history (employee_id, old_salary, new_salary, reason)
                    VALUES (?, ?, ?, ?)
                """, (emp_id, old_data['base_salary'], new_base_salary,
                      data.get('reason_change', "Cập nhật hồ sơ")))

            # 2. Cập nhật bảng employees (đã thêm cột status)
            conn.execute("""
                UPDATE employees
                SET fullname = ?, position = ?, id_card = ?, base_salary = ?,
                    salary_rate = ?, phone = ?, join_date = ?, address = ?, dependents = ?,
                    self_deduction = ?, dependent_deduction = ?, attendance_code = ?,
                    allowance_fund = ?, allowance_other = ?, default_bonus = ?,
                    department = ?, status = ?
                WHERE id = ?
            """, fields)
            matched_ids, owner_name = sync_chu_ho_from_business_info(conn)
            conn.commit()
            emp_row = conn.execute(
                'SELECT COALESCE(is_chu_ho, 0) AS is_chu_ho FROM employees WHERE id = ?',
                (emp_id,),
            ).fetchone()
            return jsonify({
                "success": True,
                "message": "Cập nhật hồ sơ thành công",
                "is_chu_ho": int(emp_row['is_chu_ho'] or 0) if emp_row else 0,
                "owner_representative_name": owner_name,
            })

        except Exception as e:
            conn.rollback()
            return jsonify({"success": False, "message": f"Lỗi DB: {str(e)}"})
        finally:
            conn.close()

    #===LỊCH SỬ TĂNG LƯƠNG===#
    @app.route('/api/get_salary_history/<int:emp_id>')
    @login_required
    def get_salary_history(emp_id):
        conn = get_db_connection()
        history = conn.execute("""
            SELECT old_salary, new_salary, change_date, reason 
            FROM salary_history WHERE employee_id = ? 
            ORDER BY change_date DESC
        """, (emp_id,)).fetchall()
        conn.close()
        return jsonify([dict(row) for row in history])

    #==== Lấy Danh Sách Nhân Viên để tính lương===#
    @app.route('/api/get_all_employees')
    def get_all_employees():
        """
        API trả về nhân viên kèm ngày công từ bảng chấm công (tham số month, year).
        """
        month = request.args.get('month', type=int)
        year = request.args.get('year', type=int)
        conn = get_db_connection()
        try:
            from Services.employee_payroll_helpers import (
                department_label,
                ensure_employee_allowance_columns,
                expense_account_for_department,
                normalize_department,
            )
            ensure_employee_allowance_columns(conn, commit=False)
            query = """
                SELECT 
                    id, fullname, id_card, phone, address, position, join_date,
                    base_salary, self_deduction, dependents, dependent_deduction,
                    COALESCE(department, 'ADMIN') AS department
                FROM employees 
                WHERE status = 1
                ORDER BY fullname COLLATE NOCASE ASC
            """
            employees = conn.execute(query).fetchall()
            attendance_days = {}
            if month and year:
                from Services.attendance_helpers import get_monthly_work_days_map
                attendance_days = get_monthly_work_days_map(conn, month, year)

            result = []
            for row in employees:
                item = dict(row)
                dept = normalize_department(item.get('department'))
                item['department'] = dept
                item['department_label'] = department_label(dept)
                item['expense_account'] = expense_account_for_department(dept)
                item['attendance_work_days'] = attendance_days.get(item['id'], 0)
                result.append(item)
            return jsonify(result)
        except sqlite3.Error as db_err:
            print(f"Database Error tại /api/get_all_employees: {str(db_err)}")
            return jsonify({"error": "Lỗi kết nối cơ sở dữ liệu"}), 500
        except Exception as e:
            print(f"System Error tại /api/get_all_employees: {str(e)}")
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/salary/prepare')
    @login_required
    def api_salary_prepare():
        """Tải dữ liệu lập bảng lương: nhân viên + ngày công từ chấm công."""
        try:
            month = int(request.args.get('month') or datetime.now().month)
            year = int(request.args.get('year') or datetime.now().year)
        except (TypeError, ValueError):
            return jsonify(success=False, message='Tháng/năm không hợp lệ'), 400

        standard_days = get_working_days_exclude_sun(month, year)
        conn = get_db_connection()
        try:
            from Services.attendance_helpers import get_monthly_work_days_map, ensure_attendance_schema
            from Services.chu_ho_helpers import sync_chu_ho_from_business_info, ensure_is_chu_ho_column
            from Services.employee_payroll_helpers import ensure_employee_allowance_columns, ensure_payroll_schema
            ensure_attendance_schema(conn)
            ensure_is_chu_ho_column(conn)
            ensure_payroll_schema(conn)
            sync_chu_ho_from_business_info(conn, commit=False)
            attendance_days = get_monthly_work_days_map(conn, month, year)

            rows = conn.execute("""
                SELECT
                    id, fullname, id_card, phone, address, position, join_date,
                    base_salary, self_deduction, dependents, dependent_deduction,
                    COALESCE(allowance_fund, 0) AS allowance_fund,
                    COALESCE(allowance_other, 0) AS allowance_other,
                    COALESCE(default_bonus, 0) AS default_bonus,
                    COALESCE(is_chu_ho, 0) AS is_chu_ho
                FROM employees
                WHERE status = 1
                ORDER BY fullname COLLATE NOCASE ASC
            """).fetchall()

            employees = []
            for row in rows:
                item = dict(row)
                emp_id = item['id']
                att = attendance_days.get(emp_id, 0)
                item['employee_id'] = emp_id
                item['attendance_work_days'] = att
                employees.append(item)

            from Services.chu_ho_helpers import get_owner_profile
            owner = get_owner_profile(conn)

            return jsonify({
                'success': True,
                'month': month,
                'year': year,
                'standard_days': standard_days,
                'has_attendance_data': bool(attendance_days),
                'employees': employees,
                'owner': owner,
            })
        except Exception as e:
            return jsonify(success=False, message=str(e)), 500
        finally:
            conn.close()

    # --- TRANG DANH SÁCH BẢNG LƯƠNG TỔNG HỢP---#
    @app.route('/DanhSachBangLuong_05LDTL')
    def DanhSachBangLuong_05LDTL():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
    
        # 1. Lấy thông tin cấu hình từ business_info
        info = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
    
        # 2. Lấy danh sách danh mục vùng từ bảng tham chiếu
        # (Giả định bạn đã có bảng salary_regions như hướng dẫn trước)
        vung_list = conn.execute("SELECT * FROM salary_regions").fetchall()
    
        # 3. Lấy tóm tắt danh sách bảng lương
        summary_data = conn.execute("""
            SELECT month, year,
                   SUM(time_salary) as total_base,
                   SUM(total_deduct) as total_deduct,
                   SUM(final_amount) as total_final,
                   COUNT(*) as employee_count
            FROM salary_detail
            GROUP BY year, month
            ORDER BY year DESC, month DESC
        """).fetchall()

        from Services.employee_debt_helpers import get_period_debt_list
        period_map = {
            (p['month'], p['year']): p
            for p in get_period_debt_list(conn, include_paid=True)['periods']
        }
        enriched_summary = []
        for row in summary_data:
            item = dict(row)
            pinfo = period_map.get((int(item['month']), int(item['year'])), {})
            item['con_lai'] = pinfo.get('con_lai', item.get('total_final', 0))
            item['da_nop'] = pinfo.get('da_nop', 0)
            item['is_paid'] = pinfo.get('is_paid', False)
            item['pay_status'] = pinfo.get('status', 'Chưa trả')
            item['pay_status_class'] = pinfo.get('status_class', 'bg-danger-subtle text-danger')
            enriched_summary.append(item)
    
        conn.close()
        return render_template('KeToanHKD/DanhSachBangLuong_05LDTL.html', 
                               summary=enriched_summary, 
                               info=info, 
                               vung_data=vung_list)

    @app.route('/salary/update_config', methods=['POST'])
    @login_required
    def update_salary_config():
        # 1. Lấy dữ liệu từ form (Người lao động và Chủ hộ)
        region = request.form.get('region')
        base_salary = request.form.get('base_salary')
    
        # Tỷ lệ NLĐ (Mặc định)
        r_bhxh = request.form.get('rate_bhxh')
        r_bhyt = request.form.get('rate_bhyt')
        r_bhtn = request.form.get('rate_bhtn')
    
        # Tỷ lệ Chủ hộ
        r_bhxh_chu = request.form.get('rate_bhxh_chu')
        r_bhyt_chu = request.form.get('rate_bhyt_chu')
        r_bhtn_chu = request.form.get('rate_bhtn_chu')
    
        conn = get_db_connection()
        try:
            # 2. Cập nhật tất cả vào bảng business_info
            conn.execute("""
                UPDATE business_info 
                SET salary_region = ?, 
                    base_salary_insurance = ?, 
                    rate_bhxh = ?, 
                    rate_bhyt = ?, 
                    rate_bhtn = ?,
                    rate_bhxh_chu = ?,
                    rate_bhyt_chu = ?,
                    rate_bhtn_chu = ?
            """, (region, base_salary, r_bhxh, r_bhyt, r_bhtn, 
                  r_bhxh_chu, r_bhyt_chu, r_bhtn_chu))
            conn.commit()
            flash("Cấu hình lương và tỷ lệ bảo hiểm đã được cập nhật thành công!", "success")
        except Exception as e:
            conn.rollback()
            flash(f"Lỗi khi cập nhật: {str(e)}", "danger")
        finally:
            conn.close()
        
        # Redirect về trang danh sách bảng lương
        return redirect(url_for('DanhSachBangLuong_05LDTL'))

    # --- TRANG TẠO/LẬP BẢNG LƯƠNG MỚI (Trang nhập liệu) ---#
    @app.route('/salary/create')
    def salary_create():
        month = request.args.get('month', datetime.now().month, type=int)
        year = request.args.get('year', datetime.now().year, type=int)
        return render_template('KeToanHKD/LapBangLuong.html', month=month, year=year)

    # --- API LƯU BẢNG LƯƠNG SAU KHI TẠO/LẬP ---#
    @app.route('/api/save_salary', methods=['POST'])
    @login_required
    def save_salary():
        payload = request.json
        if not payload:
            return jsonify(success=False, message="Dữ liệu không hợp lệ"), 400

        try:
            month = int(payload.get('month'))
            year = int(payload.get('year'))
            date_input = payload.get('date')
            records = payload.get('records', [])
        except (ValueError, TypeError):
            return jsonify(success=False, message="Tháng/Năm hoặc định dạng dữ liệu không đúng"), 400

        if not records:
            return jsonify(success=False, message="Không có dữ liệu nhân viên"), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        try:
            from Services.employee_payroll_helpers import ensure_salary_detail_allowance_columns
            ensure_salary_detail_allowance_columns(conn)
            # Chỉ chốt bảng lương — thanh toán thực hiện riêng qua /api/salary/pay
            cur.execute("DELETE FROM salary_detail WHERE month = ? AND year = ?", (month, year))

            total_amount = 0
            for r in records:
                f_amt = float(r.get('final_amount') or 0)
                total_amount += f_amt

                cur.execute("""
                    INSERT INTO salary_detail (
                        employee_id, fullname, month, year,
                        salary_rate, actual_working_days, time_salary,
                        allowance_fund, allowance_other, bonus,
                        bhxh, bhyt, bhtn, tncn_tax,
                        total_income, total_deduct, final_amount, date
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.get('employee_id'),
                    r.get('fullname'),
                    month, year,
                    float(r.get('base_salary') or 0),
                    float(r.get('actual_working_days') or 0),
                    float(r.get('time_salary') or 0),
                    float(r.get('allowance_fund') or 0),
                    float(r.get('allowance_other') or 0),
                    float(r.get('bonus') or 0),
                    float(r.get('bhxh') or 0),
                    float(r.get('bhyt') or 0),
                    float(r.get('bhtn') or 0),
                    float(r.get('tncn_tax') or 0),
                    float(r.get('total_income') or 0),
                    float(r.get('total_deduct') or 0),
                    f_amt,
                    date_input
                ))

            conn.commit()
            total_amount = round(total_amount, 0)
            return jsonify(
                success=True,
                message=f"Đã chốt bảng lương tháng {month}/{year} ({len(records)} nhân viên, tổng {total_amount:,.0f} ₫). "
                        f"Thanh toán tại Sổ công nợ phải trả nhân viên.",
            )

        except Exception as e:
            conn.rollback()
            return jsonify(success=False, message=f"Lỗi database: {str(e)}"), 500
        finally:
            conn.close()


    # --- API LẤY DỮ LIỆU LƯƠNG THEO THÁNG ---#
    @app.route('/api/get_salary')
    @login_required
    def get_salary():
        # 1. Ép kiểu dữ liệu ngay từ đầu để tránh lỗi query
        try:
            month = int(request.args.get('month', datetime.now().month))
            year = int(request.args.get('year', datetime.now().year))
        except (ValueError, TypeError):
            return jsonify(success=False, message="Tháng hoặc năm không hợp lệ"), 400
    
        # Lấy số ngày công chuẩn của tháng
        standard_days = get_working_days_exclude_sun(month, year)

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

        from Services.attendance_helpers import get_monthly_work_days_map
        from Services.employee_payroll_helpers import ensure_payroll_schema
        ensure_payroll_schema(conn)
        attendance_days = get_monthly_work_days_map(conn, month, year)
    
        # 2. Query kết hợp: Lấy thông tin gốc từ employees và dữ liệu đã lưu từ salary_detail
        # Sử dụng COALESCE để xử lý các giá trị Null ngay từ tầng Database nếu muốn, 
        # hoặc xử lý trong Python như dưới đây.
        query = """
            SELECT
                e.id as employee_id, e.fullname, e.salary_rate, e.base_salary,
                COALESCE(e.allowance_fund, 0) AS emp_allowance_fund,
                COALESCE(e.allowance_other, 0) AS emp_allowance_other,
                COALESCE(e.default_bonus, 0) AS emp_default_bonus,
                s.id as salary_id, s.actual_working_days, s.time_salary,
                s.allowance_fund, s.allowance_other, s.bonus,
                s.bhxh, s.bhyt, s.bhtn, s.tncn_tax, s.total_income,
                s.total_deduct, s.final_amount, s.date as record_date
            FROM employees e
            LEFT JOIN salary_detail s ON e.id = s.employee_id AND s.month = ? AND s.year = ?
            WHERE e.status = 1
        """
    
        rows = conn.execute(query, (month, year)).fetchall()
        conn.close()

        data = []
        for row in rows:
            item = dict(row)
        
            # 3. Nếu chưa có dữ liệu lương đã lưu (Salary ID là Null)
            if item.get('salary_id') is None:
                emp_id = item.get('employee_id')
                work_days = attendance_days.get(emp_id, 0) if emp_id else 0
                item['actual_working_days'] = work_days if work_days > 0 else standard_days
                item['attendance_work_days'] = work_days
                item['time_salary'] = item['base_salary']
                item['allowance_fund'] = float(item.get('emp_allowance_fund') or 0)
                item['allowance_other'] = float(item.get('emp_allowance_other') or 0)
                item['bonus'] = float(item.get('emp_default_bonus') or 0)
                item['bhxh'] = 0
                item['bhyt'] = 0
                item['bhtn'] = 0
                item['tncn_tax'] = 0
                item['total_income'] = (
                    item['time_salary'] + item['allowance_fund']
                    + item['allowance_other'] + item['bonus']
                )
                item['total_deduct'] = 0
                item['final_amount'] = item['total_income']

            numeric_fields = [
                'actual_working_days', 'time_salary',
                'allowance_fund', 'allowance_other', 'bonus',
                'bhxh', 'bhyt', 'bhtn', 'tncn_tax',
                'total_income', 'total_deduct', 'final_amount'
            ]
            for field in numeric_fields:
                if item.get(field) is None:
                    item[field] = 0
                else:
                    # Đảm bảo trả về kiểu số để JS tính toán được ngay
                    try:
                        item[field] = float(item[field])
                    except:
                        item[field] = 0
            
            data.append(item)

        return jsonify({
            "metadata": {
                "month": month,
                "year": year,
                "standard_days": standard_days
            },
            "records": data
        })

    # ---HIỂN THỊ TRANG IN BẢNG LƯƠNG (MỞ TAB MỚI) ---#
    @app.route('/print_salary')
    def print_salary():
        month = request.args.get('month', type=int)
        year = request.args.get('year', type=int)
    
        if not month or not year:
            return "Thiếu thông tin tháng/năm", 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

        from Services.employee_payroll_helpers import ensure_salary_detail_allowance_columns
        ensure_salary_detail_allowance_columns(conn)

        # 1. Lấy thông tin doanh nghiệp
        biz_query = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
        business_info = dict(biz_query) if biz_query else {
            'business_name': 'CHƯA CẬP NHẬT TÊN',
            'address': '...',
            'tax_code': '...',
            'representative_name': '...'
        }
    
        # 2. Lấy dữ liệu lương
        salary_data = conn.execute("""
            SELECT e.fullname, e.salary_rate, s.* FROM salary_detail s
            JOIN employees e ON s.employee_id = e.id
            WHERE s.month = ? AND s.year = ?
        """, (month, year)).fetchall()
    
        # 3. Tính toán tổng
        sum_col_5 = sum(row['time_salary'] or 0 for row in salary_data)
        sum_col_11 = sum(row['total_income'] or 0 for row in salary_data)
        sum_col_17 = sum(row['total_deduct'] or 0 for row in salary_data)
        total_sum = sum(row['final_amount'] or 0 for row in salary_data)
    
        total_text = so_thanh_chu(total_sum)
    
        # 4. THIẾT LẬP NGÀY IN MẶC ĐỊNH (Ngày cuối cùng của kỳ lương)
        # calendar.monthrange trả về (ngày đầu tuần, số ngày trong tháng)
        last_day = calendar.monthrange(year, month)[1]
        print_date = {
            'day': last_day,
            'month': month,
            'year': year
        }
    
        conn.close()
    
        return render_template(
            'KeToanHKD/BangLuong_05LDTL_Print.html',
            business_info=business_info,
            items=salary_data,
            month=month,
            year=year,
            sum_column_5=sum_col_5,
            sum_column_11=sum_col_11,
            sum_column_17=sum_col_17,
            total_sum=total_sum,
            total_text=total_text,
            print_date=print_date # Sử dụng biến này thay cho datetime.now()
        )

    def _hkd_business_info():
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    @app.route('/api/hkd/revenue-ledger', methods=['GET'])
    @login_required
    def api_hkd_revenue_ledger():
        start_date = (request.args.get('start') or '').strip()[:10]
        end_date = (request.args.get('end') or '').strip()[:10]
        if not start_date or not end_date:
            return jsonify({'success': False, 'error': 'Thiếu tham số start hoặc end (YYYY-MM-DD)'}), 400
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            from Services.hkd_revenue import fetch_hkd_revenue_ledger
            data = fetch_hkd_revenue_ledger(c, start_date, end_date)
            conn.close()
            return jsonify({'success': True, **data})
        except Exception as e:
            logger.error('api_hkd_revenue_ledger: %s', e, exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    #****** Sổ Chi Tiết Doanh Thu Bán Hàng******#
    @app.route('/SoChiTietDoanhThu')
    def SoChiTietDoanhThu():
        # Chỉ trả về trang HTML trắng, dữ liệu sẽ được load qua AJAX
        return render_template('KeToanHKD/SoChiTietDoanhThu.html')

    # Giả sử bạn có một object config hoặc lấy từ DB
    @app.route('/revenue/print-s1')
    def print_s1_view():
        # 1. Lấy tham số thời gian từ URL
        start_date = request.args.get('start', '')
        end_date = request.args.get('end', '')
    
        # 2. Lấy thông tin cấu hình cửa hàng từ database
        # Hàm này trả về dict có các key: name, address, tax_code, location...
     
        return render_template('KeToanHKD/SoChiTietDoanhThu_print.html', 
                               start=start_date, 
                               end=end_date)

    @app.route('/SoChiTietDoanhThu_S2a')
    def SoChiTietDoanhThu_S2a():
        # Chỉ trả về trang HTML trắng, dữ liệu sẽ được load qua AJAX
        return render_template('KeToanHKD/SoChiTietDoanhThu_S2a.html')

    # Giả sử bạn có một object config hoặc lấy từ DB
    @app.route('/revenue/print-s2a')
    def print_s2a_view():
        # 1. Lấy tham số thời gian từ URL
        start_date = request.args.get('start', '')
        end_date = request.args.get('end', '')
    
        # 2. Lấy thông tin cấu hình cửa hàng từ database
        # Hàm này trả về dict có các key: name, address, tax_code, location...
    
        return render_template('KeToanHKD/SoChiTietDoanhThu_S2a_print.html',
                               start=start_date,
                               end=end_date,
                               info=_hkd_business_info())

    @app.route('/SoChiTietDoanhThu_S2b')
    def SoChiTietDoanhThu_S2b():
        # Chỉ trả về trang HTML trắng, dữ liệu sẽ được load qua AJAX
        return render_template('KeToanHKD/SoChiTietDoanhThu_S2b.html')

    # Giả sử bạn có một object config hoặc lấy từ DB
    @app.route('/revenue/print-s2b')
    def print_s2b_view():
        # 1. Lấy tham số thời gian từ URL
        start_date = request.args.get('start', '')
        end_date = request.args.get('end', '')
    
        # 2. Lấy thông tin cấu hình cửa hàng từ database
        # Hàm này trả về dict có các key: name, address, tax_code, location...
    
        return render_template('KeToanHKD/SoChiTietDoanhThu_S2b_print.html',
                               start=start_date,
                               end=end_date,
                               info=_hkd_business_info())

    #=== Sổ Chi Tiết Doanh Thu và Chi Phí===#
    @app.route('/SoChiTietDoanhThu&ChiPhi_S2c')
    def SoChiTietDoanhThu_ChiPhi_S2c():
        # Chỉ trả về trang HTML trắng, dữ liệu sẽ được load qua AJAX
        return render_template('KeToanHKD/SoChiTietDoanhThu_ChiPhi_s2c.html')


    from dateutil.relativedelta import relativedelta
    @app.route('/api/reports/s2c', methods=['GET'])
    def get_report_s2c():
        start_date = request.args.get('start')
        end_date = request.args.get('end')
        if not start_date or not end_date:
            return jsonify({"error": "Thiếu tham số start hoặc end"}), 400

        conn = None
        try:
            from Services.profit_report_helpers import compute_s2c_report
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            profile = getattr(g, 'tenant_profile', None) or {}
            data = compute_s2c_report(cursor, start_date, end_date, tenant_profile=profile)
            return jsonify(data)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/reports/print-s2c')
    def print_s2c_view():
        start_date = request.args.get('start', '')
        end_date = request.args.get('end', '')

        if not start_date or not end_date:
            return "Thiếu khoảng thời gian start/end", 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            from Services.profit_report_helpers import compute_s2c_report
            profile = getattr(g, 'tenant_profile', None) or {}
            data = compute_s2c_report(cursor, start_date, end_date, tenant_profile=profile)
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            report = {
                'revenue': data['revenue'],
                'total_expenses': data['total_expenses'],
                'tax_tncn': data['tax_tncn'],
                'tax_gtgt': data.get('tax_gtgt', 0),
                'diff': data['diff'],
                'costs': data['costs'],
                'taxes': data.get('taxes'),
                'unissued_invoice_warning': data.get('unissued_invoice_warning'),
            }
            return render_template(
                'KeToanHKD/SoChiTietDoanhThu_ChiPhi_S2c_print.html',
                report=report,
                start=start_date,
                end=end_date,
                start_display=start_dt.strftime('%d/%m/%Y'),
                end_display=end_dt.strftime('%d/%m/%Y'),
            )
        except Exception as e:
            import traceback
            print("LỖI in S2c:", traceback.format_exc())
            return "Lỗi hệ thống khi in S2c-HKD", 500
        finally:
            conn.close()

    #===Kết Thúc Phần Sổ Chi Tiết Doanh Thu====#

    #******Sổ Chi Tiết Hàng Hóa******#
    @app.route('/SoChiTietHangHoa')
    def SoChiTietHangHoa():
        # Lấy thông tin cấu hình cửa hàng
    
        # Lấy danh sách sản phẩm để người dùng chọn trong dropdown
        conn = get_db_connection()
        products = conn.execute('SELECT id, name, unit FROM products ORDER BY name ASC').fetchall()
        conn.close()
    
        # Trỏ về file template theo cấu trúc thư mục của bạn
        return render_template('KeToanHKD/SoChiTietHangHoa.html', 
                                products=products)

    @app.route('/api/SoChiTietHangHoa/S2-HKD')
    def api_s2_data():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
    
        product_id = request.args.get('product_id', type=int)
        start_date = request.args.get('start') 
        end_date = request.args.get('end')
    
        if not product_id or not start_date or not end_date:
            conn.close()
            return jsonify({"error": "Thiếu tham số bắt buộc"}), 400

        try:
            # 1. THÔNG TIN SẢN PHẨM
            product = cur.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if not product:
                return jsonify({"error": "Sản phẩm không tồn tại"}), 404
        
            # 2. TỒN ĐẦU KỲ (Tính chính xác tuyệt đối đến 00:00:00)
            # Sử dụng ABS() và logic chuẩn để tránh sai sót dấu âm dương trong DB
            opening_sql = """
                SELECT 
                    SUM(CASE 
                        WHEN type IN ('import', 'RETURN_SALE', 'DELETE_SALE') THEN ABS(quantity)
                        WHEN type IN ('SALE', 'RETURN_IMPORT', 'DELETE_IMPORT') THEN -ABS(quantity)
                        ELSE 0 END) as qty,
                    SUM(CASE 
                        WHEN type IN ('import', 'RETURN_SALE', 'DELETE_SALE') THEN ABS(quantity) * cost_price
                        WHEN type IN ('SALE', 'RETURN_IMPORT', 'DELETE_IMPORT') THEN -(ABS(quantity) * cost_price)
                        ELSE 0 END) as val
                FROM stock_moves 
                WHERE product_id = ? AND date < ?
            """
            opening_res = cur.execute(opening_sql, (product_id, f"{start_date} 00:00:00")).fetchone()
            bal_qty = float(opening_res['qty'] or 0)
            bal_val = float(opening_res['val'] or 0)

            # 3. GIAO DỊCH TRONG KỲ (Dùng LEFT JOIN để lấy tên đối tác nhanh hơn)
            query = """
                SELECT 
                    sm.*,
                    s.customer_name as sale_cust,
                    sup.name as supplier_name
                FROM stock_moves sm
                LEFT JOIN sale s ON sm.ref_id = s.id AND sm.type = 'SALE'
                LEFT JOIN import i ON sm.ref_id = i.id AND sm.type = 'import'
                LEFT JOIN suppliers sup ON i.supplier_id = sup.id
                WHERE sm.product_id = ? 
                  AND sm.date >= ? AND sm.date <= ?
                ORDER BY sm.date ASC, sm.id ASC
            """
            rows = cur.execute(query, (product_id, f"{start_date} 00:00:00", f"{end_date} 23:59:59")).fetchall()

            details = []
            sum_in_qty = sum_in_val = sum_out_qty = sum_out_val = 0.0
            pn_counter = px_counter = 1
        
            for r in rows:
                # Luôn dùng trị tuyệt đối để tính toán chủ động theo move_type
                raw_qty = abs(float(r['quantity'] or 0))
                price = float(r['cost_price'] or 0)
                amount = round(raw_qty * price, 2)
                m_type = r['type']

                # Xác định Note
                if m_type == 'import':
                    display_note = f"Nhập hàng từ {r['supplier_name'] or 'NCC'}"
                elif m_type == 'SALE':
                    display_note = f"Bán hàng cho {r['sale_cust'] or 'Bán cho người tiêu dùng'}"
                else:
                    note_map = {
                        'RETURN_SALE': 'Khách trả hàng',
                        'DELETE_SALE': 'Khách trả hàng (Xóa đơn)',
                        'RETURN_IMPORT': 'Trả hàng cho NCC',
                        'DELETE_IMPORT': 'Trả hàng (Xóa nhập)'
                    }
                    display_note = note_map.get(m_type, m_type)

                # Phân loại Nhập/Xuất
                if m_type in ('import', 'RETURN_SALE', 'DELETE_SALE'):
                    in_qty, in_val = raw_qty, amount
                    out_qty, out_val = 0, 0
                    bal_qty += raw_qty
                    bal_val += amount
                    sum_in_qty += raw_qty
                    sum_in_val += amount
                    ref_no = f"PN{str(pn_counter).zfill(6)}"
                    pn_counter += 1
                else:
                    in_qty, in_val = 0, 0
                    out_qty, out_val = raw_qty, amount
                    bal_qty -= raw_qty
                    bal_val -= amount
                    sum_out_qty += raw_qty
                    sum_out_val += amount
                    ref_no = f"PX{str(px_counter).zfill(6)}"
                    px_counter += 1

                details.append({
                    "ref": ref_no,
                    "date": str(r['date']),
                    "note": display_note,
                    "price": price,
                    "in_qty": in_qty,
                    "in_val": in_val,
                    "out_qty": out_qty,
                    "out_val": out_val,
                    "bal_qty": round(bal_qty, 3),
                    "bal_val": round(bal_val, 2)
                })

            return jsonify({
                "opening": {"qty": round(bal_qty - sum_in_qty + sum_out_qty, 3), "val": round(bal_val - sum_in_val + sum_out_val, 2)},
                "details": details,
                "summary": {
                    "in_qty": sum_in_qty, "in_val": sum_in_val,
                    "out_qty": sum_out_qty, "out_val": sum_out_val,
                    "closing_qty": round(bal_qty, 3), "closing_val": round(bal_val, 2)
                }
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()


    #====== XEM VÀ IN Sổ Chi Tiết Hàng Hóa======#
    from datetime import datetime

    from datetime import datetime

    @app.route('/report/print-S2-HKD')
    def print_S2_HKD():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        product_id = request.args.get('product_id', type=int)
        start = request.args.get('start')  # YYYY-MM-DD
        end = request.args.get('end')      # YYYY-MM-DD

        if not product_id or not start or not end:
            return "Thiếu tham số bắt buộc", 400

        try:
            # 1. Lấy thông tin sản phẩm
            product = cur.execute(
                "SELECT id, name, unit FROM products WHERE id = ?",
                (product_id,)
            ).fetchone()
        
            if not product:
                return "Sản phẩm không tồn tại", 404

            # 2. Tính tồn đầu kỳ (Sử dụng logic ABS để chuẩn hóa dấu)
            opening_sql = """
                SELECT
                    SUM(CASE 
                        WHEN type IN ('import', 'RETURN_SALE', 'DELETE_SALE') THEN ABS(quantity)
                        WHEN type IN ('SALE', 'RETURN_IMPORT', 'DELETE_IMPORT') THEN -ABS(quantity)
                        ELSE 0 END) AS qty,
                    SUM(CASE 
                        WHEN type IN ('import', 'RETURN_SALE', 'DELETE_SALE') THEN ABS(quantity) * cost_price
                        WHEN type IN ('SALE', 'RETURN_IMPORT', 'DELETE_IMPORT') THEN -(ABS(quantity) * cost_price)
                        ELSE 0 END) AS val
                FROM stock_moves
                WHERE product_id = ? AND date < ?
            """
            op_row = cur.execute(opening_sql, (product_id, f"{start} 00:00:00")).fetchone()
        
            start_qty = float(op_row['qty'] or 0)
            start_val = float(op_row['val'] or 0)

            # 3. Lấy dữ liệu giao dịch chi tiết (Sử dụng JOIN để tối ưu tốc độ và lấy đủ danh sách)
            main_query = """
                SELECT 
                    sm.id, sm.type, sm.date, sm.quantity, sm.cost_price, sm.note as sm_note,
                    s.customer_name,
                    sup.name as supplier_name
                FROM stock_moves sm
                LEFT JOIN sale s ON (sm.ref_id = s.id AND sm.type = 'SALE')
                LEFT JOIN import i ON (sm.ref_id = i.id AND sm.type = 'import')
                LEFT JOIN suppliers sup ON (i.supplier_id = sup.id)
                WHERE sm.product_id = ? 
                  AND sm.date >= ? AND sm.date <= ?
                ORDER BY sm.date ASC, sm.id ASC
            """
            rows = cur.execute(main_query, (product_id, f"{start} 00:00:00", f"{end} 23:59:59")).fetchall()

            # 4. Xử lý logic hiển thị
            details = []
            curr_qty, curr_val = start_qty, start_val
            s_in_q = s_in_v = s_out_q = s_out_v = 0.0
            pn_c = px_c = 1

            for r in rows:
                q = abs(float(r['quantity'] or 0))
                p = float(r['cost_price'] or 0)
                amt = round(q * p, 2)
                t = r['type']

                # Diễn giải
                if t == 'import': 
                    note = f"Nhập hàng từ {r['supplier_name'] or 'Nhà cung cấp'}"
                elif t == 'SALE': 
                    note = f"Bán hàng cho {r['customer_name'] or 'Bán cho người tiêu dùng'}"
                else: 
                    note_map = {
                        'RETURN_SALE': 'Khách trả hàng',
                        'DELETE_SALE': 'Khách trả hàng (Xóa đơn)',
                        'RETURN_IMPORT': 'Trả hàng cho NCC',
                        'DELETE_IMPORT': 'Trả hàng (Xóa nhập)'
                    }
                    note = note_map.get(t, r['sm_note'] or t)

                # Phân loại Nhập/Xuất và tính lũy kế Tồn
                if t in ('import', 'RETURN_SALE', 'DELETE_SALE'):
                    in_q, in_v, out_q, out_v = q, amt, 0, 0
                    curr_qty += q
                    curr_val += amt
                    s_in_q += q
                    s_in_v += amt
                    ref_no = f"PN{str(pn_c).zfill(6)}"
                    pn_c += 1
                else:
                    in_q, in_v, out_q, out_v = 0, 0, q, amt
                    curr_qty -= q
                    curr_val -= amt
                    s_out_q += q
                    s_out_v += amt
                    ref_no = f"PX{str(px_c).zfill(6)}"
                    px_c += 1

                # Định dạng ngày hiển thị VN
                dt = datetime.strptime(r['date'][:10], '%Y-%m-%d')
                fmt_date = dt.strftime('%d/%m/%Y')

                details.append({
                    "ref": ref_no,
                    "date": fmt_date,
                    "note": note,
                    "price": p,
                    "in_qty": in_q, "in_val": in_v,
                    "out_qty": out_q, "out_val": out_v,
                    "bal_qty": round(curr_qty, 3), 
                    "bal_val": round(curr_val, 2)
                })

            summary = {
                "in_qty": s_in_q, "in_val": s_in_v,
                "out_qty": s_out_q, "out_val": s_out_v,
                "closing_qty": round(curr_qty, 3), 
                "closing_val": round(curr_val, 2)
            }

            today = datetime.now()
            return render_template(
                'KeToanHKD/SoChiTietHangHoa_print.html',
                product=product,
                opening={"qty": start_qty, "val": start_val},
                details=details,
                summary=summary,
                start_date=datetime.strptime(start, '%Y-%m-%d').strftime('%d/%m/%Y'),
                end_date=datetime.strptime(end, '%Y-%m-%d').strftime('%d/%m/%Y'),
                current_day=today.day,
                current_month=today.month,
                current_year=today.year
            )

        except Exception as e:
            print(f"Error S2-HKD Print: {e}")
            return f"Lỗi hệ thống: {str(e)}", 500
        finally:
            conn.close()

    # ==== CHI PHÍ SẢN XUẤT KINH DOANH====#
    @app.route('/SoChiPhiSXKD')
    def SoChiPhiSXKD():
        return render_template('KeToanHKD/SoChiPhiSXKD.html')

    @app.route('/api/reports/s3-hkd-data', methods=['GET'])
    def get_s3_report():
        start_str = request.args.get('start', '')
        end_str = request.args.get('end', '')

        # Xử lý ngày tháng an toàn hơn
        try:
            if not start_str or not end_str:
                # Nếu trống, lấy mặc định từ đầu tháng đến hiện tại
                today = datetime.now()
                start_date = today.replace(day=1).strftime('%Y-%m-%d')
                end_date = today.strftime('%Y-%m-%d')
            else:
                start_date = datetime.strptime(start_str, '%d/%m/%Y').strftime('%Y-%m-%d')
                end_date = datetime.strptime(end_str, '%d/%m/%Y').strftime('%Y-%m-%d')
        except Exception as e:
            print(f"Lỗi parse ngày: {e}")
            return jsonify({"error": "Định dạng ngày phải là dd/mm/yyyy"}), 400

        query = """
            SELECT 
                MAX(id) as id,
                date,
                voucher_no,
                reason,
                SUM(CASE WHEN expense_type = 'CP_LUONG' THEN amount ELSE 0 END) as cp_luong,
                SUM(CASE WHEN expense_type = 'CP_DIEN'  THEN amount ELSE 0 END) as cp_dien,
                SUM(CASE WHEN expense_type = 'CP_NUOC'  THEN amount ELSE 0 END) as cp_nuoc,
                SUM(CASE WHEN expense_type = 'CP_VT'    THEN amount ELSE 0 END) as cp_vt,
                SUM(CASE WHEN expense_type = 'CP_MB'    THEN amount ELSE 0 END) as cp_mb,
                SUM(CASE WHEN expense_type = 'CP_VPP'   THEN amount ELSE 0 END) as cp_vpp,
                SUM(CASE WHEN expense_type = 'CP_KHAC'  THEN amount ELSE 0 END) as cp_khac
            FROM phieu_chi
            WHERE date(date) BETWEEN ? AND ?
              AND expense_type IN ('CP_LUONG', 'CP_DIEN', 'CP_NUOC', 'CP_VT', 'CP_MB', 'CP_VPP', 'CP_KHAC')
            GROUP BY voucher_no
            ORDER BY date ASC
        """
    
        conn = get_db_connection()
        # Đảm bảo row_factory được thiết lập để truy cập theo tên cột
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        cursor.execute(query, (start_date, end_date))
        rows = cursor.fetchall()
        conn.close()

        report_data = []
        for row in rows:
            # Tính tổng để lọc dòng trắng
            row_total = (row['cp_luong'] + row['cp_dien'] + row['cp_nuoc'] + 
                         row['cp_vt'] + row['cp_mb'] + row['cp_vpp'] + row['cp_khac'])
        
            if row_total > 0:
                try:
                    # Lấy 10 ký tự đầu YYYY-MM-DD
                    display_date = datetime.strptime(row['date'][:10], '%Y-%m-%d').strftime('%d/%m/%Y')
                except:
                    display_date = row['date']

                report_data.append({
                    "id": row['id'],
                    "ngay_phieu": display_date,
                    "so_phieu": row['voucher_no'],
                    "noi_dung": row['reason'],
                    "cp_luong": row['cp_luong'],
                    "cp_dien": row['cp_dien'],
                    "cp_nuoc": row['cp_nuoc'],
                    "cp_vt": row['cp_vt'],
                    "cp_mb": row['cp_mb'],
                    "cp_ql": row['cp_vpp'],   # Cột 7
                    "cp_khac": row['cp_khac'] # Cột 8
                })

        return jsonify(report_data)

    @app.route('/report/s3-print')
    def s3_print_view():
        start_str = request.args.get('start')
        end_str = request.args.get('end')

        try:
            # Tái sử dụng logic lấy dữ liệu
            start_sql = datetime.strptime(start_str, '%d/%m/%Y').strftime('%Y-%m-%d')
            end_sql = datetime.strptime(end_str, '%d/%m/%Y').strftime('%Y-%m-%d')
        
            conn = get_db_connection()
            query = """
                SELECT 
                    date, voucher_no, reason,
                    SUM(CASE WHEN expense_type = 'CP_LUONG' THEN amount ELSE 0 END) as cp_luong,
                    SUM(CASE WHEN expense_type = 'CP_DIEN'  THEN amount ELSE 0 END) as cp_dien,
                    SUM(CASE WHEN expense_type = 'CP_NUOC'  THEN amount ELSE 0 END) as cp_nuoc,
                    SUM(CASE WHEN expense_type = 'CP_VT'    THEN amount ELSE 0 END) as cp_vt,
                    SUM(CASE WHEN expense_type = 'CP_MB'    THEN amount ELSE 0 END) as cp_mb,
                    SUM(CASE WHEN expense_type = 'CP_VPP'   THEN amount ELSE 0 END) as cp_vpp,
                    SUM(CASE WHEN expense_type = 'CP_KHAC'  THEN amount ELSE 0 END) as cp_khac
                FROM phieu_chi
                WHERE date(date) BETWEEN ? AND ?
                  AND expense_type IN ('CP_LUONG', 'CP_DIEN', 'CP_NUOC', 'CP_VT', 'CP_MB', 'CP_VPP', 'CP_KHAC')
                GROUP BY voucher_no
                ORDER BY date ASC
            """
            rows = conn.execute(query, (start_sql, end_sql)).fetchall()
            conn.close()

            items = []
            totals = {'labor':0, 'electric':0, 'water':0, 'telecom':0, 'rent':0, 'manage':0, 'other':0, 'grand':0}

            for r in rows:
                it = {
                    "ngay_phieu": datetime.strptime(r['date'][:10], '%Y-%m-%d').strftime('%d/%m/%Y'),
                    "so_phieu": r['voucher_no'],
                    "noi_dung": r['reason'],
                    "cp_luong": r['cp_luong'], "cp_dien": r['cp_dien'], "cp_nuoc": r['cp_nuoc'],
                    "cp_vt": r['cp_vt'], "cp_mb": r['cp_mb'], "cp_ql": r['cp_vpp'], "cp_khac": r['cp_khac']
                }
                items.append(it)
                # Tính tổng các cột
                totals['labor'] += it['cp_luong']
                totals['electric'] += it['cp_dien']
                totals['water'] += it['cp_nuoc']
                totals['telecom'] += it['cp_vt']
                totals['rent'] += it['cp_mb']
                totals['manage'] += it['cp_ql']
                totals['other'] += it['cp_khac']
        
            totals['grand'] = sum([v for k,v in totals.items() if k != 'grand'])

            # Tách ngày/tháng/năm từ ngày cuối kỳ để làm ngày ký tên
            day, month, year = '', '', ''
            if end_str:
                parts = end_str.split('/')
                day, month, year = parts[0], parts[1], parts[2]

            return render_template('KeToanHKD/SoChiPhiSXKD_print.html', 
                                   items=items, 
                                   totals=totals, 
                                   day=day, month=month, year=year,
                                   date_range_label=f"Từ ngày {start_str} đến ngày {end_str}")
        except Exception as e:
            return f"Lỗi hệ thống: {str(e)}", 500

    @app.route('/SoTheoDoiNSNN')
    def SoTheoDoiNSNN():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        info = dict(conn.execute("SELECT * FROM business_info LIMIT 1").fetchone() or {})
        conn.close()
        return render_template('KeToanHKD/SoTheoDoiNSNN.html', info=info)

    @app.route('/api/nsnn/report', methods=['GET'])
    def api_nsnn_report():
        start_iso = (request.args.get('start') or '').strip()
        end_iso = (request.args.get('end') or '').strip()
        business_group = request.args.get('group', '3')
        if not start_iso or not end_iso:
            return jsonify({'error': 'Thiếu tham số start/end'}), 400
        try:
            datetime.strptime(start_iso, '%Y-%m-%d')
            datetime.strptime(end_iso, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Định dạng ngày không hợp lệ'}), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.nsnn_report_helpers import build_nsnn_report
            report = build_nsnn_report(conn.cursor(), start_iso, end_iso, business_group)
            return jsonify({'success': True, **report})
        except Exception as e:
            logger.exception('api_nsnn_report failed')
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/nsnn/pay', methods=['POST'])
    def api_nsnn_pay():
        data = request.get_json() or {}
        tax_type = (data.get('tax_type') or '').strip().upper()
        start_iso = (data.get('start') or '').strip()
        end_iso = (data.get('end') or '').strip()
        business_group = data.get('group', '3')
        amount = float(data.get('amount') or 0)
        pay_date = (data.get('pay_date') or '').strip()
        receiver_name = (data.get('receiver') or '').strip()
        reason = (data.get('reason') or '').strip()
        credit_account = (data.get('pay_method') or '111').strip()
        debit_account = (data.get('debit_account') or '333').strip()

        if tax_type not in ('GTGT', 'TNCN'):
            return jsonify({'success': False, 'error': 'Loại thuế không hợp lệ'}), 400
        if amount <= 0 or not pay_date or not receiver_name or not reason:
            return jsonify({'success': False, 'error': 'Vui lòng nhập đầy đủ các trường bắt buộc (*)'}), 400
        if not start_iso or not end_iso:
            return jsonify({'success': False, 'error': 'Thiếu kỳ báo cáo'}), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            from Services.nsnn_report_helpers import build_nsnn_report, nsnn_reference_key
            report = build_nsnn_report(c, start_iso, end_iso, business_group)
            row = next((r for r in report['rows'] if r['tax_type'] == tax_type), None)
            if not row:
                return jsonify({'success': False, 'error': 'Không tìm thấy nghĩa vụ thuế'}), 404
            if amount > float(row['con_lai']) + 0.01:
                return jsonify({'success': False, 'error': 'Số tiền vượt quá số còn phải nộp'}), 400

            c.execute(
                "SELECT voucher_no FROM phieu_chi WHERE voucher_no LIKE 'PC%' ORDER BY id DESC LIMIT 1"
            )
            last = c.fetchone()
            if last and last['voucher_no']:
                try:
                    new_pc_no = f"PC{int(last['voucher_no'][2:]) + 1:06d}"
                except ValueError:
                    new_pc_no = 'PC000001'
            else:
                new_pc_no = 'PC000001'

            ref_key = nsnn_reference_key(start_iso, end_iso, tax_type)
            c.execute(
                """
                INSERT INTO phieu_chi (
                    voucher_no, receiver_name, amount, credit_account, debit_account,
                    reason, source_type, expense_type, reference_document, date, preparer
                ) VALUES (?, ?, ?, ?, ?, ?, 'nsnn_tax', 'CP_TAX', ?, ?, ?)
                """,
                (
                    new_pc_no, receiver_name, amount, credit_account, debit_account,
                    reason, ref_key, pay_date, session.get('user_name', 'Admin'),
                ),
            )
            conn.commit()
            return jsonify({
                'success': True,
                'message': 'Đã lập phiếu chi nộp thuế',
                'voucher': new_pc_no,
            })
        except Exception as e:
            conn.rollback()
            logger.exception('api_nsnn_pay failed')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    #==== Sổ Theo Dõi Tiền Lương===#
    @app.route('/update_insurance_rates', methods=['POST'])
    @login_required
    def update_insurance_rates():
        # Nhận số nguyên trực tiếp (ví dụ: 8, 17.5)
        data = {
            'rate_bhxh': request.form.get('rate_bhxh'),
            'rate_bhyt': request.form.get('rate_bhyt'),
            'rate_bhtn': request.form.get('rate_bhtn'),
            'rate_bhxh_chu': request.form.get('rate_bhxh_chu'),
            'rate_bhyt_chu': request.form.get('rate_bhyt_chu'),
            'rate_bhtn_chu': request.form.get('rate_bhtn_chu')
        }
    
        conn = get_db_connection()
        conn.execute("""
            UPDATE business_info SET 
            rate_bhxh=?, rate_bhyt=?, rate_bhtn=?,
            rate_bhxh_chu=?, rate_bhyt_chu=?, rate_bhtn_chu=?
        """, (data['rate_bhxh'], data['rate_bhyt'], data['rate_bhtn'],
              data['rate_bhxh_chu'], data['rate_bhyt_chu'], data['rate_bhtn_chu']))
        conn.commit()
        conn.close()
    
        flash("Đã cập nhật tỷ lệ bảo hiểm mới!", "success")
        return redirect(url_for('SoTheoDoiTienLuong'))


    @app.route('/SoTheoDoiTienLuong')
    @login_required
    def SoTheoDoiTienLuong():
        selected_year = request.args.get('year', default=datetime.today().year, type=int)
        current_year_today = datetime.today().year
    
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            from Services.chu_ho_helpers import sync_chu_ho_from_business_info
            sync_chu_ho_from_business_info(conn, commit=False)
            info = dict(conn.execute("SELECT * FROM business_info LIMIT 1").fetchone() or {})

            r_bhxh_chu = float(info.get('rate_bhxh_chu') or 17.5) / 100
            r_bhyt_chu = float(info.get('rate_bhyt_chu') or 3) / 100
            r_bhtn_chu = float(info.get('rate_bhtn_chu') or 1) / 100
            muc_luong_vung = float(info.get('base_salary_insurance') or 0)

            salary_details = [dict(row) for row in conn.execute(
                """
                SELECT sd.*, COALESCE(e.is_chu_ho, 0) AS is_chu_ho
                FROM salary_detail sd
                LEFT JOIN employees e ON e.id = sd.employee_id
                WHERE sd.year = ?
                """,
                (selected_year,),
            ).fetchall()]

            phieu_chi_all = [dict(row) for row in conn.execute(
                "SELECT * FROM phieu_chi WHERE reason LIKE ?", (f'%{selected_year}%',)
            ).fetchall()]

            du_lieu_thang = []
            du_lieu_chu = []
            luy_ke_no = {'luong': 0, 'bhxh': 0, 'bhyt': 0, 'bhtn': 0}
            tong_phai = {'luong': 0, 'bhxh': 0, 'bhyt': 0, 'bhtn': 0}
            tong_da = {'luong': 0, 'bhxh': 0, 'bhyt': 0, 'bhtn': 0}
            tong_chu = {'bhxh': 0, 'bhyt': 0, 'bhtn': 0, 'tong': 0, 'da_nop': 0, 'con_lai': 0}

            from Services.insurance_debt_helpers import compute_period_insurance_chu

            for month in range(1, 13):
                list_nv = [s for s in salary_details if int(s.get('month')) == month]
                n = len(list_nv)

                phai_tra = {
                    'luong': sum(float(s.get('final_amount') or 0) for s in list_nv),
                    'bhxh': sum(float(s.get('bhxh') or 0) for s in list_nv),
                    'bhyt': sum(float(s.get('bhyt') or 0) for s in list_nv),
                    'bhtn': sum(float(s.get('bhtn') or 0) for s in list_nv),
                }

                chu_ho_count = sum(1 for s in list_nv if int(s.get('is_chu_ho') or 0) == 1)
                chu_bhxh = muc_luong_vung * r_bhxh_chu * n
                chu_bhyt = muc_luong_vung * r_bhyt_chu * n
                chu_bhtn = muc_luong_vung * r_bhtn_chu * (n - chu_ho_count)
                chu_thang = chu_bhxh + chu_bhyt + chu_bhtn

                chu_ins = compute_period_insurance_chu(conn, month, selected_year) if n > 0 else None
                chu_da = {'bhxh': 0.0, 'bhyt': 0.0, 'bhtn': 0.0}
                chu_con = {'bhxh': chu_bhxh, 'bhyt': chu_bhyt, 'bhtn': chu_bhtn}
                chu_status = 'Không phát sinh'
                chu_status_class = 'bg-secondary-subtle text-secondary'
                if chu_ins:
                    for t in chu_ins['types']:
                        k = t['ins_type'].lower()
                        chu_da[k] = float(t['da_nop'] or 0)
                        chu_con[k] = float(t['con_lai'] or 0)
                    chu_status = chu_ins['summary']['status']
                    chu_status_class = chu_ins['summary']['status_class']

                du_lieu_chu.append({
                    'thang': month,
                    'n': n,
                    'chu_ho_count': chu_ho_count,
                    'bhxh': chu_bhxh,
                    'bhyt': chu_bhyt,
                    'bhtn': chu_bhtn,
                    'tong': chu_thang,
                    'da_nop': chu_ins['summary']['total_da_nop'] if chu_ins else 0,
                    'con_lai': chu_ins['summary']['total_con_lai'] if chu_ins else 0,
                    'da_bhxh': chu_da['bhxh'],
                    'da_bhyt': chu_da['bhyt'],
                    'da_bhtn': chu_da['bhtn'],
                    'con_bhxh': chu_con['bhxh'],
                    'con_bhyt': chu_con['bhyt'],
                    'con_bhtn': chu_con['bhtn'],
                    'status': chu_status,
                    'status_class': chu_status_class,
                })
                tong_chu['bhxh'] += chu_bhxh
                tong_chu['bhyt'] += chu_bhyt
                tong_chu['bhtn'] += chu_bhtn
                tong_chu['tong'] += chu_thang
                if chu_ins:
                    tong_chu['da_nop'] += chu_ins['summary']['total_da_nop']
                    tong_chu['con_lai'] += chu_ins['summary']['total_con_lai']

                da_tra = {'luong': 0, 'bhxh': 0, 'bhyt': 0, 'bhtn': 0}
                search_key = f"{month}/{selected_year}"

                for p in phieu_chi_all:
                    reason = (p.get('reason') or '').upper()
                    if search_key in reason:
                        exp_type = p.get('expense_type')
                        if exp_type == 'CP_LUONG' or p.get('source_type') == 'salary':
                            da_tra['luong'] += float(p.get('amount') or 0)
                        elif exp_type == 'CP_BHXH':
                            da_tra['bhxh'] += float(p.get('amount') or 0)
                        elif exp_type == 'CP_BHYT':
                            da_tra['bhyt'] += float(p.get('amount') or 0)
                        elif exp_type == 'CP_BHTN':
                            da_tra['bhtn'] += float(p.get('amount') or 0)

                for k in luy_ke_no:
                    tong_phai[k] += phai_tra[k]
                    tong_da[k] += da_tra[k]
                    luy_ke_no[k] += (phai_tra[k] - da_tra[k])

                if n > 0 or any(v > 0 for v in da_tra.values()):
                    last_day = calendar.monthrange(selected_year, month)[1]
                    du_lieu_thang.append({
                        'thang': month,
                        'so_hieu': f"L{month:02d}{selected_year}",
                        'ngay_ghi_so': f"{last_day:02d}/{month:02d}/{selected_year}",
                        'phai_tra': phai_tra.copy(),
                        'da_tra': da_tra.copy(),
                        'con_no': luy_ke_no.copy(),
                        'n': n
                    })

            cuoi_ky_tong = sum(luy_ke_no.values())

        return render_template('KeToanHKD/SoTheoDoiTienLuong.html',
                               info=info,
                               du_lieu_thang=du_lieu_thang,
                               du_lieu_chu=du_lieu_chu,
                               tong_phai=tong_phai,
                               tong_da=tong_da,
                               tong_chu=tong_chu,
                               cuoi_ky_tong=cuoi_ky_tong,
                               total_text=so_thanh_chu(int(cuoi_ky_tong)),
                               year=selected_year,
                               current_year_today=current_year_today,
                               now=datetime.now())

    @app.route('/SoQuyTienMat')
    def SoQuyTienMat():
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
            'KeToanHKD/SoQuyTienMat.html',
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

    @app.route('/SoTienGuiNganHang')
    @login_required
    def SoTienGuiNganHang():
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
            'KeToanHKD/SoTienGuiNganHang.html',
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

    @app.route('/api/quy-so-du', methods=['GET'])
    def api_quy_so_du():
        conn = get_db_connection()
        try:
            # Tính tất cả trong 1 lần query
            row = conn.execute("""
                SELECT 
                    COALESCE(SUM(CASE WHEN debit_account LIKE '111%' THEN amount ELSE 0 END), 0) AS debit_111,
                    COALESCE(SUM(CASE WHEN credit_account LIKE '111%' THEN amount ELSE 0 END), 0) AS credit_111,
                    COALESCE(SUM(CASE WHEN debit_account LIKE '112%' THEN amount ELSE 0 END), 0) AS debit_112,
                    COALESCE(SUM(CASE WHEN credit_account LIKE '112%' THEN amount ELSE 0 END), 0) AS credit_112
                FROM (
                    SELECT debit_account, credit_account, amount FROM phieu_thu
                    UNION ALL
                    SELECT debit_account, credit_account, amount FROM phieu_chi
                ) all_transactions
            """).fetchone()

            so_du_tien_mat  = row['debit_111'] - row['credit_111']
            so_du_ngan_hang = row['debit_112'] - row['credit_112']

            return jsonify({
                'success': True,
                'so_du_tien_mat': round(float(so_du_tien_mat), 2),
                'so_du_ngan_hang': round(float(so_du_ngan_hang), 2),
                'timestamp': datetime.now().isoformat()
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    #===API tính số dư tiền mặt, tiền gửi ngân hàng trước đây tại ngày lập phiếu Chi để đảm bảo khi sửa phiếu chi không bị vượt số dư===#
    @app.route('/api/quy-so-du-at-date', methods=['GET'])
    def api_quy_so_du_at_date():
        date_str = request.args.get('date')      # Ví dụ: '2026-02-06'
        account = request.args.get('account', '111')  # '111' hoặc '112'

        if not date_str:
            return jsonify({'success': False, 'error': 'Vui lòng cung cấp ngày'}), 400

        try:
            # Ngày cần tính số dư (đến 23:59:59 ngày đó)
            target_date = datetime.strptime(date_str, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59, microsecond=999999,
                tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")
            )
            target_date_utc = target_date.astimezone(ZoneInfo("UTC"))  # Nếu DB lưu UTC

            conn = get_db_connection()
            try:
                # Tính tổng thu (debit) đến trước hoặc bằng ngày lập phiếu
                debit_thu = conn.execute("""
                    SELECT COALESCE(SUM(amount), 0)
                    FROM phieu_thu
                    WHERE debit_account LIKE ? AND date <= ?
                """, (f'{account}%', target_date_utc)).fetchone()[0]

                # Tính tổng chi (credit) đến trước hoặc bằng ngày lập phiếu
                credit_chi = conn.execute("""
                    SELECT COALESCE(SUM(amount), 0)
                    FROM phieu_chi
                    WHERE credit_account LIKE ? AND date <= ?
                """, (f'{account}%', target_date_utc)).fetchone()[0]

                so_du = debit_thu - credit_chi

                return jsonify({
                    'success': True,
                    'so_du': round(float(so_du), 2),
                    'account': account,
                    'at_date': date_str
                })
            finally:
                conn.close()
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    #==== ROUTE HIỂN THỊ VÀ API LẤY DANH SÁCH CÔNG NỢ PHẢI THU===#
    @app.route('/SoCongNoPhaiThu')
    def SoCongNoPhaiThu():
        return render_template('KeToanHKD/SoCongNoPhaiThu.html')

    #=== API LẤY DANH SÁCH ĐƠN HÀNG CÒN NỢ THEO KHÁCH HÀNG===#
    @app.route("/api/debt/customer-detail")
    def api_debt_customer_detail():
        customer_name = request.args.get("customer")
    
        if not customer_name:
            return jsonify(success=False, error="Thiếu tên khách hàng"), 400

        db = get_db_connection()
        db.row_factory = sqlite3.Row  # Đảm bảo trả về dạng dict-like
        cur = db.cursor()

        try:
            # ── Lấy DANH SÁCH CÁC KHOẢN CÒN NỢ + JOIN LẤY COMPANY_NAME ──
            # Fix: Thêm s.company_name từ bảng sale
            sql_records = """
                SELECT
                    cn.debt_id,
                    cn.sale_no,
                    cn.date_of_debt,
                    cn.customer_name,
                    cn.sale_id,
                    s.company_name,
                    cn.unpaid_amount     AS total_debt,
                    cn.paid_amount       AS paid_amount,
                    cn.remaining_amount  AS remaining
                FROM cong_no cn
                LEFT JOIN sale s ON cn.sale_id = s.id
                WHERE cn.customer_name = ?
                  AND cn.remaining_amount > 0
                ORDER BY cn.date_of_debt ASC, cn.debt_id ASC
            """
            records_rows = cur.execute(sql_records, [customer_name]).fetchall()
            records = [dict(row) for row in records_rows]

            # ── Summary: Tổng hợp toàn bộ nợ của khách hàng này ──
            sql_summary = """
                SELECT 
                    COALESCE(SUM(unpaid_amount), 0)    AS total_debt_all,
                    COALESCE(SUM(paid_amount), 0)      AS total_paid_all,
                    COALESCE(SUM(remaining_amount), 0)  AS current_remaining
                FROM cong_no
                WHERE customer_name = ?
            """
            summary_row = cur.execute(sql_summary, [customer_name]).fetchone()

            summary = {
                "total": float(summary_row["total_debt_all"]),
                "paid": float(summary_row["total_paid_all"]),
                "remaining": float(summary_row["current_remaining"])
            }

            return jsonify(
                success=True,
                summary=summary,
                records=records
            )
        except Exception as e:
            return jsonify(success=False, error=str(e)), 500
        finally:
            db.close()

    #=== ROUTE IN TTRANG CHI TIẾT CÔNG NỢ PHẢI THU CỦA KHÁCH HÀNG===#
    from flask import render_template, request, session
    from datetime import datetime
    import sqlite3

    @app.route("/customer_debt/print-ledger")
    def print_debt_ledger():
        customer = request.args.get("customer")
        start = request.args.get("start")
        end = request.args.get("end")

        if not customer:
            return "Lỗi: Phải chọn khách hàng!", 400

        db = get_db_connection()
        db.row_factory = sqlite3.Row 
        cur = db.cursor()

        # 1. LẤY THÔNG TIN TÊN CÔNG TY (Ưu tiên từ bảng sale)
        sql_info = "SELECT company_name FROM sale WHERE customer_name = ? AND company_name != '' LIMIT 1"
        info_row = cur.execute(sql_info, [customer]).fetchone()
        display_name = info_row['company_name'] if info_row else customer

        # 2. TÍNH SỐ DƯ ĐẦU KỲ
        opening_balance = 0
        if start:
            sql_opening = "SELECT (SUM(unpaid_amount) - SUM(paid_amount)) as balance FROM cong_no WHERE customer_name = ? AND date_of_debt < ?"
            res_opening = cur.execute(sql_opening, [customer, start]).fetchone()
            opening_balance = res_opening['balance'] if res_opening and res_opening['balance'] else 0

        # 3. TRUY VẤN PHÁT SINH
        sql_main = """
            SELECT cn.debt_id, cn.sale_no, cn.date_of_debt,
                   'Nợ tiền mua hàng' as dien_giai,
                   cn.unpaid_amount as no, cn.paid_amount as co,
                   s.company_name
            FROM cong_no cn
            LEFT JOIN sale s ON cn.sale_id = s.id
            WHERE cn.customer_name = ?
        """
        params = [customer]
        if start: sql_main += " AND cn.date_of_debt >= ?"; params.append(start)
        if end: sql_main += " AND cn.date_of_debt <= ?"; params.append(end)
        sql_main += " ORDER BY cn.date_of_debt ASC, cn.debt_id ASC"
    
        rows = cur.execute(sql_main, params).fetchall()

        # 4. CHUẨN HÓA DỮ LIỆU & TÍNH LŨY KẾ (Fix lỗi split triệt để)
        formatted_rows = []
        current_balance = opening_balance
        for r in rows:
            item = dict(r)
        
            # Xử lý ngày tháng an toàn cho Jinja2
            dt_val = item.get('date_of_debt')
            if dt_val:
                if hasattr(dt_val, 'strftime'):
                    item['safe_date'] = dt_val.strftime('%Y-%m-%d')
                else:
                    item['safe_date'] = str(dt_val).split(' ')[0]
            else:
                item['safe_date'] = '—'

            current_balance += (item['no'] or 0) - (item['co'] or 0)
            item['running_balance'] = current_balance
            formatted_rows.append(item)

        now = datetime.now()
    
        # Ở đây tôi giả định biến 'info' đã được bạn lấy từ hàm định nghĩa riêng của bạn
        # Ví dụ: info = get_your_business_info_function()
    
        return render_template(
            "KeToanHKD/SoCongNoPhaiThu_print.html",
            rows=formatted_rows,
            totals={
                "opening": opening_balance,
                "no": sum(r["no"] or 0 for r in rows),
                "co": sum(r["co"] or 0 for r in rows),
                "closing": current_balance
            },
            start=start, 
            end=end, 
            customer=customer,
            display_name=display_name,
            day=now.day, 
            month=now.month, 
            year=now.year
        )

    #====API LỌC TỪNG KHÁCH HÀNG Ở FRONTEND===#
    @app.route('/api/debt/customers')
    @login_required
    def api_debt_customers():
        db = get_db_connection()
        cur = db.cursor()
        # Lấy customer_name duy nhất và company_name tương ứng (nếu có)
        # Join với bảng sale để lấy thông tin công ty
        sql = """
            SELECT DISTINCT cn.customer_name, s.company_name
            FROM cong_no cn
            LEFT JOIN sale s ON cn.sale_id = s.id
            WHERE cn.remaining_amount <> 0
            ORDER BY s.company_name COLLATE NOCASE, cn.customer_name COLLATE NOCASE
        """
        rows = cur.execute(sql).fetchall()
    
        # Chuyển thành danh sách các dictionary
        result = []
        for r in rows:
            result.append({
                "customer_name": r["customer_name"],
                "company_name": r["company_name"] or ""
            })
    
        return jsonify(result)


    @app.route('/SoCongNoPhaiTraNhanVien')
    @login_required
    def SoCongNoPhaiTraNhanVien():
        return render_template('KeToanHKD/SoCongNoPhaiTraNhanVien.html')

    @app.route('/SoCongNoBaoHiem')
    @login_required
    def SoCongNoBaoHiem():
        return render_template('KeToanHKD/SoCongNoBaoHiem.html')

    @app.route('/api/debt/insurance-periods')
    @login_required
    def api_debt_insurance_periods():
        from Services.insurance_debt_helpers import get_insurance_debt_list
        year = request.args.get('year', type=int)
        include_paid = request.args.get('include_paid', '0') == '1'
        conn = get_db_connection()
        try:
            data = get_insurance_debt_list(conn, year=year, include_paid=include_paid)
            return jsonify({'success': True, **data})
        finally:
            conn.close()

    @app.route('/SoCongNoThueNSNN')
    @login_required
    def SoCongNoThueNSNN():
        from datetime import datetime
        from Services.tax_debt_helpers import get_tax_debt_summary
        from Services.tenant_profile import get_current_tenant_profile, REVENUE_TIERS

        profile = get_current_tenant_profile()
        revenue_tier = profile.get('revenue_tier') or 'DT1'
        default_sector = profile.get('default_hkd_sector') or 'G1'
        tier_meta = REVENUE_TIERS.get(revenue_tier, REVENUE_TIERS['DT1'])

        current_year = datetime.today().year
        year_options = list(range(current_year + 1, current_year - 6, -1))
        selected_year = request.args.get('year', type=int) or current_year

        tax_data = None
        tax_error = None
        conn = get_db_connection()
        try:
            tax_data = get_tax_debt_summary(
                conn,
                year=selected_year,
                revenue_tier=revenue_tier,
                default_hkd_sector=default_sector,
                include_paid=True,
            )
        except Exception as e:
            logger.exception('SoCongNoThueNSNN load failed')
            tax_error = str(e)
        finally:
            conn.close()

        summary = (tax_data or {}).get('summary') or {}
        return render_template(
            'KeToanHKD/SoCongNoThueNSNN.html',
            current_year=current_year,
            year_options=year_options,
            selected_year=selected_year,
            revenue_tier=revenue_tier,
            revenue_tier_label=tier_meta['label'],
            default_hkd_sector=default_sector,
            tenant_profile=profile,
            tax_data=tax_data,
            tax_error=tax_error,
            summary=summary,
            nsnn_periods=(tax_data or {}).get('nsnn_periods') or [],
            s3a_items=(tax_data or {}).get('s3a_items') or [],
        )

    @app.route('/api/debt/tax-summary')
    @login_required
    def api_debt_tax_summary():
        from Services.tax_debt_helpers import get_tax_debt_summary
        from Services.tenant_profile import get_current_tenant_profile

        profile = get_current_tenant_profile()
        year = request.args.get('year', type=int)
        include_paid = request.args.get('include_paid', '0') == '1'
        revenue_tier = (request.args.get('revenue_tier') or profile.get('revenue_tier') or 'DT1').strip()
        default_sector = profile.get('default_hkd_sector') or 'G1'
        conn = get_db_connection()
        try:
            data = get_tax_debt_summary(
                conn,
                year=year,
                revenue_tier=revenue_tier,
                default_hkd_sector=default_sector,
                include_paid=include_paid,
            )
            return jsonify({'success': True, **data})
        except Exception as e:
            logger.exception('api_debt_tax_summary failed')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/debt/insurance-period-detail')
    @login_required
    def api_debt_insurance_period_detail():
        from Services.insurance_debt_helpers import compute_period_insurance
        month = request.args.get('month', type=int)
        year = request.args.get('year', type=int)
        if not month or not year:
            return jsonify({'success': False, 'error': 'Thiếu tháng/năm'}), 400
        conn = get_db_connection()
        try:
            data = compute_period_insurance(conn, month, year)
            if not data:
                return jsonify({'success': False, 'error': 'Không có bảng lương kỳ này'}), 404
            return jsonify({'success': True, **data})
        finally:
            conn.close()

    @app.route('/api/insurance/pay', methods=['POST'])
    @login_required
    def api_insurance_pay():
        """Nộp một loại BH (BHXH/BHYT/BHTN) cho kỳ lương."""
        data = request.get_json() or {}
        ins_type = (data.get('ins_type') or '').strip().upper()
        try:
            month = int(data.get('month'))
            year = int(data.get('year'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Tháng/năm không hợp lệ'}), 400

        amount = data.get('amount')
        pay_date = (data.get('pay_date') or '').strip()
        receiver_name = (data.get('receiver') or 'Bảo hiểm Xã hội').strip()
        reason = (data.get('reason') or '').strip()
        credit_account = (data.get('pay_method') or '112').strip()
        debit_account = (data.get('debit_account') or '338').strip()

        from Services.insurance_debt_helpers import (
            EXPENSE_MAP,
            expense_type_for,
            get_period_payer_detail,
            insurance_reference_key,
        )
        from Services.employee_debt_helpers import next_phieu_chi_no

        payer = (data.get('payer') or 'NLD').strip().upper()
        if payer not in ('NLD', 'CHU'):
            payer = 'NLD'

        if ins_type not in EXPENSE_MAP:
            return jsonify({'success': False, 'error': 'Loại bảo hiểm không hợp lệ'}), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            period = get_period_payer_detail(conn, month, year, payer=payer)
            if not period:
                return jsonify({'success': False, 'error': 'Không có bảng lương kỳ này'}), 404
            row = next((t for t in period['types'] if t['ins_type'] == ins_type), None)
            if not row:
                return jsonify({'success': False, 'error': 'Không tìm thấy khoản BH'}), 404
            if float(row['con_lai']) <= 0.01:
                return jsonify({'success': False, 'error': 'Khoản này đã được nộp đủ'}), 400

            pay_amount = float(amount) if amount is not None else float(row['con_lai'])
            if pay_amount <= 0:
                return jsonify({'success': False, 'error': 'Số tiền không hợp lệ'}), 400
            if pay_amount > float(row['con_lai']) + 0.01:
                return jsonify({'success': False, 'error': 'Số tiền vượt quá số còn phải nộp'}), 400
            if not pay_date:
                return jsonify({'success': False, 'error': 'Vui lòng nhập ngày nộp'}), 400

            labels = {'BHXH': 'BHXH', 'BHYT': 'BHYT', 'BHTN': 'BHTN'}
            payer_txt = 'NLĐ' if payer == 'NLD' else 'Chủ hộ'
            if not reason:
                reason = f"Nộp {labels[ins_type]} ({payer_txt}) tháng {month}/{year} ({period['employee_count']} NV)"

            new_pc_no = next_phieu_chi_no(c)
            ref_key = insurance_reference_key(ins_type, month, year, payer=payer)
            exp_type = expense_type_for(ins_type, payer)
            c.execute(
                """
                INSERT INTO phieu_chi (
                    voucher_no, receiver_name, amount, credit_account, debit_account,
                    reason, source_type, expense_type, reference_document, date, preparer
                ) VALUES (?, ?, ?, ?, ?, ?, 'insurance', ?, ?, ?, ?)
                """,
                (
                    new_pc_no, receiver_name, pay_amount, credit_account, debit_account,
                    reason, exp_type, ref_key, pay_date,
                    session.get('user_name', 'Admin'),
                ),
            )
            conn.commit()
            return jsonify({
                'success': True,
                'message': f'Đã lập phiếu chi nộp {labels[ins_type]}',
                'voucher': new_pc_no,
            })
        except Exception as e:
            conn.rollback()
            logger.exception('api_insurance_pay failed')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/insurance/pay-all', methods=['POST'])
    @login_required
    def api_insurance_pay_all():
        """Nộp cả 3 khoản BH còn nợ của kỳ (3 phiếu chi)."""
        data = request.get_json() or {}
        try:
            month = int(data.get('month'))
            year = int(data.get('year'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Tháng/năm không hợp lệ'}), 400

        pay_date = (data.get('pay_date') or '').strip()
        receiver_name = (data.get('receiver') or 'Bảo hiểm Xã hội').strip()
        credit_account = (data.get('pay_method') or '112').strip()
        debit_account = (data.get('debit_account') or '338').strip()

        if not pay_date:
            return jsonify({'success': False, 'error': 'Vui lòng nhập ngày nộp'}), 400

        from Services.insurance_debt_helpers import (
            compute_period_insurance_combined,
            expense_type_for,
            insurance_reference_key,
        )
        from Services.employee_debt_helpers import next_phieu_chi_no

        scope = (data.get('scope') or 'ALL').strip().upper()

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            combined = compute_period_insurance_combined(conn, month, year)
            if not combined:
                return jsonify({'success': False, 'error': 'Không có bảng lương kỳ này'}), 404

            vouchers = []
            pay_queue = []
            for side in ('nld', 'chu'):
                if scope in ('ALL', side.upper(), 'NLD' if side == 'nld' else 'CHU'):
                    block = combined.get(side) or {}
                    payer = 'NLD' if side == 'nld' else 'CHU'
                    payer_txt = 'NLĐ' if payer == 'NLD' else 'Chủ hộ'
                    for row in block.get('types') or []:
                        if float(row.get('con_lai') or 0) <= 0.01:
                            continue
                        pay_queue.append((row, payer, payer_txt))

            for row, payer, payer_txt in pay_queue:
                ins_type = row['ins_type']
                pay_amount = float(row['con_lai'])
                reason = f"Nộp {ins_type} ({payer_txt}) tháng {month}/{year} ({combined['employee_count']} NV)"
                new_pc_no = next_phieu_chi_no(c)
                ref_key = insurance_reference_key(ins_type, month, year, payer=payer)
                exp_type = expense_type_for(ins_type, payer)
                c.execute(
                    """
                    INSERT INTO phieu_chi (
                        voucher_no, receiver_name, amount, credit_account, debit_account,
                        reason, source_type, expense_type, reference_document, date, preparer
                    ) VALUES (?, ?, ?, ?, ?, ?, 'insurance', ?, ?, ?, ?)
                    """,
                    (
                        new_pc_no, receiver_name, pay_amount, credit_account, debit_account,
                        reason, exp_type, ref_key, pay_date,
                        session.get('user_name', 'Admin'),
                    ),
                )
                vouchers.append(new_pc_no)

            if not vouchers:
                return jsonify({'success': False, 'error': 'Kỳ này không còn khoản BH phải nộp'}), 400

            conn.commit()
            return jsonify({
                'success': True,
                'message': f'Đã lập {len(vouchers)} phiếu chi nộp bảo hiểm',
                'vouchers': vouchers,
            })
        except Exception as e:
            conn.rollback()
            logger.exception('api_insurance_pay_all failed')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/debt/employees')
    @login_required
    def api_debt_employees():
        from Services.employee_debt_helpers import get_employee_debt_summary
        conn = get_db_connection()
        try:
            data = get_employee_debt_summary(conn)
            return jsonify({'success': True, 'employees': data})
        finally:
            conn.close()

    @app.route('/api/debt/salary-periods')
    @login_required
    def api_debt_salary_periods():
        from Services.employee_debt_helpers import get_period_debt_list
        include_paid = request.args.get('include_paid', '0') == '1'
        conn = get_db_connection()
        try:
            data = get_period_debt_list(conn, include_paid=include_paid)
            return jsonify({'success': True, **data})
        finally:
            conn.close()

    @app.route('/api/debt/salary-period-detail')
    @login_required
    def api_debt_salary_period_detail():
        from Services.employee_debt_helpers import get_period_debt_detail
        month = request.args.get('month', type=int)
        year = request.args.get('year', type=int)
        if not month or not year:
            return jsonify({'success': False, 'error': 'Thiếu tháng/năm'}), 400
        conn = get_db_connection()
        try:
            data = get_period_debt_detail(conn, month, year)
            if not data:
                return jsonify({'success': False, 'error': 'Không có bảng lương kỳ này'}), 404
            return jsonify({'success': True, **data})
        finally:
            conn.close()

    @app.route('/api/debt/employee-detail')
    @login_required
    def api_debt_employee_detail():
        from Services.employee_debt_helpers import get_employee_debt_detail
        employee_id = request.args.get('employee_id', type=int)
        if not employee_id:
            return jsonify({'success': False, 'error': 'Thiếu mã nhân viên'}), 400
        conn = get_db_connection()
        try:
            data = get_employee_debt_detail(conn, employee_id)
            if not data:
                return jsonify({'success': False, 'error': 'Không tìm thấy nhân viên'}), 404
            return jsonify({'success': True, **data})
        finally:
            conn.close()

    @app.route('/api/salary/pay-period', methods=['POST'])
    @login_required
    def api_salary_pay_period():
        """Trả lương cả kỳ — 1 phiếu chi tổng (luồng mặc định)."""
        from Services.sme.hkd_side_effects import write_hkd_cash_vouchers
        from Services.tenant_profile import get_current_tenant_profile
        if not write_hkd_cash_vouchers(profile=get_current_tenant_profile()):
            return jsonify({
                'success': False,
                'error': 'Tenant SME: dùng /api/sme/payroll/pay hoặc trang Công nợ phải trả nhân viên',
            }), 400

        data = request.get_json() or {}
        try:
            month = int(data.get('month'))
            year = int(data.get('year'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Tháng/năm không hợp lệ'}), 400

        amount = data.get('amount')
        pay_date = (data.get('pay_date') or '').strip()
        receiver_name = (data.get('receiver') or 'Tập thể cán bộ nhân viên').strip()
        reason = (data.get('reason') or '').strip()
        credit_account = (data.get('pay_method') or '112').strip()
        debit_account = (data.get('debit_account') or '334').strip()

        from Services.employee_debt_helpers import (
            get_period_debt_detail,
            next_phieu_chi_no,
            period_reference_key,
        )

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            detail = get_period_debt_detail(conn, month, year)
            if not detail:
                return jsonify({'success': False, 'error': 'Không có bảng lương kỳ này'}), 404

            period = detail['period']
            con_lai = float(period['con_lai'])
            if con_lai <= 0.01:
                return jsonify({'success': False, 'error': 'Kỳ lương này đã được thanh toán đủ'}), 400

            pay_amount = float(amount) if amount is not None else con_lai
            if pay_amount <= 0:
                return jsonify({'success': False, 'error': 'Số tiền không hợp lệ'}), 400
            if pay_amount > con_lai + 0.01:
                return jsonify({'success': False, 'error': 'Số tiền vượt quá số còn phải trả của kỳ'}), 400
            if not pay_date:
                return jsonify({'success': False, 'error': 'Vui lòng nhập ngày chi trả'}), 400

            n = period['employee_count']
            if not reason:
                reason = f"Thanh toán lương tháng {month}/{year} ({n} nhân viên)"

            new_pc_no = next_phieu_chi_no(c)
            ref_key = period_reference_key(month, year)
            c.execute(
                """
                INSERT INTO phieu_chi (
                    voucher_no, receiver_name, amount, credit_account, debit_account,
                    reason, source_type, expense_type, reference_document, date, preparer
                ) VALUES (?, ?, ?, ?, ?, ?, 'salary', 'CP_LUONG', ?, ?, ?)
                """,
                (
                    new_pc_no, receiver_name, pay_amount, credit_account, debit_account,
                    reason, ref_key, pay_date, session.get('user_name', 'Admin'),
                ),
            )
            conn.commit()
            return jsonify({
                'success': True,
                'message': f'Đã lập phiếu chi trả lương cả kỳ T{month}/{year}',
                'voucher': new_pc_no,
            })
        except Exception as e:
            conn.rollback()
            logger.exception('api_salary_pay_period failed')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/salary/pay', methods=['POST'])
    @login_required
    def api_salary_pay():
        """Trả lương lẻ từng nhân viên (trường hợp đặc biệt)."""
        from Services.sme.hkd_side_effects import write_hkd_cash_vouchers
        from Services.tenant_profile import get_current_tenant_profile
        if not write_hkd_cash_vouchers(profile=get_current_tenant_profile()):
            return jsonify({
                'success': False,
                'error': 'Tenant SME: dùng /api/sme/payroll/pay-employee hoặc trang Công nợ phải trả nhân viên',
            }), 400

        data = request.get_json() or {}
        employee_id = data.get('employee_id')
        month = data.get('month')
        year = data.get('year')
        amount = float(data.get('amount') or 0)
        pay_date = (data.get('pay_date') or '').strip()
        receiver_name = (data.get('receiver') or '').strip()
        reason = (data.get('reason') or '').strip()
        credit_account = (data.get('pay_method') or '111').strip()
        debit_account = (data.get('debit_account') or '334').strip()

        try:
            employee_id = int(employee_id)
            month = int(month)
            year = int(year)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Dữ liệu nhân viên/kỳ lương không hợp lệ'}), 400

        if amount <= 0 or not pay_date or not receiver_name or not reason:
            return jsonify({'success': False, 'error': 'Vui lòng nhập đầy đủ các trường bắt buộc (*)'}), 400

        from Services.employee_debt_helpers import (
            get_employee_debt_detail,
            next_phieu_chi_no,
            salary_reference_key,
        )

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            detail = get_employee_debt_detail(conn, employee_id)
            if not detail:
                return jsonify({'success': False, 'error': 'Không tìm thấy nhân viên'}), 404

            row = next(
                (r for r in detail['records'] if int(r['month']) == month and int(r['year']) == year),
                None,
            )
            if not row:
                return jsonify({'success': False, 'error': 'Không tìm thấy kỳ lương'}), 404
            if float(row['con_lai']) <= 0.01:
                return jsonify({'success': False, 'error': 'Kỳ lương này đã được thanh toán đủ'}), 400
            if amount > float(row['con_lai']) + 0.01:
                return jsonify({'success': False, 'error': 'Số tiền vượt quá số còn phải trả'}), 400

            new_pc_no = next_phieu_chi_no(c)
            ref_key = salary_reference_key(employee_id, month, year)
            emp_name = detail['employee']['fullname']
            if f'{month}/{year}' not in reason:
                reason = f"{reason} (T{month}/{year})"

            c.execute(
                """
                INSERT INTO phieu_chi (
                    voucher_no, receiver_name, amount, credit_account, debit_account,
                    reason, source_type, expense_type, reference_document,
                    source_id, date, preparer
                ) VALUES (?, ?, ?, ?, ?, ?, 'salary', 'CP_LUONG', ?, ?, ?, ?)
                """,
                (
                    new_pc_no, receiver_name, amount, credit_account, debit_account,
                    reason, ref_key, employee_id, pay_date,
                    session.get('user_name', 'Admin'),
                ),
            )
            conn.commit()
            return jsonify({
                'success': True,
                'message': f'Đã lập phiếu chi trả lương lẻ — {emp_name}',
                'voucher': new_pc_no,
            })
        except Exception as e:
            conn.rollback()
            logger.exception('api_salary_pay failed')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/SoCongNoPhaiTra')
    def SoCongNoPhaiTra():
        return render_template('KeToanHKD/SoCongNoPhaiTra.html')

    #====API lấy danh sách nhà cung ứng cần phải được thanh toán===#
    @app.route("/api/debt/suppliers")
    def api_debt_suppliers():
        db = get_db_connection()
        cur = db.cursor()
        rows = cur.execute("""
            SELECT DISTINCT s.name
            FROM suppliers s
            JOIN import i ON s.id = i.supplier_id
            WHERE i.remaining_amount > 0
            ORDER BY s.name
        """).fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/debt/supplier-detail")
    def api_debt_supplier_detail():
        supplier_name = request.args.get("supplier")
        if not supplier_name:
            return jsonify({"success": False, "error": "Thiếu tên nhà cung cấp"}), 400

        db = get_db_connection()
        cur = db.cursor()

        # Tổng hợp
        summary = cur.execute("""
            SELECT 
                COALESCE(SUM(i.total_value), 0) AS total,
                COALESCE(SUM(i.paid_amount), 0) AS paid,
                COALESCE(SUM(i.remaining_amount), 0) AS remaining
            FROM "import" i
            JOIN suppliers s ON i.supplier_id = s.id
            WHERE s.name = ?
        """, (supplier_name,)).fetchone()

        # Chi tiết
        records = cur.execute("""
            SELECT 
                i.id,
                i.import_no,
                i.bill_no,
                i.date,
                i.total_value,
                i.paid_amount,
                i.remaining_amount,
                s.address AS supplier_address,
                s.name AS supplier_name
            FROM "import" i
            JOIN suppliers s ON i.supplier_id = s.id
            WHERE s.name = ? AND i.remaining_amount > 0
            ORDER BY i.date DESC
        """, (supplier_name,)).fetchall()

        return jsonify({
            "success": True,
            "summary": {
                "total": summary[0],
                "paid": summary[1],
                "remaining": summary[2]
            },
            "records": [dict(row) for row in records]
        })


    @app.route('/KeToanHKD/SoCongNoPhaiTra_print.html')
    def Print_SoCongNoPhaiTra():
        # Sử dụng 'supplier' (tên) để đồng bộ với API chi tiết
        supplier_name = request.args.get("supplier")
        start = request.args.get("start")
        end = request.args.get("end")

        if not supplier_name:
            return "Lỗi: Phải chọn nhà cung ứng để in sổ!", 400

        db = get_db_connection()
        cur = db.cursor()

        # 1. Lấy thông tin Hộ kinh doanh & NCC
        supplier_data = cur.execute("SELECT id, name, address FROM suppliers WHERE name = ?", (supplier_name,)).fetchone()
    
        if not supplier_data:
            return "Lỗi: Nhà cung cấp không tồn tại!", 404
    
        s_id = supplier_data['id']

        # 2. TÍNH SỐ DƯ ĐẦU KỲ (Trước ngày start)
        opening_balance = 0
        if start:
            sql_opening = """
                SELECT (SUM(total_value) - SUM(paid_amount)) as balance
                FROM import
                WHERE supplier_id = ? AND date < ?
            """
            res_opening = cur.execute(sql_opening, [s_id, f"{start} 00:00:00"]).fetchone()
            # Làm tròn để tránh số âm lẻ
            opening_balance = round(res_opening['balance'], 0) if res_opening and res_opening['balance'] else 0
            if opening_balance < 1: opening_balance = 0

        # 3. TRUY VẤN CÁC DÒNG PHÁT SINH TRONG KỲ
        sql_main = """
            SELECT 
                id,
                import_no AS purchase_no,
                date,
                'Nợ tiền mua hàng' as dien_giai,
                total_value as no,
                paid_amount as co
            FROM import
            WHERE supplier_id = ?
        """
        params = [s_id]
    
        if start:
            sql_main += " AND date >= ?"
            params.append(f"{start} 00:00:00")
        if end:
            sql_main += " AND date <= ?"
            params.append(f"{end} 23:59:59")

        sql_main += " ORDER BY date ASC, id ASC"
        rows_raw = cur.execute(sql_main, params).fetchall()
    
        # Chuyển đổi rows sang list dict và làm tròn số liệu từng dòng
        rows = []
        for r in rows_raw:
            item = dict(r)
            item['no'] = round(item['no'] or 0, 0)
            item['co'] = round(item['co'] or 0, 0)
            rows.append(item)

        # 4. TÍNH TOÁN TỔNG CỘNG
        total_no = sum(r["no"] for r in rows)
        total_co = sum(r["co"] for r in rows)
        closing_balance = opening_balance + total_no - total_co
        if closing_balance < 1: closing_balance = 0

        totals = {
            "opening": opening_balance,
            "no": total_no,
            "co": total_co,
            "closing": closing_balance
        }

        return render_template(
            "KeToanHKD/SoCongNoPhaiTra_print.html",
            rows=rows,
            totals=totals,
            start=start,
            end=end,
            supplier=supplier_name,
        )

    #=== TÀI SẢN CỐ ĐỊNH VÀ KHẤU HAO, PHÂN BỔ CHI PHÍ===#
    @app.route('/TaiSanCoDinh')
    def TaiSanCoDinh():
        selected_year = request.args.get('year', default=datetime.today().year, type=int)
        return render_template(
            'KeToanHKD/TSCD.html',
            year=selected_year,
        )

    @app.route('/api/tscd/list')
    @login_required
    def get_tscd_list():
        # Mặc định lấy năm hiện hành nếu không có tham số
        year = int(request.args.get('year', datetime.today().year))
    
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
    
        # Lấy danh sách tài sản đang hoạt động
        query = "SELECT * FROM fixed_assets WHERE tinh_trang = 'Active'"
        assets = cursor.execute(query).fetchall()
    
        result = []
        for row in assets:
            asset = dict(row)
            # SỬA LỖI: Đồng nhất tên biến nguyen_gia
            nguyen_gia = float(asset['nguyen_gia_tinh_khau_hao'] or 0)
            so_thang_kh = int(asset['so_thang_khau_hao'] or 1)
        
            if nguyen_gia <= 0: 
                continue
        
            kh_thang = nguyen_gia / so_thang_kh
        
            # --- 1. Xử lý an toàn ngày bắt đầu sử dụng ---
            val_ngay = asset.get('ngay_bat_dau_su_dung')
            if isinstance(val_ngay, str):
                ngay_bd_real = datetime.strptime(val_ngay.split(' ')[0], '%Y-%m-%d')
            elif hasattr(val_ngay, 'year'):
                ngay_bd_real = datetime(val_ngay.year, val_ngay.month, val_ngay.day)
            else:
                continue

            # Tính ngày kết thúc khấu hao thực tế (số tháng * 30.44 ngày)
            asset_end = ngay_bd_real + timedelta(days=int(so_thang_kh * 30.44))
        
            # --- 2. Tính lũy kế khấu hao đến hết năm trước (Giữ nguyên logic cũ) ---
            months_before_this_year = (year - ngay_bd_real.year) * 12 + (1 - ngay_bd_real.month)
            luy_ke_dau_nam = 0
            if months_before_this_year > 0:
                months_past = min(months_before_this_year, so_thang_kh)
                luy_ke_dau_nam = months_past * kh_thang

            # --- 3. Tính khấu hao từng Quý trong năm hiện tại theo NGÀY ---
            quarters_value = [0, 0, 0, 0] # q1, q2, q3, q4
        
            for q_idx in range(1, 5):
                # Xác định ngày bắt đầu/kết thúc quý (Sử dụng hàm get_days_in_quarter bạn đã có)
                first_month_of_q = (q_idx - 1) * 3 + 1
                days_in_q, q_start, q_end = get_days_in_quarter(year, first_month_of_q)
            
                # Giao thoa giữa Quý và thời gian sử dụng TS
                overlap_start = max(q_start, ngay_bd_real)
                overlap_end = min(q_end, asset_end)
            
                if overlap_start <= overlap_end:
                    days_selected = (overlap_end - overlap_start).days + 1
                
                    # Công thức chính xác theo yêu cầu của bạn
                    kh_1_quy = kh_thang * 3
                    quarters_value[q_idx-1] = (kh_1_quy / days_in_q) * days_selected

            q1, q2, q3, q4 = quarters_value

            # --- 4. Cập nhật kết quả ---
            asset.update({
                "monthly": round(kh_thang, 0),
                "luy_ke_dau_nam": round(luy_ke_dau_nam, 0),
                "q1": round(q1, 0), 
                "q2": round(q2, 0), 
                "q3": round(q3, 0), 
                "q4": round(q4, 0),
                "total_acc": round(luy_ke_dau_nam + q1 + q2 + q3 + q4, 0),
                "con_lai": round(nguyen_gia - (luy_ke_dau_nam + q1 + q2 + q3 + q4), 0)
            })
            result.append(asset)
        
        conn.close()
        return jsonify(result)


    @app.route('/api/tscd/export-and-create', methods=['POST'])
    @login_required
    def export_and_create_tscd():
        """Đưa TSCĐ vào sử dụng: kích hoạt bản ghi fixed_assets (InStock) hoặc luồng cũ từ tồn kho."""
        from Services.fixed_assets_helpers import FIXED_ASSETS_TABLE, STATUS_ACTIVE, STATUS_IN_STOCK

        data = request.get_json()
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        try:
            p_id = data.get('product_id')
            ngay_xuat = data.get('ngay_xuat', datetime.now().strftime('%Y-%m-%d'))
            months = int(data.get('months', 36) or 36)
            is_khau_tru = int(data.get('co_khau_tru', 0) or 0) == 1

            c.execute(f"""
                SELECT fa.*, p.product_code, p.name AS product_name
                FROM {FIXED_ASSETS_TABLE} fa
                JOIN products p ON p.id = fa.product_id
                WHERE fa.product_id = ? AND fa.tinh_trang = ?
                ORDER BY fa.id DESC LIMIT 1
            """, (p_id, STATUS_IN_STOCK))
            asset = c.fetchone()

            if asset:
                base_val = float(asset['nguyen_gia_tinh_khau_hao'] or 0) - float(asset['thue_gtgt'] or 0)
                if not is_khau_tru:
                    final_nguyen_gia = float(asset['nguyen_gia_tinh_khau_hao'] or 0)
                else:
                    final_nguyen_gia = base_val

                c.execute(f"""
                    UPDATE {FIXED_ASSETS_TABLE}
                    SET tinh_trang = ?, ngay_bat_dau_su_dung = ?, so_thang_khau_hao = ?,
                        co_duoc_khau_tru_thue = ?, nguyen_gia_tinh_khau_hao = ?
                    WHERE id = ?
                """, (STATUS_ACTIVE, ngay_xuat, months, 1 if is_khau_tru else 0, final_nguyen_gia, asset['id']))

                conn.commit()
                return jsonify({
                    'success': True,
                    'ma_ts': asset['ma_tai_san'],
                    'px_no': asset['voucher_no'] or '',
                    'nguyen_gia': final_nguyen_gia,
                    'mode': 'fixed_assets_register',
                })

            # --- Luồng cũ: xuất từ tồn kho bán hàng (dữ liệu legacy) ---
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute("""
                SELECT COUNT(DISTINCT ref_document)
                FROM stock_moves
                WHERE type IN ('SALE', 'export', 'RETURN_IMPORT', 'DELETE_IMPORT')
            """)
            next_seq = (c.fetchone()[0] or 0) + 1
            new_px_no = f"PX{str(next_seq).zfill(6)}"

            c.execute("""
                SELECT p.product_code, p.name, id.buyprice, id.cost_price, id.tax, id.discount, id.subtotal
                FROM products p
                JOIN import_details id ON p.id = id.product_id
                WHERE p.id = ?
                ORDER BY id.id DESC LIMIT 1
            """, (p_id,))
            origin = c.fetchone()
            if not origin:
                return jsonify({
                    'success': False,
                    'error': 'Không tìm thấy TSCĐ chờ đưa vào sử dụng. Hãy nhập kho loại TSCĐ trước.',
                }), 400

            base_val = float(origin['subtotal'] or 0) - float(origin['discount'] or 0)
            final_nguyen_gia = base_val if is_khau_tru else (base_val + float(origin['tax'] or 0))

            c.execute("""
                INSERT INTO stock_moves (
                    product_id, date, type, type1, ref_type, ref_id, ref_document,
                    quantity, cost_price, note
                ) VALUES (?, ?, 'export', 'Xuất dùng TSCĐ', 'TSCD', ?, ?, -1, ?, ?)
            """, (
                p_id, now_str, next_seq, new_px_no, origin['cost_price'],
                f"Xuất dùng TSCĐ (legacy) — {origin['product_code']} — {origin['name']}",
            ))
            move_id = c.lastrowid

            ma_ts = origin['product_code'] or f"TSCD-{p_id}"
            c.execute(f"""
                INSERT INTO {FIXED_ASSETS_TABLE} (
                    ma_tai_san, ten_tai_san, voucher_no, ngay_chung_tu, gia_mua_chua_thue,
                    nguyen_gia_tinh_khau_hao, thue_gtgt, co_duoc_khau_tru_thue,
                    ngay_bat_dau_su_dung, so_thang_khau_hao, stock_move_id, tinh_trang, product_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ma_ts, origin['name'], new_px_no, ngay_xuat, origin['buyprice'],
                final_nguyen_gia, origin['tax'], 1 if is_khau_tru else 0,
                ngay_xuat, months, move_id, STATUS_ACTIVE, p_id,
            ))

            from Services.inventory_stock_helpers import sync_inventory_quantity_from_moves
            sync_inventory_quantity_from_moves(c, p_id)
            c.execute("""
                INSERT INTO inventory_transactions (product_id, type, type1, quantity, reference_id, created_at)
                VALUES (?, 'export', 'Xuất dùng TSCĐ', -1, ?, ?)
            """, (p_id, move_id, now_str))

            conn.commit()
            return jsonify({
                'success': True,
                'ma_ts': ma_ts,
                'px_no': new_px_no,
                'ref_id': next_seq,
                'nguyen_gia': final_nguyen_gia,
                'mode': 'legacy_inventory',
            })

        except Exception as e:
            if conn:
                conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            if conn:
                conn.close()

    #=== Lấy Danh Sách TSCĐ chờ đưa vào sử dụng (fixed_assets InStock) ===#
    @app.route('/api/products/for-tscd', methods=['GET'])
    @login_required
    def get_products_for_tscd():
        from Services.fixed_assets_helpers import FIXED_ASSETS_TABLE, STATUS_IN_STOCK

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        sql = f"""
            SELECT
                p.id, p.product_code, p.name,
                fa.gia_mua_chua_thue AS buyprice,
                fa.thue_gtgt AS tax,
                COALESCE(id.discount, 0) AS discount,
                COALESCE(id.subtotal, fa.nguyen_gia_tinh_khau_hao) AS subtotal,
                fa.so_luong AS stock_qty,
                fa.id AS fixed_asset_id,
                fa.ma_tai_san,
                fa.voucher_no
            FROM {FIXED_ASSETS_TABLE} fa
            JOIN products p ON p.id = fa.product_id
            LEFT JOIN import_details id ON id.id = fa.import_detail_id
            WHERE fa.tinh_trang = ?
            ORDER BY fa.id DESC
        """
        c.execute(sql, (STATUS_IN_STOCK,))
        rows = [dict(r) for r in c.fetchall()]

        if not rows:
            c.execute("""
                SELECT p.id, p.product_code, p.name,
                       id.buyprice, id.tax, id.discount, id.subtotal,
                       inv.quantity AS stock_qty
                FROM products p
                JOIN inventory inv ON p.id = inv.product_id
                LEFT JOIN import_details id ON p.id = id.product_id
                WHERE inv.quantity > 0 AND COALESCE(p.product_type, '') = 'fixed_asset'
                GROUP BY p.id
                ORDER BY id.id DESC
            """)
            rows = [dict(r) for r in c.fetchall()]

        conn.close()
        return jsonify(rows)

    @app.route('/CongCuDungCu')
    @login_required
    def CongCuDungCu():
        selected_year = request.args.get('year', default=datetime.today().year, type=int)
        return render_template('KeToanHKD/CCDC.html', year=selected_year)

    @app.route('/api/ccdc/list')
    @login_required
    def get_ccdc_list():
        from Services.fixed_assets_helpers import TOOLS_TABLE, STATUS_ACTIVE

        year = int(request.args.get('year', datetime.today().year))
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT * FROM {TOOLS_TABLE}
            WHERE tinh_trang = ?
            ORDER BY id DESC
        """, (STATUS_ACTIVE,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            nguyen_gia = float(item.get('nguyen_gia') or 0)
            so_thang = int(item.get('so_thang_phan_bo') or 12)
            if nguyen_gia <= 0 or so_thang <= 0:
                continue
            pb_thang = nguyen_gia / so_thang
            val_ngay = item.get('ngay_bat_dau_su_dung')
            if isinstance(val_ngay, str):
                ngay_bd = datetime.strptime(val_ngay.split(' ')[0], '%Y-%m-%d')
            elif hasattr(val_ngay, 'year'):
                ngay_bd = datetime(val_ngay.year, val_ngay.month, val_ngay.day)
            else:
                continue

            asset_end = ngay_bd + timedelta(days=int(so_thang * 30.44))
            months_before = (year - ngay_bd.year) * 12 + (1 - ngay_bd.month)
            luy_ke_dau_nam = min(max(months_before, 0), so_thang) * pb_thang

            quarters_value = [0, 0, 0, 0]
            for q_idx in range(1, 5):
                first_month_of_q = (q_idx - 1) * 3 + 1
                days_in_q, q_start, q_end = get_days_in_quarter(year, first_month_of_q)
                overlap_start = max(q_start, ngay_bd)
                overlap_end = min(q_end, asset_end)
                if overlap_start <= overlap_end:
                    days_selected = (overlap_end - overlap_start).days + 1
                    pb_1_quy = pb_thang * 3
                    quarters_value[q_idx - 1] = (pb_1_quy / days_in_q) * days_selected

            q1, q2, q3, q4 = quarters_value
            item['phan_bo_thang'] = round(pb_thang, 0)
            item['luy_ke_dau_nam'] = round(luy_ke_dau_nam, 0)
            item['q1'] = round(q1, 0)
            item['q2'] = round(q2, 0)
            item['q3'] = round(q3, 0)
            item['q4'] = round(q4, 0)
            item['total_acc'] = round(luy_ke_dau_nam + q1 + q2 + q3 + q4, 0)
            item['con_lai'] = round(nguyen_gia - (luy_ke_dau_nam + q1 + q2 + q3 + q4), 0)
            item['tinh_trang_label'] = 'Đang phân bổ'
            result.append(item)
        conn.close()
        return jsonify(result)

    @app.route('/api/products/for-ccdc', methods=['GET'])
    @login_required
    def get_products_for_ccdc():
        from Services.fixed_assets_helpers import TOOLS_TABLE, STATUS_IN_STOCK

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(f"""
            SELECT t.id AS tool_id, p.id, p.product_code, p.name,
                   t.gia_mua_chua_thue AS buyprice, t.thue_gtgt AS tax,
                   t.nguyen_gia AS subtotal, t.so_luong AS stock_qty, t.ma_ccdc
            FROM {TOOLS_TABLE} t
            JOIN products p ON p.id = t.product_id
            WHERE t.tinh_trang = ?
            ORDER BY t.id DESC
        """, (STATUS_IN_STOCK,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify(rows)

    @app.route('/api/ccdc/activate', methods=['POST'])
    @login_required
    def activate_ccdc():
        from Services.fixed_assets_helpers import TOOLS_TABLE, STATUS_ACTIVE, STATUS_IN_STOCK

        data = request.get_json() or {}
        product_id = data.get('product_id')
        ngay_sd = data.get('ngay_bat_dau_su_dung', datetime.now().strftime('%Y-%m-%d'))
        months = int(data.get('months', 12) or 12)

        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute(f"""
                UPDATE {TOOLS_TABLE}
                SET tinh_trang = ?, ngay_bat_dau_su_dung = ?, so_thang_phan_bo = ?
                WHERE product_id = ? AND tinh_trang = ?
            """, (STATUS_ACTIVE, ngay_sd, months, product_id, STATUS_IN_STOCK))
            if c.rowcount == 0:
                return jsonify({'success': False, 'error': 'Không tìm thấy CCDC chờ kích hoạt'}), 404
            conn.commit()
            return jsonify({'success': True})
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    #==== XUẤT VẬT TƯ PHỤC VỤ SẢN XUẤT===#
    @app.route('/api/materials/export', methods=['POST'])
    @login_required
    def export_material_production():
        data = request.get_json()
        conn = get_db_connection()
        c = conn.cursor()
        try:
            p_id = data.get('product_id')
            qty = float(data.get('quantity', 1))
            export_date = data.get('ngay_xuat') or datetime.now().strftime('%Y-%m-%d')
            now_str = f"{export_date} {datetime.now().strftime('%H:%M:%S')}"
        
            # 1. Tự động tính số phiếu (absolute_seq) để gán vào ref_id
            # Logic này đếm các nhóm chứng từ duy nhất để tạo số thứ tự tiếp theo
            c.execute("""
                SELECT COUNT(DISTINCT COALESCE(ref_document, ref_id)) 
                FROM stock_moves 
                WHERE type IN ('SALE', 'RETURN_IMPORT', 'DELETE_IMPORT', 'export')
            """)
            current_count = c.fetchone()[0] or 0
            new_seq = current_count + 1
        
            # Tạo chuỗi hiển thị PX0000xx
            new_px_no = f"PX{str(new_seq).zfill(6)}"

            # 2. Thực hiện chèn vào stock_moves
            # Chèn new_seq vào ref_id để trang in có thể truy vấn chính xác
            c.execute("""
                INSERT INTO stock_moves (
                    product_id, 
                    date, 
                    type, 
                    type1, 
                    ref_id, 
                    ref_type, 
                    ref_document, 
                    quantity, 
                    cost_price, 
                    note
                ) VALUES (
                    ?, ?, 'export', 'Xuất cho sản xuất', ?, 'PVSX', ?, 
                    ?, (SELECT COALESCE(avg_cost, 0) FROM inventory WHERE product_id = ?), 
                    ?
                )
            """, (
                p_id, 
                now_str, 
                new_seq,
                new_px_no,
                -qty,
                p_id, 
                f"Xuất vật tư sản xuất: {data.get('production_note', '')}"
            ))
        
            move_id = c.lastrowid # Lấy ID của dòng vừa chèn (nếu cần dùng)

            from Services.inventory_stock_helpers import sync_inventory_quantity_from_moves
            sync_inventory_quantity_from_moves(c, p_id)

            # 4. Ghi log vào bảng giao dịch (Nếu hệ thống của bạn có bảng này)
            c.execute("""
                INSERT INTO inventory_transactions (product_id, type, type1, quantity, reference_id, created_at)
                VALUES (?, 'export', 'Xuất vật tư', ?, ?, ?)
            """, (p_id, -qty, move_id, now_str))

            conn.commit()
            return jsonify({
                "success": True, 
                "px_no": new_px_no, 
                "ref_id": new_seq
            })

        except Exception as e:
            conn.rollback()
            print(f"Lỗi xuất vật tư: {str(e)}")
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

    #==== THEO DÕI KHOẢN VAY====#
    @app.route('/loan-tracking')
    def SoTheoDoiKhoanVay():
        """Hiển thị trang Sổ theo dõi khoản vay"""
        return render_template('KeToanHKD/SoTheoDoiKhoanVay.html')

    # --- API: LẤY DANH SÁCH KHOẢN VAY ---
    # API lấy danh sách khoản vay
    @app.route('/api/loans/list', methods=['GET'])
    def get_loans():
        db = None
        try:
            db = get_db_connection()
            db.row_factory = sqlite3.Row
            cursor = db.cursor()
        
            # Lấy danh sách vay
            cursor.execute("SELECT * FROM loans ORDER BY created_at DESC")
            rows = cursor.fetchall()
        
            loans = []
            for row in rows:
                loan = dict(row)
            
                # --- 1. Xử lý an toàn Start Date ---
                # Ưu tiên start_date, nếu không có thì dùng created_at
                start_val = loan.get('start_date') or loan.get('created_at')
            
                if not start_val:
                    continue # Bỏ qua nếu không có dữ liệu ngày

                # FIX LỖI SPLIT: Kiểm tra nếu là chuỗi thì mới split, nếu là date object thì dùng trực tiếp
                if isinstance(start_val, str):
                    try:
                        # Lấy phần yyyy-mm-dd từ chuỗi (đề phòng có HH:MM:SS)
                        start_str = start_val.split(' ')[0]
                        start_dt = datetime.strptime(start_str, '%Y-%m-%d')
                    except ValueError:
                        # Backup plan nếu format khác
                        continue
                elif isinstance(start_val, (date, datetime)):
                    start_dt = datetime(start_val.year, start_val.month, start_val.day)
                else:
                    continue

                # --- 2. Đếm số lần đã trả lãi (TK 635) ---
                # Tài khoản 635 - Chi phí tài chính (lãi vay)
                cursor.execute("""
                    SELECT COUNT(*) FROM phieu_chi 
                    WHERE (debit_account = '635' OR expense_type = 'CP_TRALAIVAY')
                      AND reference_document = ?
                """, (loan['contract_number'],))
                times_paid = cursor.fetchone()[0] or 0

                # --- 3. Tính ngày trả lãi tiếp theo ---
                # Ngày trả lãi tiếp theo = Start Date + (số lần đã trả + 1) tháng
                next_pay_dt = start_dt + relativedelta(months=(times_paid + 1))
            
                # Tính trạng thái quá hạn (nếu cần)
                is_overdue = next_pay_dt.date() < date.today()
            
                loan.update({
                    'next_payment_iso': next_pay_dt.strftime('%Y-%m-%d'),
                    'next_payment_vn': next_pay_dt.strftime('%d/%m/%Y'),
                    'times_paid': times_paid,
                    'is_overdue': is_overdue
                })
                loans.append(loan)
            
            return jsonify(loans)
        except Exception as e:
            import traceback
            print("ERROR in /api/loans/list:", traceback.format_exc())
            return jsonify({"error": str(e)}), 500
        finally:
            if db:
                db.close()

    # API thêm khoản vay mới
    @app.route('/api/loans/add', methods=['POST'])
    def add_loan():
        try:
            data = request.get_json()
            db = get_db_connection()
            cursor = db.cursor()
            # Bổ sung start_date vào bảng để làm căn cứ tính kỳ hạn
            cursor.execute("""
                INSERT INTO loans (contract_number, lender_name, loan_amount, interest_rate_year, term_months, start_date)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                data['contract_number'],
                data['lender_name'],
                float(data['loan_amount']),
                float(data['interest_rate_year']),
                int(data['term_months']),
                data.get('start_date', datetime.now().strftime('%Y-%m-%d'))
            ))
            db.commit()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- API: CẬP NHẬT TRẢ NỢ (Trả gốc) ---
    @app.route('/api/loans/pay', methods=['POST'])
    def pay_loan_principal():
        data = request.get_json()
        loan_id = data.get('id')
        pay_amount = float(data.get('amount', 0))

        if not loan_id or pay_amount <= 0:
            return jsonify({"success": False, "error": "Dữ liệu không hợp lệ"}), 400

        try:
            db = get_db_connection()
            cursor = db.cursor()
        
            # Cập nhật cộng dồn số nợ gốc đã trả
            cursor.execute("""
                UPDATE loans 
                SET amount_paid = amount_paid + ? 
                WHERE id = ?
            """, (pay_amount, loan_id))
        
            db.commit()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    #=== API THANH TOÁN LÃI VAY VÀ NỢ GỐC===#
    @app.route('/api/loans/action', methods=['POST'])
    def loan_action():
        data = request.get_json()
        loan_id = data.get('id')
        action_type = data.get('type') 
        amount = float(data.get('amount', 0))
        selected_date = data.get('date')
    
        db = get_db_connection()
        c = db.cursor()
    
        try:
            c.execute("SELECT * FROM loans WHERE id = ?", (loan_id,))
            loan = c.fetchone()
        
            # 1. Tạo mã phiếu chi tự động
            c.execute("SELECT voucher_no FROM phieu_chi WHERE voucher_no LIKE 'PC%' ORDER BY id DESC LIMIT 1")
            last_pc = c.fetchone()
            if last_pc and last_pc[0][2:].isdigit():
                new_pc = f"PC{(int(last_pc[0][2:]) + 1):06d}"
            else:
                new_pc = "PC000001"
        
            # 2. Xử lý theo loại thanh toán
            if action_type == 'interest':
                debit_acc = '635'
                reason = f"Trả lãi vay tháng kỳ tiếp theo - HĐ {loan['contract_number']}"
                expense_type = "CP_TRALAIVAY"
                c.execute("""UPDATE loans SET 
                             interest_paid = COALESCE(interest_paid, 0) + ?, 
                             paid_interest_period = COALESCE(paid_interest_period, 0) + 1 
                             WHERE id = ?""", (amount, loan_id))
            else:
                debit_acc = '341'
                reason = f"Trả nợ gốc HĐ {loan['contract_number']}"
                expense_type = "TRANOGOC"
                c.execute("""UPDATE loans SET 
                             amount_paid = COALESCE(amount_paid, 0) + ? 
                             WHERE id = ?""", (amount, loan_id))

            # 3. Chèn vào bảng phieu_chi
            c.execute("""INSERT INTO phieu_chi (voucher_no, receiver_name, amount, credit_account, 
                         debit_account, reason, source_type, expense_type, source_id, date) 
                         VALUES (?, ?, ?, ?, ?, ?, 'loan', ?, ?, ?)""",
                      (new_pc, loan['lender_name'], amount, data.get('payment_method'), 
                       debit_acc, reason, expense_type, loan_id, selected_date))
        
            db.commit()
            return jsonify({"success": True, "voucher": new_pc})
        except Exception as e:
            db.rollback()
            return jsonify({"success": False, "error": str(e)}), 500

    #===CẬP NHẬT KHOẢN VAY KHI CÓ THAY ĐỔI LÃI SUẤT HAY KỲ HẠN VAY ĐƯỢC GIA HẠN===#
    @app.route('/api/loans/update', methods=['POST'])
    def update_loan():
        data = request.get_json()
        loan_id = data.get('id')
    
        db = get_db_connection()
        c = db.cursor()
    
        try:
            c.execute("""
                UPDATE loans 
                SET loan_amount = ?, 
                    interest_rate_year = ?, 
                    term_months = ?, 
                    start_date = ?,
                    interest_paid = ?,
                    paid_interest_period = ?,
                    amount_paid = ? 
                WHERE id = ?
            """, (
                float(data.get('loan_amount', 0)),
                float(data.get('interest_rate_year', 0)),
                int(data.get('term_months', 0)),
                data.get('start_date'),
                float(data.get('interest_paid', 0)),
                int(data.get('paid_interest_period', 0)),
                float(data.get('amount_paid', 0)), # Trường mới bổ sung
                loan_id
            ))
            db.commit()
            return jsonify({"success": True})
        except Exception as e:
            db.rollback()
            return jsonify({"success": False, "error": str(e)}), 500

    # --- VIEW: HIỂN THỊ TRANG ---
    @app.route('/thue-khac-s3a')
    def SoTheoDoiThueKhac():
        """Hiển thị trang Sổ theo dõi nghĩa vụ thuế khác (S3a-HKD)"""
        return render_template('KeToanHKD/SoTheoDoiThueKhac.html')

    # --- API: LẤY DANH SÁCH THUẾ ---
    @app.route('/api/thue-khac/list', methods=['GET'])
    def get_thue_khac():
        try:
            from_date = request.args.get('from_date')
            to_date   = request.args.get('to_date')

            db = get_db_connection()
            cursor = db.cursor()
            cursor.row_factory = sqlite3.Row

            # 1. SQL Query: Bổ sung logic tính tổng Cột 7 trực tiếp nếu database chưa có sẵn
            # Cột 7 = thue_xk_nk_ptram + thue_xk_nk_tuyet_doi
            query = """
                SELECT 
                    t.*,
                    (COALESCE(t.thue_xk_nk_ptram, 0) + COALESCE(t.thue_xk_nk_tuyet_doi, 0)) as tong_thue_nk_xk_ttdb,
                    COALESCE(t.paid_amount, 0) as paid_amount,
                    p.id AS phieu_chi_id,
                    p.voucher_no
                FROM thue_khac t
                LEFT JOIN phieu_chi p 
                    ON t.id = p.source_id 
                    AND p.source_type = 'tax'
            """
        
            params = []
            conditions = []

            if from_date:
                conditions.append("t.ngay_ghi_so >= ?")
                params.append(from_date)
            if to_date:
                conditions.append("t.ngay_ghi_so <= ?")
                params.append(to_date)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY t.ngay_ghi_so ASC, t.id ASC"

            cursor.execute(query, params)
            rows = cursor.fetchall()

            data = []
            for row in rows:
                item = dict(row)
            
                # --- CHUẨN HÓA DỮ LIỆU SỐ ---
                # Tránh lỗi None khi frontend thực hiện tính toán JS
                numeric_fields = [
                    'luong_hang', 'muc_thue_tuyet_doi', 'gia_tinh_thue', 
                    'thue_suat', 'thue_xk_nk_ptram', 'thue_xk_nk_tuyet_doi',
                    'tong_thue_nk_xk_ttdb', 'thue_phai_nop', 'thue_bvmt', 
                    'thue_tai_nguyen', 'thue_sd_dat', 'paid_amount'
                ]
                for field in numeric_fields:
                    item[field] = item.get(field) or 0

                # --- LOGIC TÌNH TRẠNG (STATUS) ---
                total_must_pay = item['thue_phai_nop']
                paid = item['paid_amount']
            
                if total_must_pay <= 0:
                    item['tinh_trang'] = "Không có nghĩa vụ"
                    item['status_class'] = "bg-secondary-subtle text-secondary"
                elif paid >= total_must_pay:
                    item['tinh_trang'] = "Đã nộp đủ"
                    item['status_class'] = "bg-success-subtle text-success"
                elif paid > 0:
                    item['tinh_trang'] = "Nộp một phần"
                    item['status_class'] = "bg-warning-subtle text-warning"
                else:
                    item['tinh_trang'] = "Chưa nộp"
                    item['status_class'] = "bg-danger-subtle text-danger"
                
                data.append(item)

            return jsonify(data)

        except Exception as e:
            # Ghi log chi tiết lỗi tại server
            print(f"CRITICAL ERROR: /api/thue-khac/list - {str(e)}")
            # Trả về mã 200 kèm list rỗng để Frontend không bị "crash" giao diện
            return jsonify([]), 200

    # --- API: THÊM DÒNG DỮ LIỆU MỚI ---
    @app.route('/api/thue-khac/add', methods=['POST'])
    def add_thue_khac():
        try:
            data = request.get_json()
            db = get_db_connection()
            cursor = db.cursor()
        
            query = """
                INSERT INTO thue_khac (
                    ngay_ghi_so, dien_giai, luong_hang, muc_thue_tuyet_doi, 
                    gia_tinh_thue, thue_suat, thue_xk_nk_ptram, 
                    thue_xk_nk_tuyet_doi, tong_thue_nk_xk_ttdb, 
                    thue_bvmt, thue_tai_nguyen, thue_sd_dat, 
                    thue_phai_nop, created_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        
            # Đảm bảo ép kiểu float để tránh lỗi NULL hoặc chuỗi rỗng
            params = (
                data.get('ngay_ghi_so'),
                data.get('dien_giai'),
                float(data.get('luong_hang') or 0),
                float(data.get('muc_thue_tuyet_doi') or 0),
                float(data.get('gia_tinh_thue') or 0),
                float(data.get('thue_suat') or 0),
                float(data.get('thue_xk_nk_ptram') or 0),
                float(data.get('thue_xk_nk_tuyet_doi') or 0),
                float(data.get('tong_thue_nk_xk_ttdb') or 0), # Cột 7 mới
                float(data.get('thue_bvmt') or 0),
                float(data.get('thue_tai_nguyen') or 0),
                float(data.get('thue_sd_dat') or 0),
                float(data.get('thue_phai_nop') or 0),
                session.get('user_name', 'Admin')
            )
        
            cursor.execute(query, params)
            db.commit()
            return jsonify({"success": True, "message": "Thành công"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # --- API: HẠCH TOÁN CHI TRẢ THUẾ (Tạo phiếu chi tự động) ---
    @app.route('/api/thue-khac/pay', methods=['POST'])
    def pay_tax_action():
        data = request.get_json()
    
        # Lấy dữ liệu từ Frontend gửi lên
        row_id = data.get('id')
        amount = float(data.get('amount', 0))
        pay_date = data.get('pay_date')  # Khớp với Frontend
        receiver_name = data.get('receiver', '').strip()  # Khớp với Frontend
        debit_account = data.get('debit_account', '333').strip()
        credit_account = data.get('pay_method', '111')  # Khớp với Frontend
        reason = data.get('reason', '').strip()

        # Kiểm tra dữ liệu đầu vào
        if not row_id or amount <= 0 or not pay_date or not reason or not receiver_name:
            return jsonify({"success": False, "error": "Vui lòng nhập đầy đủ các trường bắt buộc (*)"}), 400

        db = get_db_connection()
        db.row_factory = sqlite3.Row  # Đảm bảo truy xuất được theo tên cột
        c = db.cursor()

        try:
            # 1. Kiểm tra sự tồn tại của bản ghi thuế
            c.execute("SELECT thue_phai_nop, paid_amount FROM thue_khac WHERE id = ?", (row_id,))
            tax = c.fetchone()
            if not tax:
                return jsonify({"success": False, "error": "Không tìm thấy nghiệp vụ thuế này"}), 404

            # Tính toán số tiền đã nộp mới
            paid_before = tax['paid_amount'] or 0
            new_paid = paid_before + amount

            # 2. Tự động sinh số phiếu chi mới (PCxxxxxx)
            c.execute("SELECT voucher_no FROM phieu_chi WHERE voucher_no LIKE 'PC%' ORDER BY id DESC LIMIT 1")
            last = c.fetchone()
            if last and last['voucher_no']:
                try:
                    # Tách phần số từ chuỗi 'PC000001'
                    last_no = int(last['voucher_no'][2:])
                    new_pc_no = f"PC{last_no + 1:06d}"
                except ValueError:
                    new_pc_no = "PC000001"
            else:
                new_pc_no = "PC000001"

            # 3. Chèn dữ liệu vào bảng phiếu chi (phieu_chi)
            c.execute("""
                INSERT INTO phieu_chi (
                    voucher_no, 
                    receiver_name, 
                    amount, 
                    credit_account, 
                    debit_account, 
                    reason,
                    source_type, 
                    expense_type, 
                    source_id, 
                    date, 
                    preparer
                ) VALUES (?, ?, ?, ?, ?, ?, 'tax', ?, ?, ?, ?)
            """, (
                new_pc_no, 
                receiver_name, 
                amount, 
                credit_account, 
                debit_account, 
                reason, 
                'CP_TAX',
                row_id, 
                pay_date, 
                session.get('user_name', 'Admin')
            ))

            # 4. Cập nhật lại tổng số tiền đã nộp vào bảng thue_khac
            c.execute("UPDATE thue_khac SET paid_amount = ? WHERE id = ?", (new_paid, row_id))

            db.commit()
            return jsonify({
                "success": True, 
                "message": "Đã lập phiếu chi thành công", 
                "voucher": new_pc_no
            })

        except Exception as e:
            db.rollback()
            print(f"Error: {str(e)}") # Log lỗi ra console để debug
            return jsonify({"success": False, "error": f"Lỗi hệ thống: {str(e)}"}), 500

    @app.route('/api/thue-khac/update', methods=['POST'])
    def update_thue_khac():
        try:
            data = request.get_json()
            db = get_db_connection()
            cursor = db.cursor()
        
            query = """
                UPDATE thue_khac SET 
                    ngay_ghi_so=?, dien_giai=?, luong_hang=?, muc_thue_tuyet_doi=?,
                    gia_tinh_thue=?, thue_suat=?, thue_xk_nk_ptram=?, thue_xk_nk_tuyet_doi=?,
                    tong_thue_nk_xk_ttdb=?, thue_bvmt=?, thue_tai_nguyen=?, thue_sd_dat=?,
                    thue_phai_nop=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """
            params = (
                data.get('ngay_ghi_so'), data.get('dien_giai'),
                float(data.get('luong_hang') or 0), float(data.get('muc_thue_tuyet_doi') or 0),
                float(data.get('gia_tinh_thue') or 0), float(data.get('thue_suat') or 0),
                float(data.get('thue_xk_nk_ptram') or 0), float(data.get('thue_xk_nk_tuyet_doi') or 0),
                float(data.get('tong_thue_nk_xk_ttdb') or 0),
                float(data.get('thue_bvmt') or 0), float(data.get('thue_tai_nguyen') or 0),
                float(data.get('thue_sd_dat') or 0), float(data.get('thue_phai_nop') or 0),
                data.get('id')
            )
            cursor.execute(query, params)
            db.commit()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    #=== ROUTE IN SỔ THEO DÕI CÁC KHOẢN THUẾ KHÁC===#
    @app.route('/api/thue-khac/print')
    def print_thue_khac():
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
    
        db = get_db_connection()
        db.row_factory = sqlite3.Row 
        cursor = db.cursor()
    
        # Truy vấn dữ liệu
        query = "SELECT * FROM thue_khac WHERE ngay_ghi_so BETWEEN ? AND ? ORDER BY ngay_ghi_so ASC"
        cursor.execute(query, (from_date, to_date))
        rows = cursor.fetchall()
        items = [dict(row) for row in rows]
    
        # Tính toán tổng cộng
        totals = {
            'tax_ptram': sum(item.get('thue_xk_nk_ptram', 0) or 0 for item in items),
            'tax_tuyetdoi': sum(item.get('thue_xk_nk_tuyet_doi', 0) or 0 for item in items),
            'tax_total': sum(item.get('tong_thue_nk_xk_ttdb', 0) or 0 for item in items),     # Tổng tax_ptram + tax_tuyetdoi
            'env_tax': sum(item.get('thue_bvmt', 0) or 0 for item in items),
            'resource_tax': sum(item.get('thue_tai_nguyen', 0) or 0 for item in items),
            'land_tax': sum(item.get('thue_sd_dat', 0) or 0 for item in items),
        }

        return render_template('KeToanHKD/SoTheoDoiThueKhac_print.html', 
                               items=items, 
                               totals=totals, 
                               from_date=from_date, 
                               to_date=to_date)

    @app.route('/api/thue-khac/history/<int:tax_id>', methods=['GET'])
    def get_tax_payment_history(tax_id):
        try:
            db = get_db_connection()
            cursor = db.cursor()
            cursor.row_factory = sqlite3.Row
            cursor.execute("""
                SELECT id, voucher_no, amount, date, reason
                FROM phieu_chi 
                WHERE source_id = ? AND source_type = 'tax'
                ORDER BY id ASC
            """, (tax_id,))
            rows = cursor.fetchall()
            return jsonify([dict(row) for row in rows])
        except Exception as e:
            print("Lỗi history:", str(e))
            return jsonify([]), 200

    @app.route('/api/database/reset', methods=['POST'])
    @login_required
    def api_database_reset():
        data = request.get_json() or {}
        confirm_password = data.get('password', '').strip()

        # ==================== XÁC NHẬN MẬT KHẨU ====================
        RESET_PASSWORD = "RESET123"   # ← THAY ĐỔI THÀNH MẬT KHẨU MẠNH CỦA BẠN

        if confirm_password != RESET_PASSWORD:
            return jsonify({
                "success": False,
                "error": "Mật khẩu xác nhận không đúng."
            }), 403

        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            # Danh sách bảng cần xóa (theo thứ tự tránh lỗi foreign key)
            tables = [
                "import_details",
                "import_payments",
                "return_import",
                "import",
                "return_sales",
                "sale_items",
                "sale",
                "phieu_xuat_kho",
                "Chi_tiet_phieu_nhap_kho",
                "phieu_nhap_kho",
                "phieu_thu",
                "phieu_chi",
                "cong_no",
                "stock_moves",
                "inventory_transactions",
                "inventory",
                "Salary_Detail",
                "customers",
                "employees",
                "import_sequence",
                "loans",
                "outward_invoices",
                "products",
                "product_aliases",
                "salary_history",
                "supplier_invoice",
                "suppliers",
                "fixed_assets",
                "tax_declarations",
                "thue_khac",
                "rooms",
                "renters"

            ]

            deleted_tables = []
            for table in tables:
                try:
                    cursor.execute(f"DELETE FROM `{table}`")
                    deleted_tables.append(table)
                    print(f"Đã xóa dữ liệu bảng: {table}")
                except Exception as e:
                    print(f"Bảng {table} lỗi hoặc không tồn tại: {e}")

            # Reset AUTOINCREMENT
            cursor.execute("DELETE FROM sqlite_sequence")

            conn.commit()

            # Logging (an toàn, không phụ thuộc current_user)
            username = getattr(current_user, 'username', 'Unknown')
            logging.warning(f"🔴 TOÀN BỘ DATABASE ĐÃ BỊ XÓA bởi user: {username} (IP: {request.remote_addr})")

            return jsonify({
                "success": True,
                "message": "Đã xóa toàn bộ dữ liệu trong database thành công.",
                "deleted_tables": deleted_tables
            })

        except Exception as e:
            if conn:
                conn.rollback()
            logging.error(f"Lỗi reset database: {str(e)}", exc_info=True)
            return jsonify({
                "success": False,
                "error": f"Lỗi hệ thống: {str(e)}"
            }), 500

        finally:
            if conn:
                conn.close()

