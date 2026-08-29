"""Routes báo cáo doanh thu & lợi nhuận — tách từ app.py."""
import sqlite3
from datetime import datetime

from flask import jsonify, render_template, request

from auth import login_required
from db_utils import get_db_connection


def register_reports_routes(app):
    @app.route('/profit')
    @login_required
    def profit():
        return render_template('profit.html')

    @app.route('/reports')
    @login_required
    def reports():
        return render_template('reports.html')

    @app.route('/api/reports/profit', methods=['GET'])
    @login_required
    def api_profit_report():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        from_date_iso = request.args.get('from') # yyyy-mm-dd
        to_date_iso = request.args.get('to')     # yyyy-mm-dd

        if not from_date_iso or not to_date_iso:
            return jsonify({"error": "Thiếu thông tin ngày"}), 400

        try:
            start_dt = datetime.strptime(from_date_iso, '%Y-%m-%d')
            end_dt = datetime.strptime(to_date_iso, '%Y-%m-%d')
        except ValueError:
            return jsonify({"error": "Định dạng ngày không hợp lệ"}), 400

        try:
            from Services.profit_report_helpers import compute_profit_report
            from flask import g
            profile = getattr(g, 'tenant_profile', None) or {}
            result = compute_profit_report(c, from_date_iso, to_date_iso, tenant_profile=profile)
            return jsonify({"status": "success", **result})
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"Profit Report Error: {str(e)}")
            return jsonify({"error": "Lỗi hạch toán hệ thống", "detail": str(e)[:300]}), 500
        finally:
            conn.close()

    # ⚡️ ENDPOINT BÁO CÁO DOANH THU ĐÃ CẬP NHẬT ⚡️
    # ----------------------------------------------------------------------
    @app.route('/api/reports/sale', methods=['GET'])
    def get_sale_report():
        start_iso = request.args.get('start') # "2026-01-01"
        end_iso = request.args.get('end')     # "2026-01-30"

        # Mở rộng phạm vi giờ để bao quát trọn vẹn ngày được chọn
        # Ngày bắt đầu từ 0 giờ 0 phút 0 giây
        start_query = f"{start_iso} 00:00:00"
        # Ngày kết thúc đến 23 giờ 59 phút 59 giây
        end_query = f"{end_iso} 23:59:59"

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # TRUY VẤN 1: DOANH THU HÀNG NGÀY
        # Sử dụng DATE(date) để nhóm tất cả các giờ trong ngày 30/01 vào 1 dòng duy nhất
        cursor.execute("""
            SELECT 
                DATE(date) as day, 
                SUM(total_amount) as revenue, 
                COUNT(id) as bills 
            FROM sale 
            WHERE date BETWEEN ? AND ? AND status = 'completed'
            GROUP BY DATE(date)
            ORDER BY day ASC
        """, (start_query, end_query))

        sale_data = [dict(row) for row in cursor.fetchall()]

        # TRUY VẤN 2: TOP SẢN PHẨM (Fix lỗi SUM nhầm total_amount của hóa đơn)
        cursor.execute("""
            SELECT 
                p.name, 
                SUM(si.quantity) as qty,
                SUM(si.quantity * si.price) as total -- Tính tiền từng dòng item
            FROM sale_items si
            JOIN sale s ON s.id = si.sale_id
            JOIN products p ON p.id = si.product_id
            WHERE s.date BETWEEN ? AND ? AND s.status = 'completed'
            GROUP BY p.id
            ORDER BY total DESC
            LIMIT 10
        """, (start_query, end_query))

        top_products = [dict(row) for row in cursor.fetchall()]

        conn.close()
        return jsonify({"sale": sale_data, "top_products": top_products})

    @app.route('/reports/sale')
    def sale_report_page():
        # Giả định tên file HTML là 'sale_report.html'
        return render_template('reports.html')
