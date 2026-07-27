"""API cân điện tử — cấu hình, giải mã mã vạch cân."""
from flask import jsonify, request

from auth import login_required
from Services.scale_service import (
    get_scale_config,
    parse_scale_serial_line,
    parse_weight_barcode,
    resolve_weight_scan,
    save_scale_settings,
)


def register_scale_routes(app):
    @app.route('/api/scale/config', methods=['GET'])
    @login_required
    def api_scale_config_get():
        return jsonify({'success': True, **get_scale_config()})

    @app.route('/api/scale/config', methods=['POST'])
    @login_required
    def api_scale_config_save():
        data = request.get_json(silent=True) or {}
        saved = save_scale_settings(data)
        return jsonify({'success': True, 'message': 'Đã lưu cấu hình cân', **saved})

    @app.route('/api/scale/parse-barcode', methods=['POST'])
    @login_required
    def api_scale_parse_barcode():
        data = request.get_json(silent=True) or {}
        barcode = (data.get('barcode') or '').strip()
        if not barcode:
            return jsonify({'success': False, 'error': 'Thiếu mã vạch'}), 400
        result = resolve_weight_scan(barcode)
        code = 200 if result.get('success') else 404
        return jsonify(result), code

    @app.route('/api/scale/parse-line', methods=['POST'])
    @login_required
    def api_scale_parse_line():
        data = request.get_json(silent=True) or {}
        line = data.get('line') or ''
        protocol = data.get('protocol') or get_scale_config().get('protocol')
        weight = parse_scale_serial_line(line, protocol)
        if weight is None:
            return jsonify({'success': False, 'error': 'Không đọc được khối lượng'}), 400
        return jsonify({'success': True, 'weight_kg': weight})

    @app.route('/api/scale/calc', methods=['POST'])
    @login_required
    def api_scale_calc():
        """Tính giá bán theo sản phẩm + khối lượng cân."""
        from Services.scale_service import build_weight_cart_item, lookup_weight_product
        from db_utils import get_db_connection

        data = request.get_json(silent=True) or {}
        product_id = data.get('product_id')
        weight_kg = data.get('weight_kg')
        if not product_id or weight_kg is None:
            return jsonify({'success': False, 'error': 'Thiếu product_id hoặc weight_kg'}), 400

        conn = get_db_connection()
        try:
            row = conn.execute("""
                SELECT p.id, p.name, p.unit, p.base_price, p.price, p.unit_ratio, p.unit1,
                       p.sell_by_weight, p.weight_plu, COALESCE(i.quantity, 0) AS stock_qty
                FROM products p
                LEFT JOIN inventory i ON p.id = i.product_id
                WHERE p.id = ?
            """, (product_id,)).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Không tìm thấy sản phẩm'}), 404
            product = dict(row)
            item = build_weight_cart_item(product, weight_kg)
            if not item:
                return jsonify({'success': False, 'error': 'Khối lượng không hợp lệ'}), 400
            return jsonify({'success': True, 'data': item})
        finally:
            conn.close()
