"""Routes sản phẩm — tách từ app.py."""
import os
import sqlite3

from flask import jsonify, render_template, request
from flask_login import login_required

from db_utils import get_db_connection
from Services.hkd_sector import HKD_SECTORS, normalize_nn_code, resolve_hkd_sector


def _assign_goods_codes(c, product_id, unit1=None, external_barcode=None, external_barcode1=None):
    from Services.import_line_helpers import assign_product_codes
    code, barcode, _b1 = assign_product_codes(
        c, product_id, 'goods', unit1,
        external_barcode=external_barcode,
        external_barcode1=external_barcode1,
    )
    return code, barcode


def _next_seq_product_code(c, prefix):
    """Sinh mã DV001, TP001… theo max hiện có với prefix cho trước."""
    from Services.import_line_helpers import _max_seq_with_prefix
    width = 3 if prefix.upper() in ('TP', 'DV') else 4
    return _max_seq_with_prefix(c, prefix, width)


def _next_tp_product_code(c):
    return _next_seq_product_code(c, 'TP')


def _assign_finished_goods_codes(c, product_id, unit1=None):
    """Mã thành phẩm giống products.html: TP001, barcode TP00101 / TP00102."""
    from Services.import_line_helpers import assign_product_codes
    code, barcode, _barcode1 = assign_product_codes(
        c, product_id, 'finished_goods', unit1,
    )
    return code, barcode


def _assign_service_codes(c, product_id, external_barcode=None):
    from Services.import_line_helpers import assign_product_codes
    code, barcode, _b1 = assign_product_codes(
        c, product_id, 'service', None, external_barcode=external_barcode,
    )
    return code, barcode


def _normalize_product_code(raw):
    return (raw or '').strip().upper()


def _product_code_taken(c, product_code, exclude_id=None):
    if not product_code:
        return False
    if exclude_id:
        row = c.execute(
            "SELECT id FROM products WHERE product_code = ? AND id != ?",
            (product_code, exclude_id),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT id FROM products WHERE product_code = ?",
            (product_code,),
        ).fetchone()
    return row is not None


def _is_subscription_plan_row(c, product_id):
    try:
        row = c.execute(
            "SELECT COALESCE(is_subscription_plan, 0) FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        return bool(row and row[0])
    except sqlite3.OperationalError:
        return False


def get_product_list_with_stock(query=None):
    """
    Truy vấn sản phẩm và LEFT JOIN với tồn kho (inventory) để lấy số lượng.
    Bao gồm cột barcode1 và xử lý lỗi chi tiết.
    """
    conn = get_db_connection()
    if conn is None:
        return jsonify({"success": False, "error": "Không thể kết nối cơ sở dữ liệu."}), 500
        
    c = conn.cursor()
    
    sql = """
        SELECT
            p.id, p.name, p.product_code, p.barcode, p.base_price, p.unit, 
            p.unit1, p.unit_ratio, p.price as sale_price,
            p.barcode1, p.sell_by_weight, p.weight_plu,
            COALESCE(p.product_type, 'goods') AS product_type,
            COALESCE(i.quantity, 0) AS quantity
        FROM products p
        LEFT JOIN inventory i ON p.id = i.product_id
        WHERE 1=1
    """
    params = []
    
    if query:
        # Tìm kiếm trong 3 cột: tên, mã vạch cơ bản, hoặc mã vạch đơn vị bán
        sql += " AND (p.name LIKE ? OR p.barcode LIKE ? OR p.barcode1 LIKE ? OR p.product_code LIKE ?)"
        params.extend([f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%'])
    
    sql += " LIMIT 50"
        
    try:
        c.execute(sql, tuple(params))
        products = c.fetchall()
        
        result = [dict(row) for row in products]
        
        return jsonify(result), 200
        
    except sqlite3.OperationalError as e:
        # Xử lý lỗi SQL cụ thể, ví dụ: 'no such column' hoặc 'no such table'
        conn.rollback()
        print(f"LỖI VẬN HÀNH SQL (Kiểm tra tên cột/bảng): {e}")
        return jsonify({"success": False, "error": f"Lỗi truy vấn SQL: {e}. Vui lòng kiểm tra console server."}), 500
    except Exception as e:
        # Xử lý các lỗi khác
        conn.rollback()
        print(f"LỖI HỆ THỐNG KHÁC KHI TÌM KIẾM: {e}")
        return jsonify({"success": False, "error": f"Lỗi hệ thống không xác định: {e}"}), 500
    finally:
        conn.close()


def register_products_routes(app):
    @app.route('/api/scan', methods=['POST'])
    def scan_barcode():
        barcode = (request.json.get('barcode') or '').strip()
        conn = get_db_connection()
        try:
            from Services.scale_service import resolve_weight_scan
            from Services.product_barcode import find_product_by_scan, scan_matches_barcode1

            weight_scan = resolve_weight_scan(barcode)
            if weight_scan.get('success'):
                return jsonify(weight_scan)

            product = find_product_by_scan(conn, barcode)
            if product:
                is_unit1 = scan_matches_barcode1(barcode, product['barcode1'])
                sell_by_weight = int((product['sell_by_weight'] if 'sell_by_weight' in product.keys() else 0) or 0) == 1
                product_type = ((product['product_type'] if 'product_type' in product.keys() else None) or 'goods').lower()
                is_service = product_type == 'service'
                stock = float(product['quantity'] if 'quantity' in product.keys() else 0)
                if is_unit1 and not is_service:
                    ratio = float(product['unit_ratio'] or 1) or 1.0
                    max_qty = int(stock / ratio) if ratio else int(stock)
                else:
                    max_qty = 999999 if is_service else int(stock)
                return jsonify({
                    "success": True,
                    "data": {
                        "id": product['id'],
                        "name": product['name'],
                        "unit": product['unit1'] if is_unit1 else product['unit'],
                        "price": product['price'] if is_unit1 else product['base_price'],
                        "useUnit1": is_unit1,
                        "ratio": product['unit_ratio'],
                        "sellByWeight": sell_by_weight,
                        "weightPlu": product['weight_plu'],
                        "product_type": product_type,
                        "maxQty": max_qty,
                        "barcode": product['barcode'],
                        "barcode1": product['barcode1'],
                        "product_code": product['product_code'],
                    }
                })
            return jsonify({"success": False, "message": "Không tìm thấy sản phẩm"}), 404
        finally:
            conn.close()

    @app.route('/api/products/<int:product_id>/attach-barcode', methods=['POST'])
    @login_required
    def api_attach_product_barcode(product_id):
        """Gắn tem NSX vào SP đã có — không đụng tên/giá (luồng sửa phiếu + camera)."""
        data = request.get_json(silent=True) or {}
        barcode = (data.get('barcode') or '').strip()
        barcode1 = (data.get('barcode1') or '').strip() or None
        if not barcode and not barcode1:
            return jsonify({"success": False, "error": "Thiếu mã vạch"}), 400
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute(
                "SELECT id, product_type, unit1, name FROM products WHERE id = ?",
                (product_id,),
            )
            row = c.fetchone()
            if not row:
                return jsonify({"success": False, "error": "Không tìm thấy sản phẩm"}), 404
            p = dict(row)
            ptype = (p.get('product_type') or 'goods').strip() or 'goods'
            if ptype in ('ready_made', 'raw_materials'):
                ptype = 'goods'
            from Services.import_line_helpers import assign_product_codes
            code, bc, b1 = assign_product_codes(
                c, product_id, ptype, p.get('unit1'),
                external_barcode=barcode or None,
                external_barcode1=barcode1,
            )
            conn.commit()
            return jsonify({
                "success": True,
                "product_id": product_id,
                "product_code": code,
                "barcode": bc,
                "barcode1": b1,
            })
        except ValueError as e:
            conn.rollback()
            return jsonify({"success": False, "error": str(e)}), 400
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return jsonify({"success": False, "error": f"Mã vạch trùng: {e}"}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/products/lookup-scan', methods=['POST', 'GET'])
    @login_required
    def api_lookup_scan():
        """Tra cứu SP theo tem NSX / mã nội bộ — dùng phiếu nhập kho."""
        if request.method == 'GET':
            barcode = (request.args.get('barcode') or request.args.get('q') or '').strip()
        else:
            data = request.get_json(silent=True) or {}
            barcode = (data.get('barcode') or data.get('q') or '').strip()
        if not barcode:
            return jsonify({"success": False, "error": "Thiếu mã vạch"}), 400
        conn = get_db_connection()
        try:
            from Services.product_barcode import find_product_by_scan, scan_matches_barcode1
            row = find_product_by_scan(conn, barcode)
            if not row:
                return jsonify({
                    "success": True,
                    "found": False,
                    "barcode": barcode,
                    "product": None,
                })
            product = dict(row)
            product['matched_wholesale'] = scan_matches_barcode1(barcode, product.get('barcode1'))
            return jsonify({
                "success": True,
                "found": True,
                "barcode": barcode,
                "product": product,
            })
        finally:
            conn.close()

    @app.route('/api/products/manage', methods=['GET', 'POST', 'PUT', 'DELETE'])
    @login_required
    def product_manage():
        conn = get_db_connection()
        c = conn.cursor()

        try:
            # ==================== GET: TRUY VẤN DANH SÁCH ====================
            if request.method == 'GET':
                q = request.args.get('q', '')
                c.execute("""
                    SELECT 
                        p.*,
                        COALESCE(i.quantity, 0) AS quantity,
                        COALESCE(i.avg_cost, 0) AS avg_cost
                    FROM products p
                    LEFT JOIN inventory i ON p.id = i.product_id
                    WHERE p.name LIKE ? OR p.barcode LIKE ? OR p.product_code LIKE ?
                    ORDER BY p.id DESC
                """, (f'%{q}%', f'%{q}%', f'%{q}%'))
                products = c.fetchall()
                return jsonify([dict(row) for row in products])

            # Lấy dữ liệu JSON từ body
             # Lấy dữ liệu JSON từ body (dùng cho POST, PUT, DELETE)
            data = request.get_json(silent=True) or {}
            product_id = data.get('id')

            # ==================== POST: THÊM MỚI ====================
            if request.method == 'POST':
                name = data.get('name', '').strip()
                if not name:
                    return jsonify({"success": False, "error": "Tên sản phẩm bắt buộc"}), 400

                item_kind = (data.get('item_kind') or 'goods').strip().lower()

                if item_kind == 'service':
                    unit = (data.get('unit') or 'Lần').strip()
                    base_price = float(data.get('base_price') or 0)
                    hkd_sector = normalize_nn_code(
                        (data.get('hkd_sector_code') or '').strip() or 'NN2',
                        default='NN2',
                    )
                    if hkd_sector not in HKD_SECTORS:
                        return jsonify({"success": False, "error": "Vui lòng chọn nhóm ngành (NN1–NN4)"}), 400

                    c.execute("""
                        INSERT INTO products (
                            name, unit, base_price, price, product_type, hkd_sector_code, sell_by_weight
                        ) VALUES (?, ?, ?, 0, 'service', ?, 0)
                    """, (name, unit, base_price, hkd_sector))
                    new_id = c.lastrowid
                    c.execute("INSERT OR IGNORE INTO inventory (product_id, quantity, avg_cost) VALUES (?, 0, 0)", (new_id,))
                    code, barcode = _assign_service_codes(
                        c, new_id, external_barcode=(data.get('barcode') or '').strip() or None,
                    )
                    conn.commit()
                    return jsonify({"success": True, "id": new_id, "product_code": code, "barcode": barcode})

                unit1 = data.get('unit1') or None
                sell_by_weight = 1 if str(data.get('sell_by_weight', 0)) in ('1', 'true', True) else 0
                weight_plu = (data.get('weight_plu') or '').strip() or None
                unit = data.get('unit', 'kg' if sell_by_weight else 'Cái')

                if item_kind == 'finished_goods':
                    c.execute("""
                        INSERT INTO products (
                            name, unit, base_price, price, unit1, unit_ratio,
                            sell_by_weight, weight_plu, product_type, hkd_sector_code
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'finished_goods', 'G3')
                    """, (
                        name, unit, data.get('base_price', 0), data.get('price', 0), unit1,
                        data.get('unit_ratio', 1), sell_by_weight, weight_plu,
                    ))
                    new_id = c.lastrowid
                    c.execute("INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?, 0, 0)", (new_id,))
                    from Services.import_line_helpers import assign_product_codes
                    code, barcode, _b1 = assign_product_codes(
                        c, new_id, 'finished_goods', unit1,
                        external_barcode=(data.get('barcode') or '').strip() or None,
                        external_barcode1=(data.get('barcode1') or '').strip() or None,
                    )
                    conn.commit()
                    return jsonify({"success": True, "id": new_id, "product_code": code, "barcode": barcode})

                c.execute("""
                    INSERT INTO products (
                        name, unit, base_price, price, unit1, unit_ratio,
                        sell_by_weight, weight_plu, product_type, hkd_sector_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'goods', 'G1')
                """, (
                    name, unit, data.get('base_price', 0), data.get('price', 0), unit1,
                    data.get('unit_ratio', 1), sell_by_weight, weight_plu,
                ))
                new_id = c.lastrowid
                c.execute("INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?, 0, 0)", (new_id,))
                code, barcode = _assign_goods_codes(
                    c, new_id, unit1,
                    external_barcode=(data.get('barcode') or '').strip() or None,
                    external_barcode1=(data.get('barcode1') or '').strip() or None,
                )
                conn.commit()
                return jsonify({"success": True, "id": new_id, "product_code": code, "barcode": barcode})

            # ==================== PUT: CẬP NHẬT ====================
            elif request.method == 'PUT':
                product_id = data.get('id')
                if not product_id:
                    return jsonify({"success": False, "error": "Thiếu ID sản phẩm"}), 400

                item_kind = (data.get('item_kind') or '').strip().lower()
                c.execute("SELECT product_type FROM products WHERE id = ?", (product_id,))
                row = c.fetchone()
                current_type = (row[0] or '').strip() if row else ''
                is_service = item_kind == 'service' or current_type == 'service'
                is_finished = item_kind == 'finished_goods' or current_type == 'finished_goods'

                if is_service:
                    hkd_sector = normalize_nn_code(
                        (data.get('hkd_sector_code') or '').strip() or 'NN2',
                        default='NN2',
                    )
                    if hkd_sector not in HKD_SECTORS:
                        return jsonify({"success": False, "error": "Vui lòng chọn nhóm ngành (NN1–NN4)"}), 400

                    base_price = float(data.get('base_price') or 0)
                    new_code = _normalize_product_code(data.get('product_code'))
                    is_sub_plan = _is_subscription_plan_row(c, product_id)
                    if new_code and _product_code_taken(c, new_code, product_id):
                        return jsonify({"success": False, "error": f"Mã dịch vụ '{new_code}' đã tồn tại"}), 400

                    if is_sub_plan:
                        c.execute("""
                            UPDATE products SET
                                name=?, unit=?, base_price=?, price=?,
                                product_type='service', hkd_sector_code=?,
                                unit1=NULL, unit_ratio=1, sell_by_weight=0, weight_plu=NULL
                            WHERE id=?
                        """, (
                            data['name'], data.get('unit', 'Lần'), base_price, base_price,
                            hkd_sector, product_id,
                        ))
                    else:
                        c.execute("""
                            UPDATE products SET
                                name=?, unit=?, base_price=?, price=0,
                                product_type='service', hkd_sector_code=?,
                                unit1=NULL, unit_ratio=1, sell_by_weight=0, weight_plu=NULL
                            WHERE id=?
                        """, (
                            data['name'], data.get('unit', 'Lần'), base_price,
                            hkd_sector, product_id,
                        ))

                    if new_code:
                        barcode = f"{new_code}01"
                        c.execute(
                            "UPDATE products SET product_code=?, barcode=? WHERE id=?",
                            (new_code, barcode, product_id),
                        )
                elif is_finished:
                    c.execute("""UPDATE products SET 
                                 name=?, unit=?, base_price=?, price=?, unit1=?, unit_ratio=?,
                                 sell_by_weight=?, weight_plu=?,
                                 product_type='finished_goods', hkd_sector_code='G3'
                                 WHERE id=?""",
                              (data['name'], data.get('unit', 'Cái'), data.get('base_price', 0), data.get('price', 0),
                               data.get('unit1'), data.get('unit_ratio', 1),
                               1 if str(data.get('sell_by_weight', 0)) in ('1', 'true', True) else 0,
                               (data.get('weight_plu') or '').strip() or None,
                               product_id))
                    # Gán mã TP001 / barcode TP00101 nếu chưa có
                    c.execute(
                        "SELECT product_code, unit1 FROM products WHERE id = ?",
                        (product_id,),
                    )
                    prow = c.fetchone()
                    existing_code = (prow[0] if prow else '') or ''
                    if not str(existing_code).strip() or ('barcode' in data) or ('barcode1' in data):
                        from Services.import_line_helpers import assign_product_codes
                        assign_product_codes(
                            c, product_id, 'finished_goods',
                            (prow[1] if prow else None) or data.get('unit1'),
                            external_barcode=(data.get('barcode') or '').strip() or None,
                            external_barcode1=(data.get('barcode1') or '').strip() or None,
                        )
                else:
                    c.execute("""UPDATE products SET 
                                 name=?, unit=?, base_price=?, price=?, unit1=?, unit_ratio=?,
                                 sell_by_weight=?, weight_plu=?
                                 WHERE id=?""",
                              (data['name'], data.get('unit', 'Cái'), data.get('base_price', 0), data.get('price', 0),
                               data.get('unit1'), data.get('unit_ratio', 1),
                               1 if str(data.get('sell_by_weight', 0)) in ('1', 'true', True) else 0,
                               (data.get('weight_plu') or '').strip() or None,
                               product_id))
                    if 'barcode' in data or 'barcode1' in data:
                        from Services.import_line_helpers import assign_product_codes
                        assign_product_codes(
                            c, product_id, 'goods', data.get('unit1'),
                            external_barcode=(data.get('barcode') or '').strip() or None,
                            external_barcode1=(data.get('barcode1') or '').strip() or None,
                        )
                conn.commit()
                return jsonify({"success": True, "id": product_id})

            # ==================== DELETE: XÓA SẢN PHẨM ====================
            elif request.method == 'DELETE':
                # Ưu tiên lấy ID từ Query String (args) hoặc JSON data
                p_id = request.args.get('id') or data.get('id')
                if not p_id:
                    return jsonify({"success": False, "error": "Thiếu ID sản phẩm"}), 400

                if _is_subscription_plan_row(c, p_id):
                    return jsonify({
                        "success": False,
                        "error": "Không thể xóa gói subscription hệ thống (DV001–DV004). Chỉ sửa tên/mã/giá trên form dịch vụ.",
                    }), 400

                # --- Sửa lỗi max_id is not defined ---
                res_max = c.execute("SELECT MAX(id) FROM products").fetchone()
                max_id_val = res_max[0] if res_max[0] else 0

                # 1. Kiểm tra phát sinh giao dịch (Dùng bảng sale_items của bạn)
                c.execute("SELECT 1 FROM sale_items WHERE product_id = ? LIMIT 1", (p_id,))
                if c.fetchone():
                    return jsonify({"success": False, "error": "Sản phẩm đã có trong hóa đơn bán hàng"}), 400

                # 2. Kiểm tra tồn kho (Bảng inventory và stock_moves)
                c.execute("SELECT 1 FROM stock_moves WHERE product_id = ? LIMIT 1", (p_id,))
                if c.fetchone():
                    return jsonify({"success": False, "error": "Sản phẩm đã phát sinh lịch sử nhập/xuất kho"}), 400

                # 3. Thực hiện xóa liên hoàn (Cascade delete thủ công)
                prod = c.execute(
                    "SELECT id, name, product_code FROM products WHERE id = ?", (p_id,)
                ).fetchone()
                c.execute("DELETE FROM inventory WHERE product_id = ?", (p_id,))
                c.execute("DELETE FROM products WHERE id = ?", (p_id,))

                # 4. Logic làm sạch Auto-increment Sequence nếu xóa sản phẩm cuối
                if int(p_id) == max_id_val:
                    c.execute("SELECT MAX(id) FROM products")
                    new_max = c.fetchone()[0] or 0
                    c.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'products'", (new_max,))

                conn.commit()

                from Services.audit_log import write_audit
                if prod:
                    write_audit(
                        'delete', 'products',
                        f"Xóa sản phẩm {prod['name']}",
                        entity_type='product', entity_id=p_id,
                        entity_label=prod['name'],
                        old_data=dict(prod),
                    )

                return jsonify({"success": True, "message": "Đã xóa sản phẩm thành công"})

        except ValueError as e:
            if conn: conn.rollback()
            return jsonify({"success": False, "error": str(e)}), 400
        except sqlite3.Error as e:
            if conn: conn.rollback()
            return jsonify({"success": False, "error": f"Lỗi DB: {str(e)}"}), 500
        except Exception as e:
            if conn: conn.rollback()
            return jsonify({"success": False, "error": f"Lỗi hệ thống: {str(e)}"}), 500
        finally:
            if conn: conn.close()

    # API BATCH CẬP NHẬT GIÁ
    @app.route('/api/products/batch_update', methods=['POST'])
    @login_required
    def batch_update_products():
        updates = request.json.get('updates', [])
        if not updates: return jsonify({"success": True})
        conn = get_db_connection()
        c = conn.cursor()
        try:
            for upd in updates:
                pid = upd['product_id']
                base_price = float(upd.get('base_price', 0))
                unit_ratio = int(upd.get('unit_ratio', 1))
                unit1 = upd.get('unit1', '').strip()
                price = float(upd.get('price', 0))
                c.execute("""
                    UPDATE products SET base_price=?, price=?, unit1=?, unit_ratio=?
                    WHERE id=?
                """, (base_price, price, unit1, unit_ratio, pid))
            conn.commit()
            return jsonify({"success": True})
        except Exception as e:
            conn.rollback()
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()

    # === API: CẬP NHẬT GIÁ BÁN SẢN PHẨM ===
    @app.route('/api/products/update_prices', methods=['PUT'])
    # @login_required
    # @admin_or_master_required
    def update_product_prices():
        conn = get_db_connection()
        c = conn.cursor()
        try:
            price_updates = request.get_json()
            if not isinstance(price_updates, list):
                return jsonify({'success': False, 'error': 'Dữ liệu phải là một mảng'}), 400

            for update in price_updates:
                product_id = update.get('id')
                if not product_id:
                    continue

                # Xây dựng câu lệnh UPDATE động chỉ cho các trường được cung cấp
                set_clauses = []
                params = []

                if update.get('base_price') is not None:
                    set_clauses.append("base_price = ?")
                    params.append(update['base_price'])

                if update.get('unit1') is not None:
                    set_clauses.append("unit1 = ?")
                    params.append(update['unit1'])

                if update.get('unit_ratio') is not None:
                    set_clauses.append("unit_ratio = ?")
                    params.append(update['unit_ratio'])

                if update.get('price') is not None:
                    set_clauses.append("price = ?")
                    params.append(update['price'])

                if not set_clauses:
                    continue # Bỏ qua nếu không có trường nào để cập nhật

                sql = f"UPDATE products SET {', '.join(set_clauses)} WHERE id = ?"
                params.append(product_id)

                c.execute(sql, tuple(params))

            conn.commit()
            return jsonify({'success': True})


        except sqlite3.Error as e:
            conn.rollback()
            return jsonify({'success': False, 'error': f'Lỗi Database khi cập nhật giá: {e}'}), 500    
        finally:
            # close_db(conn)
            pass

    @app.route('/api/products', methods=['GET'])
    def api_products():
        query = request.args.get('q', '').strip()
        # Nếu có tham số exact=1, ta sẽ lọc chính xác tên
        exact = request.args.get('exact', '0') == '1'
        return get_product_list_with_stock(query=query)

    @app.route('/api/products/barcode/<barcode>', methods=['GET'])
    def api_get_product_by_barcode(barcode):
        conn = get_db_connection()
        try:
            from Services.product_barcode import find_product_by_scan
            row = find_product_by_scan(conn, barcode)
            if not row:
                return jsonify(None), 404
            return jsonify(dict(row))
        finally:
            conn.close()

    @app.route('/api/products/next-code', methods=['GET'])
    def api_products_next_code():
        product_type = (request.args.get('type') or 'goods').strip().lower()
        conn = get_db_connection()
        try:
            from Services.import_line_helpers import peek_next_product_code
            code = peek_next_product_code(conn.cursor(), product_type)
            if not code:
                return jsonify({'success': False, 'error': 'Loại hàng không hỗ trợ preview mã'}), 400
            pt = product_type
            if pt == 'materials':
                barcode, barcode1 = f"{code}01", f"{code}02"
            elif pt in ('fixed_asset', 'tools', 'service'):
                barcode, barcode1 = code, None
            else:
                barcode, barcode1 = f"{code}01", f"{code}02"
            return jsonify({
                'success': True,
                'product_type': product_type,
                'next_code': code,
                'barcode': barcode,
                'barcode1': barcode1,
            })
        finally:
            conn.close()

    @app.route('/api/products/upsert', methods=['POST'])
    def api_upsert_product():
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Không nhận được dữ liệu JSON"}), 400

        product_id = data.get('id')
        name = (data.get('name') or '').strip()
        unit = (data.get('unit') or 'Cái').strip()
        base_price = float(data.get('base_price') or 0)
        buyprice = float(data.get('buyprice') or 0)
        import_id = float(data.get('import_id') or 0)


        # Thông tin sỉ
        unit1 = (data.get('unit1') or '').strip() or None
        unit_ratio = float(data.get('unit_ratio') or 1)
        price = float(data.get('price') or 0) # Giá bán sỉ

        if not name:
            return jsonify({"success": False, "error": "Thiếu tên sản phẩm"}), 400

        from Services.hkd_sector import resolve_hkd_sector

        requested_type = (data.get('product_type') or 'ready_made').strip()

        conn = get_db_connection()
        c = conn.cursor()
        try:
            if product_id:
                c.execute("SELECT product_type FROM products WHERE id = ?", (product_id,))
                row = c.fetchone()
                current_type = (row[0] or '').strip() if row else ''

                c.execute("""
                    UPDATE products SET 
                    name=?, unit=?, buyprice=?, base_price=?, unit1=?, unit_ratio=?, price=?, import_id=?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (name, unit, buyprice, base_price, unit1, unit_ratio, price, import_id, product_id))

                # Luồng import HKD: chỉ gán goods/G1 khi SP chưa có loại hoặc đã là goods
                if requested_type == 'goods' and current_type in ('', 'goods'):
                    c.execute(
                        "UPDATE products SET product_type=?, hkd_sector_code=? WHERE id=?",
                        ('goods', 'G1', product_id),
                    )
                elif requested_type in ('materials', 'fixed_asset', 'tools', 'service'):
                    sector = resolve_hkd_sector(requested_type)
                    c.execute(
                        "UPDATE products SET product_type=?, hkd_sector_code=? WHERE id=?",
                        (requested_type, sector, product_id),
                    )
                ext_bc = (data.get('barcode') or '').strip() or None
                ext_b1 = (data.get('barcode1') or '').strip() or None
                if ext_bc or ext_b1:
                    from Services.import_line_helpers import assign_product_codes
                    ptype = requested_type if requested_type not in ('', 'ready_made') else (current_type or 'goods')
                    if ptype in ('ready_made', 'raw_materials'):
                        ptype = 'goods'
                    assign_product_codes(
                        c, product_id, ptype or 'goods', unit1,
                        external_barcode=ext_bc,
                        external_barcode1=ext_b1,
                    )
            else:
                product_type = requested_type
                hkd_sector = resolve_hkd_sector(product_type)

                c.execute("""
                    INSERT INTO products (
                        name, unit, buyprice, base_price, unit1, unit_ratio, price, import_id,
                        product_type, hkd_sector_code
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (name, unit, buyprice, base_price, unit1, unit_ratio, price, import_id, product_type, hkd_sector))
                product_id = c.lastrowid

                if product_type == 'ready_made':
                    from Services.fb_product_codes import assign_ready_made_product_codes
                    assign_ready_made_product_codes(c, product_id, bool(unit1))
                elif product_type == 'raw_materials':
                    from Services.fb_product_codes import assign_raw_material_product_codes
                    assign_raw_material_product_codes(c, product_id, bool(unit1))
                elif product_type in ('materials', 'fixed_asset', 'tools', 'service', 'goods'):
                    from Services.import_line_helpers import assign_product_codes
                    assign_product_codes(
                        c, product_id, product_type, unit1,
                        external_barcode=(data.get('barcode') or '').strip() or None,
                        external_barcode1=(data.get('barcode1') or '').strip() or None,
                    )
                else:
                    from Services.import_line_helpers import assign_product_codes
                    assign_product_codes(
                        c, product_id, 'goods', unit1,
                        external_barcode=(data.get('barcode') or '').strip() or None,
                        external_barcode1=(data.get('barcode1') or '').strip() or None,
                    )

                if product_type not in ('service', 'fixed_asset', 'tools'):
                    c.execute("INSERT OR IGNORE INTO inventory (product_id, quantity, avg_cost) VALUES (?, 0, 0)", (product_id,))

            conn.commit()

            # Lấy lại dữ liệu sau khi update/insert để trả về client
            c.execute("SELECT * FROM products WHERE id = ?", (product_id,))
            p = c.fetchone()

            return jsonify({
                "success": True,
                "product": dict(p)
            })
        except ValueError as e:
            conn.rollback()
            return jsonify({"success": False, "error": str(e)}), 400
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return jsonify({"success": False, "error": f"Mã vạch trùng: {e}"}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

    # === NEW: COMPLETE SALE (ghi sale_items, trừ kho, tính tổng, ghi stock_moves) ===
    @app.route('/api/products')
    def api_search_products():
        query = request.args.get('q', '').strip().lower()
        if not query:
            return jsonify([])

        conn = get_db_connection()
        c = conn.cursor()
        like = f"%{query}%"
        c.execute("""
            SELECT id, barcode, name, unit, quantity, base_price, 
                   unit1, unit_ratio, price
            FROM products 
            WHERE quantity > 0 
              AND (LOWER(name) LIKE ? OR barcode LIKE ?)
            ORDER BY name 
            LIMIT 15
        """, (like, like))
        rows = c.fetchall()
        conn.close()

        products = []
        for row in rows:
            p = dict(row)
            # Đảm bảo price luôn có
            if not p['price'] and p['unit1'] and p['unit_ratio']:
                p['price'] = p['base_price'] * p['unit_ratio']
            products.append(p)
        return jsonify(products)

    @app.get("/api/products/barcode/{barcode}")
    def get_by_barcode(barcode: str):
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT id, barcode, name, unit, quantity, base_price, 
                   unit1, unit_ratio, price
            FROM products 
            WHERE barcode = ? AND quantity > 0
        """, (barcode.upper(),))
        row = c.fetchone()
        conn.close()

        if not row:
            return jsonify({})

        p = dict(row)
        if not p['price'] and p['unit1'] and p['unit_ratio']:
            p['price'] = p['base_price'] * p['unit_ratio']
        return jsonify(p)

    @app.route('/api/hkd/sector-options')
    @login_required
    def api_hkd_sector_options():
        from flask import g
        from Services.hkd_sector import get_sector_ui_options, normalize_enabled_nn_sectors
        from Services.tenant_profile import infer_enabled_nn_sectors

        profile = getattr(g, 'tenant_profile', None) or {}
        purpose = (request.args.get('purpose') or '').strip().lower()
        options = get_sector_ui_options()

        if purpose == 'service':
            # Dịch vụ mặc định NN2 — luôn có trong dropdown dù tenant chỉ bật NN1
            enabled_set = set(infer_enabled_nn_sectors(profile, profile.get('business_line')))
            enabled_set.add('NN2')
            options = [opt for opt in options if opt['code'] in enabled_set]
        elif profile.get('enabled_nn_sectors'):
            enabled_set = set(normalize_enabled_nn_sectors(profile.get('enabled_nn_sectors')))
            options = [opt for opt in options if opt['code'] in enabled_set]

        return jsonify([
            {
                **opt,
                'label': opt['label'],
                'storage_code': opt['legacy_code'],
            }
            for opt in options
        ])

    @app.route('/products')
    @login_required
    def products():
        return render_template('products.html')
