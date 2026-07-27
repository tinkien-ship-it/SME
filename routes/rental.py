"""Đăng ký route cho thuê phòng (giữ nguyên URL/endpoint)."""
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
from Services.invoice_buyer import DEFAULT_RETAIL_BUYER_NAME, normalize_retail_buyer_name
from Services.hkd_sector import resolve_item_hkd_sector
from Services.sale_helpers import insert_sale_item_with_sector
from Services.rental_billing import compute_rental_debt, fetch_renter_payment_dates

logger = logging.getLogger(__name__)


def complete_rental_bank_payment(sale_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        sale = cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            return {"success": False, "error": "Không tìm thấy hóa đơn"}
        if sale['status'] == 'completed':
            conn.commit()
            return {"success": True, "already_completed": True}

        meta = {}
        user_note = sale['note'] or ''
        try:
            meta = json.loads(sale['note'] or '{}')
            user_note = meta.get('user_note', '')
        except (json.JSONDecodeError, TypeError):
            pass

        renter_id = meta.get('renter_id')
        new_elec_index = meta.get('new_elec_index')
        current_period = meta.get('current_period')
        room_no = meta.get('room_no', '')
        payment_method = sale['payment_method']
        total_amount = float(sale['total_amount'] or 0)
        sale_no = sale['sale_no'] or f"ĐH{str(sale_id).zfill(6)}"
        sale_date = sale['date']
        customer_name = sale['customer_name'] or DEFAULT_RETAIL_BUYER_NAME
        address = sale['address'] or ''
        tax_code = sale['tax_code'] or ''

        if renter_id:
            if new_elec_index is not None and str(new_elec_index).strip() != "":
                cursor.execute("""
                    UPDATE renters SET last_elec_index = ?, last_paid_period = ? WHERE id = ?
                """, (new_elec_index, current_period, renter_id))
            elif current_period:
                cursor.execute("""
                    UPDATE renters SET last_paid_period = ? WHERE id = ?
                """, (current_period, renter_id))

        if payment_method in ["111", "112"]:
            last_pt = cursor.execute(
                "SELECT voucher_no FROM phieu_thu WHERE voucher_no LIKE 'PT%' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last_pt and last_pt['voucher_no']:
                try:
                    number_part = "".join(filter(str.isdigit, last_pt['voucher_no']))
                    pt_num = int(number_part) + 1 if number_part else 1
                except Exception:
                    pt_num = 1
            else:
                pt_num = 1
            pt_vno = f"PT{pt_num:06d}"
            reason = f"Thu tiền dịch vụ thuê phòng số {room_no} - Hóa đơn {sale_no}"
            cursor.execute("""
                INSERT INTO phieu_thu (voucher_no, payer_name, address, tax_code, amount,
                                     debit_account, credit_account, reason, reference_document, sale_id, date)
                VALUES (?, ?, ?, ?, ?, ?, '511', ?, ?, ?, ?)
            """, (pt_vno, customer_name, address, tax_code, total_amount, payment_method, reason, sale_no, sale_id, sale_date))

        cursor.execute("UPDATE sale SET status = 'completed', note = ? WHERE id = ?", (user_note, sale_id))
        conn.commit()
        return {"success": True, "sale_id": sale_id}
    except Exception as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        if conn:
            conn.close()


def register_rental_routes(app):
    """Đăng ký route cho thuê phòng (giữ nguyên URL/endpoint)."""
    from auth import login_required
    #======================================================================================= Start of Rental Service ========================================================================#
    @app.route('/rental')
    @login_required
    def rental_service():
        return render_template('rental_service.html')

    @app.route('/api/rental/renter', methods=['POST'])
    @login_required
    def api_create_renter():
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Dữ liệu không hợp lệ'}), 400
        
        room_no = data.get('room_no')
        full_name = data.get('full_name')
    
        if not room_no or not full_name:
            return jsonify({'success': False, 'error': 'Vui lòng điền đầy đủ số phòng và họ tên khách thuê'}), 400

        # Lấy ngày hiện tại làm mặc định nếu start_date bị trống
        current_date_str = datetime.now().strftime('%Y-%m-%d')
        start_date = data.get('start_date')

        if not start_date or str(start_date).strip() == "":
            start_date = current_date_str
        else:
            try:
                # Nếu vì lý do nào đó frontend lỡ truyền định dạng dd/mm/yyyy, backend tự quy đổi
                if '/' in str(start_date):
                    start_date = datetime.strptime(str(start_date).strip(), '%d/%m/%Y').strftime('%Y-%m-%d')
                else:
                    # Kiểm tra tính hợp lệ của chuỗi định dạng yyyy-mm-dd gửi từ Flatpickr altInput
                    datetime.strptime(str(start_date).strip(), '%Y-%m-%d')
                    start_date = str(start_date).strip()
            except ValueError:
                # Fallback về ngày hiện tại nếu định dạng ngày truyền lên bị lỗi nghiêm trọng
                start_date = current_date_str

        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # 1. Chèn dữ liệu vào bảng renters (Sử dụng ngày bắt đầu start_date đã chuẩn hóa yyyy-mm-dd)
            cursor.execute('''
                INSERT INTO renters (
                    room_no, full_name, cccd, phone, company_name, tax_code, email, company_address,
                    start_date, num_people, period_type, period_value, rental_price,
                    electricity_rate, water_rate, elevator_fee, service_fee, last_elec_index, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ''', (
                room_no, 
                full_name, 
                data.get('cccd'), 
                data.get('phone'),
                data.get('company_name'), 
                data.get('tax_code'),
                data.get('email'),
                data.get('company_address'),
                start_date, # Chuỗi ngày tháng yyyy-mm-dd đã xử lý chuẩn chỉnh
                int(data.get('num_people', 1)), 
                data.get('period_type', 'month'),
                int(data.get('period_value', 1)), 
                float(data.get('rental_price') or 0),
                float(data.get('electricity_rate') or 0), 
                float(data.get('water_rate') or 0),
                float(data.get('elevator_fee') or 0), 
                float(data.get('service_fee') or 0),
                float(data.get('last_elec_index') or 0)
            ))

            # 2. Cập nhật bảng rooms: Chuyển trạng thái sang 'occupied' và cập nhật giá phòng mới
            cursor.execute('''
                UPDATE rooms 
                SET status = 'occupied', 
                    price = ? 
                WHERE room_no = ?
            ''', (float(data.get('rental_price') or 0), room_no))

            conn.commit()
            return jsonify({'success': True, 'message': 'Đã tạo hợp đồng và cập nhật trạng thái phòng thành công'})
    
        except Exception as e:
            if conn:
                conn.rollback()
            # Log lỗi chi tiết để debug trên hệ thống Vietnix khi cần thiết
            print(f"Create Renter Error for Room {room_no}: {str(e)}")
            return jsonify({'success': False, 'error': f"Lỗi hệ thống: {str(e)}"}), 500
    
        finally:
            if conn:
                conn.close()

    @app.route('/api/rental/renter/update', methods=['POST'])
    @login_required
    def update_renter():
        data = request.json
        room_no = data.get('room_no')
        new_price = data.get('rental_price')
    
        if not room_no:
            return jsonify({'success': False, 'message': 'Thiếu số phòng'}), 400

        # Lấy ngày hiện tại làm mặc định dự phòng nếu không có hoặc chuỗi ngày bị rỗng
        current_date_str = datetime.now().strftime('%Y-%m-%d')
        start_date = data.get('start_date')
    
        if not start_date or str(start_date).strip() == "":
            start_date = current_date_str
        else:
            # Kiểm tra thử và chuẩn hóa định dạng dữ liệu ngày tháng
            try:
                # Nếu client lỡ gửi định dạng dd/mm/yyyy, backend tự động chuyển về yyyy-mm-dd
                if '/' in str(start_date):
                    start_date = datetime.strptime(str(start_date).strip(), '%d/%m/%Y').strftime('%Y-%m-%d')
                else:
                    # Kiểm tra xem chuỗi yyyy-mm-dd gửi từ Flatpickr có đúng định dạng không
                    datetime.strptime(str(start_date).strip(), '%Y-%m-%d')
                    start_date = str(start_date).strip()
            except ValueError:
                # Nếu định dạng gửi lên bị lỗi không xác định, sử dụng ngày hiện tại để tránh lỗi hệ thống
                start_date = current_date_str

        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # 1. Cập nhật thông tin chi tiết khách thuê (Ngày bắt đầu đã được chuẩn hóa yyyy-mm-dd)
            sql_renter = '''
                UPDATE renters 
                SET full_name = ?, cccd = ?, phone = ?, num_people = ?, 
                    rental_price = ?, electricity_rate = ?, water_rate = ?, 
                    elevator_fee = ?, service_fee = ?, company_name = ?, 
                    tax_code = ?, company_address = ?, email = ?, 
                    last_elec_index = ?, period_type = ?, period_value = ?, start_date = ?
                WHERE room_no = ?
            '''
        
            params_renter = (
                data.get('full_name'), 
                data.get('cccd'), 
                data.get('phone'), 
                int(data.get('num_people', 1)), 
                float(new_price or 0), 
                float(data.get('electricity_rate', 0)),
                float(data.get('water_rate', 0)), 
                float(data.get('elevator_fee', 0)), 
                float(data.get('service_fee', 0)),
                data.get('company_name'), 
                data.get('tax_code'), 
                data.get('company_address'), 
                data.get('email'), 
                float(data.get('last_elec_index', 0)),
                data.get('period_type', 'month'),
                int(data.get('period_value', 12)),
                start_date, # Sử dụng biến ngày tháng đã được xử lý chuẩn hóa ở trên
                room_no
            )
        
            cursor.execute(sql_renter, params_renter)

            # 2. Đồng bộ trạng thái và giá vào bảng rooms
            cursor.execute('''
                UPDATE rooms 
                SET price = ?, status = 'occupied' 
                WHERE room_no = ?
            ''', (float(new_price or 0), room_no))

            conn.commit()
            return jsonify({'success': True, 'message': f'Cập nhật phòng {room_no} thành công!'})

        except Exception as e:
            if conn: 
                conn.rollback()
            # Log lỗi chi tiết để thuận tiện debug trên hosting Vietnix
            print(f"Update Renter Error for Room {room_no}: {str(e)}") 
            return jsonify({'success': False, 'message': f"Lỗi hệ thống: {str(e)}"}), 500
        finally:
            if conn: 
                conn.close()

    @app.route('/api/rental/renter', methods=['GET'])
    @login_required
    def get_renters():
        try:
            conn = get_db_connection()
            # Chuyển đổi hàng thành dictionary để tránh lỗi khi render JSON
            conn.row_factory = sqlite3.Row 
        
            # Lấy toàn bộ thông tin từ bảng renters
            # Đảm bảo bảng renters của bạn có cột 'tax_code'
            renters = conn.execute('SELECT * FROM renters ORDER BY room_no ASC').fetchall()
        
            results = []
            for r in renters:
                item = dict(r)
                # Đảm bảo các giá trị số không bị null để tránh lỗi khi tính toán ở JS
                item['room_no'] = item.get('room_no') or 0
                item['rental_price'] = item.get('rental_price') or 0
                item['electricity_rate'] = item.get('electricity_rate') or 0
                item['water_rate'] = item.get('water_rate') or 0
                item['elevator_fee'] = item.get('elevator_fee') or 0
                item['service_fee'] = item.get('service_fee') or 0
                item['last_elec_index'] = item.get('last_elec_index') or 0

                payment_dates = fetch_renter_payment_dates(conn, item['id'], item.get('room_no'))
                debt = compute_rental_debt(
                    item.get('start_date'),
                    payment_dates,
                    period_type=item.get('period_type') or 'month',
                )
                item['unpaid_months'] = debt['unpaid_months']
                item['unpaid_months_display'] = debt['unpaid_months_display']
                item['current_period_paid'] = debt['current_period_paid']
                item['current_billing_month'] = debt['current_billing_month']

                results.append(item)
            
            conn.close()
            return jsonify(results)
        except Exception as e:
            print(f"Error in get_renters: {str(e)}") # Xem lỗi tại terminal
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/rental/payment', methods=['POST'])
    @login_required
    def api_rental_payment():
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Không nhận được dữ liệu"}), 400

        renter_id = data.get('renter_id')
        items = data.get('items', [])

        # 1. Thu thập thông tin khách hàng
        customer_name = normalize_retail_buyer_name(data.get('customer_name'))
        company_name = data.get('company_name', '')
        tax_code = data.get('tax_code', '') 
        address = data.get('address', '')
        customer_phone = data.get('customer_phone', '')
        email = data.get('email', '')
        payment_method = data.get('payment_method', '111')
        note = data.get('note', '')
        status = 'pending' if payment_method == '112' else 'completed'
    
        # Chỉ số điện mới
        new_elec_index = data.get('new_elec_index')

        # Lấy và chuẩn hóa ngày thanh toán truyền từ Frontend (yyyy-mm-dd)
        req_payment_date = data.get('payment_date')
        now_dt = datetime.now()
    
        if req_payment_date and str(req_payment_date).strip() != "":
            try:
                parsed_date = datetime.strptime(str(req_payment_date).strip(), '%Y-%m-%d')
                sale_date = parsed_date.replace(hour=now_dt.hour, minute=now_dt.minute, second=now_dt.second).strftime('%Y-%m-%d %H:%M:%S')
                current_period = parsed_date.strftime('%Y-%m')
            except ValueError:
                sale_date = now_dt.strftime('%Y-%m-%d %H:%M:%S')
                current_period = now_dt.strftime('%Y-%m')
        else:
            sale_date = now_dt.strftime('%Y-%m-%d %H:%M:%S')
            current_period = now_dt.strftime('%Y-%m')

        if status == 'pending':
            note = json.dumps({
                'renter_id': renter_id,
                'new_elec_index': new_elec_index,
                'current_period': current_period,
                'room_no': data.get('room_no'),
                'user_note': note
            }, ensure_ascii=False)

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("BEGIN IMMEDIATE")

            # Tính tổng tiền từ danh sách khoản thu
            total_amount = sum(float(i.get('total', 0)) for i in items)

            # 2. Tạo bản ghi sale
            cursor.execute("""
                INSERT INTO sale (date, total_amount, payment_method, customer_name, company_name, 
                                 tax_code, customer_phone, address, note, status, email, business_line)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rental_service')
            """, (sale_date, total_amount, payment_method, customer_name, company_name,
                  tax_code, customer_phone, address, note, status, email))
        
            sale_id = cursor.lastrowid
            sale_no = f"ĐH{str(sale_id).zfill(6)}"
            cursor.execute("UPDATE sale SET sale_no = ? WHERE id = ?", (sale_no, sale_id))

            # 3. Ghi sale_items (Sửa lỗi khớp map Key chính xác với Frontend)
            for i in items:
                # Dự phòng lấy 'item_name' hoặc 'product_name' đề phòng thay đổi cấu trúc dữ liệu
                product_name = i.get('item_name') or i.get('product_name') or 'Khoản thu dịch vụ phòng'
                unit_price = i.get('unit_price') or i.get('price') or 0
                hkd_sector = resolve_item_hkd_sector(business_line='rental_service')

                insert_sale_item_with_sector(
                    cursor,
                    ['sale_id', 'product_name', 'quantity', 'price', 'unit', 'line_total'],
                    [
                        sale_id,
                        product_name,
                        i.get('quantity', 1),
                        unit_price,
                        i.get('unit', ''),
                        i.get('total', 0),
                    ],
                    hkd_sector_code=hkd_sector,
                )

            # 4–6. Chỉ hoàn tất nghiệp vụ khi không chờ QR chuyển khoản
            if status == 'completed':
                if renter_id:
                    if new_elec_index is not None and str(new_elec_index).strip() != "":
                        cursor.execute("""
                            UPDATE renters 
                            SET last_elec_index = ?, last_paid_period = ? 
                            WHERE id = ?
                        """, (new_elec_index, current_period, renter_id))
                    else:
                        cursor.execute("""
                            UPDATE renters 
                            SET last_paid_period = ? 
                            WHERE id = ?
                        """, (current_period, renter_id))

                if payment_method in ["111", "112"]:
                    last_pt = cursor.execute("SELECT voucher_no FROM phieu_thu WHERE voucher_no LIKE 'PT%' ORDER BY id DESC LIMIT 1").fetchone()
                    if last_pt and last_pt['voucher_no']:
                        try:
                            number_part = "".join(filter(str.isdigit, last_pt['voucher_no']))
                            pt_num = int(number_part) + 1 if number_part else 1
                        except:
                            pt_num = 1
                    else:
                        pt_num = 1

                    pt_vno = f"PT{pt_num:06d}"
                    reason = f"Thu tiền dịch vụ thuê phòng số {data.get('room_no', '')} - Hóa đơn {sale_no}"

                    cursor.execute("""
                        INSERT INTO phieu_thu (voucher_no, payer_name, address, tax_code, amount, 
                                             debit_account, credit_account, reason, reference_document, sale_id, date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (pt_vno, customer_name, address, tax_code, total_amount, payment_method, '511', reason, sale_no, sale_id, sale_date))

                elif payment_method == "131":
                    cursor.execute("""
                        INSERT INTO cong_no (customer_name, company_name, address, tax_code, debit_account, 
                                            credit_account, date_of_debt, unpaid_amount, sale_id, sale_no)
                        VALUES (?, ?, ?, ?, '131', '511', ?, ?, ?, ?)
                    """, (customer_name, company_name, address, tax_code, sale_date, total_amount, sale_id, sale_no))

            conn.commit()
            return jsonify({
                "success": True,
                "sale_id": sale_id,
                "sale_no": sale_no,
                "pending_qr": status == 'pending'
            })

        except Exception as e:
            if conn: 
                conn.rollback()
            print(f"Lỗi API Payment: {str(e)}")
            return jsonify({"success": False, "error": f"Lỗi hệ thống khi lập phiếu: {str(e)}"}), 500
        finally:
            if conn: 
                conn.close()

    @app.route('/api/rental/confirm-payment/<int:sale_id>', methods=['POST'])
    @login_required
    def confirm_rental_payment(sale_id):
        result = complete_rental_bank_payment(sale_id)
        code = 200 if result.get('success') else 400
        if result.get('error') == 'Không tìm thấy hóa đơn':
            code = 404
        return jsonify(result), code

    @app.route('/api/rooms', methods=['GET'])
    def get_rooms():
        try:
            conn = get_db_connection()
            rooms = conn.execute('SELECT * FROM rooms ORDER BY room_no ASC').fetchall()
            conn.close()
            # Chuyển đổi kết quả sang danh sách dict để Frontend đọc được
            return jsonify([dict(row) for row in rooms])
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/api/rooms/import', methods=['POST'])
    def import_rooms():
        # Fix: Kiểm tra đúng tên key gửi từ Client (là 'roomExcelFile' thay vì 'file')
        file_key = 'roomExcelFile' 
        if file_key not in request.files:
            return jsonify({'success': False, 'message': 'Không tìm thấy dữ liệu file trong request'}), 400
    
        file = request.files[file_key]
        if file.filename == '':
            return jsonify({'success': False, 'message': 'Bạn chưa chọn file Excel'}), 400
    
        conn = None
        try:
            # Đọc dữ liệu (Hỗ trợ cả .xlsx và .csv)
            if file.filename.endswith('.csv'):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)
        
            # Làm sạch tên cột (xóa khoảng trắng thừa)
            df.columns = [col.strip() for col in df.columns]
        
            # Kiểm tra các cột bắt buộc
            required_cols = ['room_no', 'price']
            if not all(col in df.columns for col in required_cols):
                return jsonify({'success': False, 'message': f'File thiếu cột bắt buộc. Cần có: {", ".join(required_cols)}'}), 400

            conn = get_db_connection()
            cursor = conn.cursor()

            for _, row in df.iterrows():
                room_no = str(row['room_no']).strip()
                # Xử lý giá tiền (loại bỏ dấu phẩy nếu có và ép kiểu)
                try:
                    price = float(str(row['price']).replace(',', ''))
                except:
                    price = 0.0

                status = str(row.get('status')).strip() if pd.notna(row.get('status')) else 'available'
                note = str(row.get('note')).strip() if pd.notna(row.get('note')) else ''

                # Thực hiện UPSERT
                cursor.execute('''
                    INSERT INTO rooms (room_no, price, status, note)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(room_no) DO UPDATE SET
                        price = excluded.price,
                        status = excluded.status,
                        note = excluded.note
                ''', (room_no, price, status, note))
        
            conn.commit()
            return jsonify({'success': True, 'message': f'Đã cập nhật thành công {len(df)} phòng vào hệ thống!'})

        except Exception as e:
            if conn:
                conn.rollback()
            return jsonify({'success': False, 'message': f'Lỗi: {str(e)}'}), 500
        finally:
            if conn:
                conn.close()

    # 3. API: Lấy thông tin người thuê theo số phòng (Dùng cho handleRoomClick)
    @app.route('/api/rental/renter-by-room/<room_no>', methods=['GET'])
    def get_renter_by_room(room_no):
        conn = get_db_connection()
        try:
            # THÊM ĐIỀU KIỆN status = 'active'
            renter = conn.execute('''
                SELECT * FROM renters 
                WHERE room_no = ? AND status = 'active'
            ''', (room_no,)).fetchone()
        
            if renter:
                item = dict(renter)
                payment_dates = fetch_renter_payment_dates(conn, item['id'], item.get('room_no'))
                debt = compute_rental_debt(
                    item.get('start_date'),
                    payment_dates,
                    period_type=item.get('period_type') or 'month',
                )
                item.update(debt)
                return jsonify(item)
            return jsonify({'error': 'Không tìm thấy khách thuê đang ở phòng này'}), 404
        finally:
            conn.close()

    @app.route('/api/rental/room/template')
    @login_required
    def download_room_template():
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Mau_Nhap_Phong"

        # ==================== HEADER (Chỉ 3 cột) ====================
        headers = ["room_no", "price", "status"]
        ws.append(headers)

        # Định dạng Header (Màu xanh đậm, chữ trắng)
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type="solid")
        header_align = Alignment(horizontal="center", vertical="center")

        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 20

        # ==================== DÒNG HƯỚNG DẪN ====================
        ws.append(["101", "4000000", "available"])
        ws.append(["102", "3500000", "occupied"])
        ws.append(["103", "3500000", "maintenance"])
    
        # Định dạng dòng ví dụ (Chữ nghiêng, màu xám)
        guide_font = Font(italic=True, color="6B7280")
        for row in ws.iter_rows(min_row=2, max_row=4):
            for cell in row:
                cell.font = guide_font

        # ==================== TRẢ FILE ====================
        output = BytesIO()
        wb.save(output)
        output.seek(0)

        return send_file(
            output,
            download_name='Mau_Danh_Sach_Phong.xlsx',
            as_attachment=True,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    @app.route('/api/rental/checkout-room/<room_no>', methods=['POST'])
    def checkout_room(room_no):
        # Khởi tạo kết nối theo tenant
        conn = get_db_connection()
        try:
            # 1. Cập nhật trạng thái phòng thành 'available'
            conn.execute(
                "UPDATE rooms SET status = 'available' WHERE room_no = ?", 
                (room_no,)
            )
        
            # 2. Cập nhật khách thuê hiện tại thành 'inactive' và ghi ngày trả phòng
            conn.execute(
                "UPDATE renters SET status = 'inactive', checkout_date = CURRENT_DATE WHERE room_no = ? AND status = 'active'", 
                (room_no,)
            )
        
            conn.commit()
            return jsonify({"success": True, "message": "Trả phòng thành công"})
        
        except Exception as e:
            # Hoàn tác nếu có lỗi để tránh sai lệch dữ liệu giữa các bảng
            conn.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            # Luôn đóng kết nối sau khi thực hiện xong để giải phóng tài nguyên
            conn.close()

    #=============================================================================== End of rental_service==================================================================================#
