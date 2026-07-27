"""Routes NCC và đơn hàng — tách từ app.py."""
import re
import sqlite3
from datetime import datetime

from flask import jsonify, render_template, request

from auth import admin_or_master_required, login_required
from db_utils import MAIN_DB_PATH, get_db_connection


def register_suppliers_orders_routes(app):

    @app.route('/suppliers')
    @login_required
    def suppliers_page():
        return render_template('suppliers.html')

    @app.route('/api/suppliers/by-tax-code/<tax_code>', methods=['GET'])
    @login_required
    def get_or_create_supplier_by_tax(tax_code):
        """
        API: Lấy hoặc tự động tạo nhà cung cấp theo mã số thuế
        - Nếu MST đã tồn tại → trả về thông tin hiện có
        - Nếu chưa tồn tại → tạo mới với tên mặc định và trả về
        """
        # Chuẩn hóa mã số thuế
        tax_code = (tax_code or "").strip().upper().replace(" ", "")

        if not tax_code:
            return jsonify({
                "success": False,
                "error": "Mã số thuế không được để trống"
            }), 400

        # Kiểm tra định dạng MST Việt Nam cơ bản (10 số hoặc 13 số, có thể có dấu gạch ngang)
        if not re.match(r'^\d{10}(-\d{3})?$|^\d{13}$', tax_code):
            return jsonify({
                "success": False,
                "error": "Mã số thuế không đúng định dạng (10 hoặc 13 số)"
            }), 400

        try:
            with get_db_connection() as conn:  # ← sử dụng hàm get_db_connection() có sẵn của bạn
                conn.row_factory = sqlite3.Row
                c = conn.cursor()

                # Tìm nhà cung cấp theo MST
                c.execute("""
                    SELECT id, name, tax_code, address, phone, email, created_at
                    FROM suppliers 
                    WHERE tax_code = ?
                """, (tax_code,))

                supplier = c.fetchone()

                if supplier:
                    return jsonify({
                        "success": True,
                        "supplier": dict(supplier),
                        "created": False,
                        "message": "Đã tìm thấy nhà cung cấp"
                    })

                # Tạo mới nếu không tìm thấy
                default_name = f"NCC {tax_code}"

                c.execute("""
                    INSERT INTO suppliers 
                    (name, tax_code, created_at)
                    VALUES (?, ?, datetime('now'))
                """, (default_name, tax_code))

                new_id = c.lastrowid
                conn.commit()

                # Lấy lại thông tin vừa tạo
                c.execute("""
                    SELECT id, name, tax_code, created_at
                    FROM suppliers 
                    WHERE id = ?
                """, (new_id,))

                new_supplier = c.fetchone()

                return jsonify({
                    "success": True,
                    "supplier": dict(new_supplier),
                    "created": True,
                    "message": "Đã tự động tạo nhà cung cấp mới"
                })

        except sqlite3.IntegrityError:
            # Trường hợp mã số thuế đã tồn tại (race condition)
            return jsonify({
                "success": False,
                "error": "Mã số thuế này đã được sử dụng"
            }), 409

        except sqlite3.Error as e:
            return jsonify({
                "success": False,
                "error": f"Lỗi cơ sở dữ liệu: {str(e)}"
            }), 500

        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Lỗi hệ thống: {str(e)}"
            }), 500

    @app.route('/order')
    @login_required
    def order():
        # order.html tự load danh sách qua /api/sale/list (bảng sale)
        return render_template('order.html')

    # === API SUPPLIERS ===
    @app.route('/api/suppliers', methods=['GET', 'POST', 'PUT', 'DELETE'])
    @login_required
    def api_suppliers():
        conn = get_db_connection()
        c = conn.cursor()
        try:
            if request.method == 'GET':
                q = request.args.get('q', '')
                if q:
                    like = f"%{q}%"
                    c.execute("SELECT * FROM suppliers WHERE code LIKE ? OR name LIKE ? OR phone LIKE ? OR tax_code LIKE ?", (like,)*4)
                else:
                    c.execute("SELECT * FROM suppliers ORDER BY name")
                return jsonify([dict(row) for row in c.fetchall()])
            data = request.get_json() or {}
            if request.method == 'POST':
                name = data.get('name', '').strip()
                if not name: return jsonify({"error": "Tên NCC trống"}), 400
                c.execute("SELECT COUNT(*) FROM suppliers")
                count = c.fetchone()[0]
                code = data.get('code', '').strip() or f"NCC{count + 1:06d}"
                c.execute("INSERT INTO suppliers (code, name, phone, email, address, note, tax_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (code, name, data.get('phone',''), data.get('email',''), data.get('address',''), data.get('note',''), data.get('tax_code','')))
                conn.commit()
                return jsonify({"success": True, "code": code})
            if request.method in ['PUT', 'DELETE']:
                id_ = data.get('id')
                if not id_: return jsonify({"error": "Thiếu ID"}), 400
                if request.method == 'PUT':
                    c.execute("UPDATE suppliers SET code=?, name=?, phone=?, email=?, address=?, note=?, tax_code=? WHERE id=?",
                              (data.get('code',''), data.get('name',''), data.get('phone',''), data.get('email',''), data.get('address',''), data.get('note',''), data.get('tax_code',''), id_))
                else:
                    c.execute("DELETE FROM suppliers WHERE id=?", (id_,))
                conn.commit()
                return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()

    #====NHẬP THÔNG TIN NHÀ CUNG CẤP VÀO BẢNG SUPPLIERS TỪ PHIẾU NHẬP===#
    @app.route('/api/suppliers/upsert', methods=['POST'])
    @login_required
    def api_suppliers_upsert():
        conn = get_db_connection()
        c = conn.cursor()
        try:
            data = request.get_json()
            tax_code = data.get('tax_code', '').strip()
            name = data.get('name', '').strip()
            address = data.get('address', '').strip()
            phone = data.get('phone', '').strip()

            # Tìm kiếm ưu tiên MST, sau đó đến Tên
            supplier_id = None
            if tax_code:
                c.execute("SELECT id FROM suppliers WHERE tax_code = ?", (tax_code,))
                res = c.fetchone()
                if res: supplier_id = res['id']

            if not supplier_id:
                c.execute("SELECT id FROM suppliers WHERE name = ?", (name,))
                res = c.fetchone()
                if res: supplier_id = res['id']

            if supplier_id:
                # Update thông tin mới nhất từ XML
                c.execute("""
                    UPDATE suppliers 
                    SET tax_code = COALESCE(NULLIF(tax_code, ''), ?), 
                        address = ?, phone = ? 
                    WHERE id = ?
                """, (tax_code, address, phone, supplier_id))
            else:
                # Tạo mới nếu hoàn toàn chưa có
                c.execute("SELECT COUNT(*) FROM suppliers")
                count = c.fetchone()[0]
                code = f"NCC{count + 1:06d}"
                c.execute("""
                    INSERT INTO suppliers (code, name, tax_code, address, phone) 
                    VALUES (?, ?, ?, ?, ?)
                """, (code, name, tax_code, address, phone))
                supplier_id = c.lastrowid

            conn.commit()
            return jsonify({"success": True, "supplier_id": supplier_id})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

    # === API SUPPLIERS IMPORT ===
    @app.route('/api/suppliers/import', methods=['POST'])
    @login_required
    @admin_or_master_required
    def api_suppliers_import():
        conn = get_db_connection()
        c = conn.cursor()
        data = request.get_json()
        imported_count = 0
        errors = []
        try:
            for item in data:
                name = item.get('name', '').strip()
                if not name:
                    errors.append({"item": item, "error": "Tên NCC trống"})
                    continue
                code = item.get('code', '').strip()
                if not code:
                    c.execute("SELECT COUNT(*) FROM suppliers")
                    count = c.fetchone()[0]
                    code = f"NCC{count + 1:06d}"
                try:
                    c.execute("INSERT INTO suppliers (code, name, phone, email, address, note, tax_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (code, name, item.get('phone',''), item.get('email',''), item.get('address',''), item.get('note',''), item.get('tax_code','')))
                    imported_count += 1
                except sqlite3.IntegrityError:
                    errors.append({"item": item, "error": "Mã NCC hoặc trường UNIQUE đã tồn tại"})
                except Exception as e:
                    errors.append({"item": item, "error": str(e)})
            conn.commit()
            return jsonify({"success": True, "count": imported_count, "errors": errors})
        except Exception as e:
            conn.rollback()
            return jsonify({"error": f"Lỗi server: {str(e)}"}), 500
        finally:
            conn.close()

    # === API ĐƠN HÀNG ===
    @app.route('/api/orders', methods=['GET', 'POST', 'PUT'])
    # @login_required # Giữ nguyên nếu bạn đang dùng decorator này
    def api_orders():
        conn = get_db_connection()

        # Quan trọng: Đảm bảo cursor trả về kết quả dưới dạng dictionary để jsonify hoạt động tốt
        conn.row_factory = sqlite3.Row 
        c = conn.cursor()

        # --- 1. POST: TẠO ĐƠN HÀNG MỚI (Nháp) ---
        if request.method == 'POST':
            data = request.get_json()

            # Lấy các trường dữ liệu, bao gồm các trường MỚI
            customer_name = data.get('customer_name', '')
            customer_phone = data.get('customer_phone', '')
            customer_taxcode = data.get('customer_taxcode', '')
            customer_address = data.get('customer_address', '')
            note = data.get('note', '')

            # Đặt giá trị mặc định cho đơn hàng mới
            total = 0 
            status = 'Hoàn Thành' 
            payment_method = data.get('payment_method', 'Tiền mặt')

            sql = """
                INSERT INTO sale (
                    date, customer_name, customer_phone, customer_taxcode, customer_address, 
                    total, payment_method, note, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
                customer_name, customer_phone, customer_taxcode, customer_address, 
                total, payment_method, note, status
            )

            try:
                c.execute(sql, params)
                conn.commit()
                return jsonify({"success": True, "id": c.lastrowid}), 201
            except Exception as e:
                conn.rollback()
                return jsonify({"success": False, "error": f"Lỗi tạo đơn hàng: {e}"}), 500


        # --- 2. PUT: CẬP NHẬT CHI TIẾT ĐƠN HÀNG ---
        if request.method == 'PUT':
            data = request.get_json()
            order_id = data.get('id')
            if not order_id:
                return jsonify({"success": False, "error": "Thiếu ID đơn hàng"}), 400

            sql = """
                UPDATE sale SET 
                    customer_name=?, customer_phone=?, customer_taxcode=?, customer_address=?, note=? 
                WHERE id=?
            """
            params = (
                data.get('customer_name',''), 
                data.get('customer_phone',''), 
                data.get('customer_taxcode',''),   # Cột mới
                data.get('customer_address',''),   # Cột mới
                data.get('note',''), 
                order_id
            )

            try:
                c.execute(sql, params)
                conn.commit()
                return jsonify({"success": True}), 200
            except Exception as e:
                conn.rollback()
                return jsonify({"success": False, "error": f"Lỗi cập nhật đơn hàng: {e}"}), 500


        # --- 3. GET: LẤY DANH SÁCH ĐƠN HÀNG (có tìm kiếm) ---
        query = request.args.get('q', '').strip()

        # Lấy TẤT CẢ các cột cần thiết cho bảng Orders
        sql = """
            SELECT 
                id, date, customer_name, customer_phone, total_amount as total, invoice_number, 
                customer_taxcode, customer_address, status, note, payment_method 
            FROM sale 
            WHERE 1=1
        """
        params = []

        if query:
            # Tìm kiếm theo tên, SĐT, hoặc số hóa đơn
            sql += " AND (customer_name LIKE ? OR customer_phone LIKE ? OR invoice_number LIKE ?)"
            params.extend([f'%{query}%', f'%{query}%', f'%{query}%'])

        sql += " ORDER BY id DESC LIMIT 100"

        try:
            c.execute(sql, tuple(params))
            # Trả về kết quả dưới dạng JSON (List of Dicts)
            return jsonify([dict(row) for row in c.fetchall()]), 200
        except Exception as e:
            return jsonify({"success": False, "error": f"Lỗi truy vấn danh sách đơn hàng: {e}"}), 500
        finally:
            # Lưu ý: Nếu bạn đang sử dụng g.db, việc đóng kết nối sẽ do @app.teardown_appcontext xử lý.
            # Nếu không, bạn cần đảm bảo conn.close() được gọi.
            if 'g' not in globals() or not hasattr(g, '_database'):
                 conn.close()

    @app.route('/api/orders/items', methods=['POST'])
    # @login_required # Giữ nguyên nếu bạn đang dùng decorator này
    def api_orders_items():
        """Lưu chi tiết (sale_items), tính tổng tiền và cập nhật total_amount cho đơn hàng."""
        conn = get_db_connection()
        c = conn.cursor()
        data = request.get_json()

        order_id = data.get('id')
        items = data.get('items', []) # Danh sách các mặt hàng (sản phẩm, số lượng, giá)

        if not order_id:
            return jsonify({"success": False, "error": "Thiếu ID đơn hàng"}), 400

        try:
            # 1. Xóa các chi tiết cũ của đơn hàng này
            c.execute("DELETE FROM sale_items WHERE sale_id=?", (order_id,))

            # 2. Thêm lại các chi tiết mới và tính tổng tiền
            grand_total = 0

            for item in items:
                # Chuyển đổi an toàn sang float
                try:
                    quantity = float(item.get('quantity', 0))
                    price = float(item.get('price', 0))
                except (TypeError, ValueError):
                    raise ValueError("Số lượng hoặc Đơn giá không hợp lệ.")

                item_total = quantity * price
                grand_total += item_total

                # Kiểm tra dữ liệu sản phẩm cơ bản
                if item.get('product_id') is None or not item.get('product_name'):
                     raise ValueError("Thiếu thông tin sản phẩm.")

                c.execute("""
                    INSERT INTO sale_items (sale_id, product_id, product_name, unit_name, quantity, price, total) 
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id, 
                    item.get('product_id'), 
                    item.get('product_name'), 
                    item.get('unit_name', 'Cái'), # Giá trị mặc định là 'Cái' nếu không có
                    quantity, 
                    price, 
                    item_total
                ))

            # 3. Cập nhật cột total_amount (theo tên cột CSDL của bạn) vào bảng sale
            c.execute("UPDATE sale SET total_amount=? WHERE id=?", (grand_total, order_id))

            conn.commit()
            return jsonify({"success": True, "total_amount": grand_total}), 200

        except Exception as e:
            conn.rollback()
            error_message = str(e) if isinstance(e, ValueError) else f"Lỗi xử lý chi tiết đơn hàng: {e}"
            return jsonify({"success": False, "error": error_message}), 500
        finally:
            # Đảm bảo đóng kết nối
            conn.close()

    @app.route('/api/orders', methods=['POST'])
    def api_create_order():
        data = request.get_json()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO orders (customer_name, customer_phone, note, total, status)
            VALUES (?, ?, ?, 0, 'pending')
        """, (data['customer_name'], data.get('customer_phone'), data.get('note')))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.route('/api/orders/list')
    def api_orders_list():
        # Lấy tham số
        start = request.args.get('start') # Ví dụ: "2025-11-23"
        end = request.args.get('end')     # Ví dụ: "2025-11-23"
        q = request.args.get('q', '').strip()

        # --- SỬA LỖI LỆCH NGÀY QUAN TRỌNG NHẤT ---
        # Thêm 00:00:00 cho ngày bắt đầu và 23:59:59 cho ngày kết thúc
        start_of_day = f"{start} 00:00:00" if start else None
        end_of_day = f"{end} 23:59:59" if end else None
        # ----------------------------------------

        try:
            # 1. Sử dụng 'with' để quản lý kết nối an toàn
            with sqlite3.connect(MAIN_DB_PATH) as conn:
                # Thiết lập để trả về kết quả dưới dạng dictionary
                conn.row_factory = sqlite3.Row
                c = conn.cursor()

                sql = """
                    SELECT id, customer_name, total_amount, date, invoice_number, status
                    FROM sale
                    WHERE 1=1
                """
                params = []

                # Áp dụng ngày bắt đầu
                if start_of_day:
                    sql += " AND date >= ?"
                    params.append(start_of_day)

                # Áp dụng ngày kết thúc (ĐÃ FIX)
                if end_of_day:
                    sql += " AND date <= ?"
                    params.append(end_of_day)

                if q:
                    like = f"%{q}%"
                    sql += " AND (customer_name LIKE ? OR invoice_number LIKE ? OR CAST(id AS TEXT) LIKE ?)"
                    params += [like, like, like]

                # Giữ nguyên việc giới hạn kết quả (LIMIT 500) để ngăn chặn việc tải quá nhiều dữ liệu
                sql += " ORDER BY date DESC, id DESC LIMIT 500"

                rows = c.execute(sql, params).fetchall()

                # 2. Chuyển đổi kết quả (đã tối ưu: sử dụng list comprehension)
                data = [dict(r) for r in rows]
                return jsonify(data)

        except sqlite3.Error as e:
            print(f"Lỗi Database khi lấy danh sách đơn hàng: {e}")
            return jsonify({"success": False, "message": f"Lỗi cơ sở dữ liệu: {e}"}), 500
        except Exception as e:
            print(f"Lỗi không xác định khi lấy danh sách đơn hàng: {e}")
            return jsonify({"success": False, "message": "Lỗi máy chủ không xác định."}), 500


    # 3. Trang XUẤT E-INVOICE (in trực tiếp)

    @app.route("/api/orders/<int:id>", methods=["DELETE"])
    def delete_order(id):
        try:
            # Sử dụng 'with' để đảm bảo kết nối được đóng (conn.close()) dù có lỗi hay không.
            with sqlite3.connect(MAIN_DB_PATH) as conn:
                # Tự động bật chế độ commit
                # (Trong khối 'with' mặc định, commit sẽ được gọi khi khối kết thúc thành công, 
                # nhưng tốt hơn là nên gọi tường minh hoặc kiểm soát)

                c = conn.cursor()

                # Kiểm tra xem có bản ghi nào bị xóa không
                c.execute("DELETE FROM sale WHERE id = ?", (id,))

                # Lưu các thay đổi (commit transaction)
                conn.commit()

                # Kiểm tra số lượng hàng bị ảnh hưởng
                if c.rowcount == 0:
                    # Nếu không có hàng nào bị xóa (ID không tồn tại)
                    return jsonify({"success": False, "message": f"Không tìm thấy đơn hàng có ID: {id}"}), 404

                return jsonify({"success": True, "message": f"Đã xóa đơn hàng ID: {id}"})

        except sqlite3.Error as e:
            # Bắt lỗi database (ví dụ: database bị khóa, lỗi I/O, v.v.)
            print(f"Lỗi Database khi xóa đơn hàng ID {id}: {e}")
            return jsonify({"success": False, "message": f"Lỗi cơ sở dữ liệu: {e}"}), 500
        except Exception as e:
            # Bắt các lỗi chung khác
            print(f"Lỗi không xác định khi xóa đơn hàng ID {id}: {e}")
            return jsonify({"success": False, "message": "Lỗi máy chủ không xác định."}), 500

    @app.route("/api/orders/upsert", methods=["POST"])
    def upsert_order():
        data = request.json

        # 1. Kiểm tra dữ liệu đầu vào (Validation)
        customer_name = data.get("customer_name", "").strip()
        if not customer_name:
            return jsonify({"success": False, "message": "Tên khách hàng là bắt buộc."}), 400 # Bad Request

        try:
            # 2. Sử dụng 'with' và quản lý giao dịch
            with sqlite3.connect(MAIN_DB_PATH) as conn:
                c = conn.cursor()

                # Ghi chú: Nếu invoice_number không phải là Autoincrement, 
                # bạn nên tạo logic đánh số hóa đơn ở đây thay vì dùng None.
                c.execute("""
                    INSERT INTO sale (invoice_number, customer_name, date, total_amount, status)
                    VALUES (?, ?, DATE('now'), 0, 'Nháp')
                """, (None, customer_name))

                conn.commit()
                new_id = c.lastrowid

                return jsonify({"success": True, "id": new_id})

        except sqlite3.Error as e:
            # Lỗi xảy ra, transaction sẽ tự động được rollback (trong hầu hết các trường hợp SQLite)
            print(f"Lỗi Database khi tạo đơn hàng mới: {e}")
            return jsonify({"success": False, "message": f"Lỗi cơ sở dữ liệu: {e}"}), 500
        except Exception as e:
            print(f"Lỗi không xác định khi tạo đơn hàng mới: {e}")
            return jsonify({"success": False, "message": "Lỗi máy chủ không xác định."}), 500

    @app.route('/api/orders', methods=['PUT'])
    def api_update_order():
        data = request.get_json()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE orders 
            SET customer_name=?, customer_phone=?, note=?
            WHERE id=?
        """, (data['customer_name'], data.get('customer_phone'), data.get('note'), data['id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.route('/api/orders/invoice', methods=['POST'])
    def api_save_invoice():
        data = request.get_json()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET invoice_number=? WHERE id=?", (data['invoice_number'], data['id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.route('/api/orders/invoice', methods=['POST'])
    # @login_required # Giữ nguyên nếu bạn đang dùng decorator này
    def api_update_invoice():
        """Cập nhật Số hóa đơn và trạng thái cho đơn hàng."""
        conn = get_db_connection()
        c = conn.cursor()
        data = request.get_json()

        order_id = data.get('id')
        invoice_number = data.get('invoice_number', '').strip()

        if not order_id or not invoice_number:
            return jsonify({"success": False, "error": "Thiếu ID đơn hàng hoặc Số hóa đơn"}), 400

        # Thực hiện cập nhật Số Hóa Đơn và chuyển trạng thái sang "Hoàn thành"
        sql = """
            UPDATE sale SET 
                invoice_number=?, 
                status='Hoàn thành' 
            WHERE id=?
        """
        params = (invoice_number, order_id)

        try:
            c.execute(sql, params)
            if c.rowcount == 0:
                 return jsonify({"success": False, "error": "Không tìm thấy đơn hàng để cập nhật"}), 404

            conn.commit()
            return jsonify({"success": True, "id": order_id}), 200
        except Exception as e:
            conn.rollback()
            return jsonify({"success": False, "error": f"Lỗi cập nhật số hóa đơn: {e}"}), 500
        finally:
            conn.close()
