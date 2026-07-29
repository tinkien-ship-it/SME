"""Routes Tính Giá Thành (Thành Phẩm) — Kế Toán HKD."""
import logging

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from db_utils import get_db_connection
from Services.production_costing import (
    cancel_production_order,
    create_production_order,
    delete_bom,
    ensure_production_schema,
    get_bom,
    get_production_order,
    list_boms,
    list_finished_products,
    list_material_products,
    list_production_orders,
    preview_materials,
    save_bom,
)

logger = logging.getLogger(__name__)


def register_production_routes(app):
    @app.route('/ketoan_hkd/production')
    @app.route('/production')
    @login_required
    def production_page():
        return render_template('KeToanHKD/production.html')

    # ---- Products ----
    @app.route('/api/production/finished-products')
    @login_required
    def api_production_finished_products():
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            return jsonify({
                'success': True,
                'data': list_finished_products(conn, request.args.get('q', '')),
            })
        except Exception as exc:
            logger.exception('finished-products: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/materials')
    @login_required
    def api_production_materials():
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            return jsonify({
                'success': True,
                'data': list_material_products(
                    conn,
                    request.args.get('q', ''),
                    code_prefix=request.args.get('code_prefix', ''),
                ),
            })
        except Exception as exc:
            logger.exception('materials: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    # ---- BOM ----
    @app.route('/api/production/bom')
    @login_required
    def api_production_bom_list():
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            return jsonify({'success': True, 'data': list_boms(conn)})
        except Exception as exc:
            logger.exception('bom list: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/bom/<int:finished_product_id>')
    @login_required
    def api_production_bom_get(finished_product_id):
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            bom = get_bom(conn, finished_product_id)
            if not bom:
                return jsonify({'success': False, 'error': 'Chưa có định mức'}), 404
            return jsonify({'success': True, 'data': bom})
        except Exception as exc:
            logger.exception('bom get: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/bom', methods=['POST'])
    @login_required
    def api_production_bom_save():
        data = request.get_json(silent=True) or {}
        try:
            fg_id = int(data.get('finished_product_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Thành phẩm không hợp lệ'}), 400
        if fg_id <= 0:
            return jsonify({'success': False, 'error': 'Chọn thành phẩm'}), 400

        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            bom = save_bom(
                conn,
                fg_id,
                data.get('items') or [],
                note=data.get('note') or '',
            )
            return jsonify({'success': True, 'data': bom, 'message': 'Đã lưu định mức'})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('bom save: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/bom/<int:finished_product_id>', methods=['DELETE'])
    @login_required
    def api_production_bom_delete(finished_product_id):
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            delete_bom(conn, finished_product_id)
            return jsonify({'success': True, 'message': 'Đã xóa định mức'})
        except Exception as exc:
            logger.exception('bom delete: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    # ---- Preview / Orders ----
    @app.route('/api/production/preview', methods=['POST'])
    @login_required
    def api_production_preview():
        data = request.get_json(silent=True) or {}
        try:
            fg_id = int(data.get('finished_product_id') or 0)
            qty = float(data.get('qty_completed') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Dữ liệu không hợp lệ'}), 400

        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            lines = preview_materials(
                conn, fg_id, qty, data.get('materials') or data.get('material_overrides'),
            )
            material_total = round(sum(l['line_cost'] for l in lines), 2)
            try:
                labor = max(0.0, float(data.get('labor_cost') or 0))
            except (TypeError, ValueError):
                labor = 0.0
            try:
                other = max(0.0, float(data.get('other_cost') or 0))
            except (TypeError, ValueError):
                other = 0.0
            total = round(material_total + labor + other, 2)
            unit = round(total / qty, 4) if qty else 0
            return jsonify({
                'success': True,
                'data': {
                    'materials': lines,
                    'total_material_cost': material_total,
                    'labor_cost': labor,
                    'other_cost': other,
                    'total_cost': total,
                    'unit_cost': unit,
                    'qty_completed': qty,
                },
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('preview: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/orders')
    @login_required
    def api_production_orders_list():
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            rows = list_production_orders(
                conn,
                date_from=request.args.get('from', ''),
                date_to=request.args.get('to', ''),
                status=request.args.get('status', ''),
                q=request.args.get('q', ''),
            )
            return jsonify({'success': True, 'data': rows, 'count': len(rows)})
        except Exception as exc:
            logger.exception('orders list: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/orders/<int:order_id>')
    @login_required
    def api_production_order_get(order_id):
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            order = get_production_order(conn, order_id)
            if not order:
                return jsonify({'success': False, 'error': 'Không tìm thấy phiếu'}), 404
            return jsonify({'success': True, 'data': order})
        except Exception as exc:
            logger.exception('order get: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/orders', methods=['POST'])
    @login_required
    def api_production_order_create():
        data = request.get_json(silent=True) or {}
        try:
            fg_id = int(data.get('finished_product_id') or 0)
            qty = float(data.get('qty_completed') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Dữ liệu không hợp lệ'}), 400

        username = ''
        try:
            username = getattr(current_user, 'username', '') or ''
        except Exception:
            pass

        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            order = create_production_order(
                conn,
                finished_product_id=fg_id,
                qty_completed=qty,
                production_date=data.get('production_date'),
                note=data.get('note') or '',
                material_overrides=data.get('materials') or data.get('material_overrides'),
                labor_cost=float(data.get('labor_cost') or 0),
                other_cost=float(data.get('other_cost') or 0),
                created_by=username,
                allow_negative_stock=bool(data.get('allow_negative_stock')),
            )
            return jsonify({
                'success': True,
                'data': order,
                'message': (
                    f"Đã sản xuất {order['voucher_no']}: "
                    f"giá thành {order['unit_cost']:,.0f} đ/{order.get('finished_unit') or 'ĐV'}"
                ),
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('order create: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/production/orders/<int:order_id>/cancel', methods=['POST'])
    @login_required
    def api_production_order_cancel(order_id):
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            order = cancel_production_order(
                conn,
                order_id,
                cancel_note=data.get('cancel_note') or data.get('note') or '',
                allow_negative_stock=bool(data.get('allow_negative_stock')),
            )
            return jsonify({
                'success': True,
                'data': order,
                'message': f"Đã hủy phiếu {order['voucher_no']}",
            })
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('order cancel: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/ketoan_hkd/production/<int:order_id>/print')
    @app.route('/production/<int:order_id>/print')
    @login_required
    def production_print(order_id):
        conn = get_db_connection()
        try:
            ensure_production_schema(conn)
            order = get_production_order(conn, order_id)
            if not order:
                return 'Không tìm thấy phiếu sản xuất', 404
            biz = {'business_name': '', 'address': '', 'phone': ''}
            try:
                info = conn.execute(
                    "SELECT business_name, address, phone FROM business_info LIMIT 1"
                ).fetchone()
                if info:
                    biz = dict(info)
            except Exception:
                pass
            return render_template(
                'KeToanHKD/production_print.html',
                order=order,
                info=biz,
            )
        finally:
            conn.close()
