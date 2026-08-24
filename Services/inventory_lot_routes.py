"""API và trang theo dõi lô hàng — FEFO vận hành, kế hoạch tiêu thụ."""
from __future__ import annotations

from datetime import datetime

from flask import jsonify, render_template, request
from flask_login import login_required

from db_utils import get_db_connection, sqlite_commit
from Services.fifo_lots import (
    consumption_plan,
    expiry_alerts,
    fifo_violations,
    list_lots,
    lot_physical_reconcile,
    update_lot_expiry,
)
from Services.inventory_cost_method import cost_method_status
from Services.inventory_lot_schema import ensure_inventory_lot_schema as _ensure_schema


def register_inventory_lot_routes(app, *, sme_page_guard=None):
    guard = sme_page_guard or (lambda f: f)

    @app.route('/SME_inventory_lots')
    @login_required
    @guard
    def SME_inventory_lots():
        return render_template('KeToanSME/inventory_lots.html')

    @app.route('/api/inventory/lots', methods=['GET'])
    @login_required
    def api_inventory_lots_list():
        product_id = request.args.get('product_id', type=int)
        q = (request.args.get('q') or request.args.get('search') or '').strip() or None
        only_open = request.args.get('only_open', '1') in ('1', 'true', 'True')
        fiscal_year = request.args.get('fiscal_year', type=int) or datetime.now().year
        # Khi tìm kiếm: bỏ lọc năm — tránh “không thấy” lô nhập năm khác
        list_fiscal_year = None if q else fiscal_year
        limit = min(request.args.get('limit', 500, type=int) or 500, 2000)
        offset = request.args.get('offset', 0, type=int) or 0
        conn = get_db_connection()
        try:
            _ensure_schema(conn)
            rows = list_lots(
                conn,
                product_id=product_id,
                q=q,
                only_open=only_open,
                fiscal_year=list_fiscal_year,
                limit=limit,
                offset=offset,
            )
            violations = fifo_violations(conn, fiscal_year=fiscal_year, limit=50)
            reconcile = lot_physical_reconcile(conn)
            alerts = expiry_alerts(conn, days=30, limit=50)
            plan = consumption_plan(conn, product_id=product_id, q=q, only_open=True, limit=100)
            status = cost_method_status(conn)
            return jsonify({
                'success': True,
                'lots': rows,
                'violations': violations,
                'reconcile': reconcile,
                'expiry_alerts': alerts,
                'consumption_plan': plan,
                'cost_method': status,
                'fiscal_year': fiscal_year,
                'search_all_years': bool(q),
                'q': q or '',
                'lot_count': len(rows),
                'issue_order': 'fefo' if status.get('lot_tracking_enabled') else 'fifo',
            })
        finally:
            conn.close()

    @app.route('/api/inventory/lots/<int:lot_id>/expiry', methods=['PATCH', 'POST'])
    @login_required
    def api_inventory_lot_expiry(lot_id: int):
        data = request.get_json(silent=True) or {}
        expiry_date = data.get('expiry_date')
        if expiry_date is not None and str(expiry_date).strip() == '':
            expiry_date = None
        conn = get_db_connection()
        try:
            _ensure_schema(conn)
            row = conn.execute(
                'SELECT id FROM inventory_lots WHERE id = ?',
                (int(lot_id),),
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Không tìm thấy lô'}), 404
            cur = conn.cursor()
            update_lot_expiry(cur, int(lot_id), expiry_date)
            sqlite_commit(conn, label='lot_expiry')
            lot = conn.execute(
                """
                SELECT l.*, p.name AS product_name
                FROM inventory_lots l
                LEFT JOIN products p ON p.id = l.product_id
                WHERE l.id = ?
                """,
                (int(lot_id),),
            ).fetchone()
            return jsonify({'success': True, 'lot': dict(lot) if lot else None})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/inventory/lots/consumptions', methods=['GET'])
    @login_required
    def api_inventory_lot_consumptions():
        ref_type = (request.args.get('ref_type') or '').strip()
        ref_id = request.args.get('ref_id', type=int)
        product_id = request.args.get('product_id', type=int)
        if not ref_type or not ref_id:
            return jsonify({'success': False, 'error': 'Thiếu ref_type hoặc ref_id'}), 400
        conn = get_db_connection()
        try:
            _ensure_schema(conn)
            clauses = ['c.ref_type = ?', 'c.ref_id = ?']
            params = [ref_type, int(ref_id)]
            if product_id:
                clauses.append('c.product_id = ?')
                params.append(int(product_id))
            rows = conn.execute(
                f"""
                SELECT c.*, l.lot_no, l.received_at, l.expiry_date, l.source_type, p.name AS product_name
                FROM inventory_lot_consumptions c
                JOIN inventory_lots l ON l.id = c.lot_id
                LEFT JOIN products p ON p.id = c.product_id
                WHERE {' AND '.join(clauses)}
                ORDER BY c.id
                """,
                params,
            ).fetchall()
            return jsonify({'success': True, 'items': [dict(r) for r in rows]})
        finally:
            conn.close()
