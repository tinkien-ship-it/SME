"""Routes quản lý danh sách nhân viên."""
import sqlite3

from flask import jsonify, render_template, request

from auth import login_required
from db_utils import get_db_connection


def register_employees_routes(app):

    @app.route('/employees')
    @login_required
    def employees_page():
        return render_template('employees.html')

    @app.route('/api/employees/manage', methods=['GET'])
    @login_required
    def api_employees_manage():
        q = (request.args.get('q') or '').strip()
        status = (request.args.get('status') or '').strip()

        conn = get_db_connection()
        try:
            sql = """
                SELECT
                    id, fullname, position, id_card, base_salary, salary_rate,
                    status, phone, join_date, created_at, address,
                    dependents, self_deduction, dependent_deduction, attendance_code
                FROM employees
                WHERE 1=1
            """
            params = []
            if status in ('0', '1'):
                sql += ' AND CAST(status AS TEXT) = ?'
                params.append(status)
            if q:
                like = f'%{q}%'
                sql += """
                    AND (
                        COALESCE(fullname, '') LIKE ?
                        OR COALESCE(id_card, '') LIKE ?
                        OR COALESCE(phone, '') LIKE ?
                        OR COALESCE(position, '') LIKE ?
                        OR COALESCE(address, '') LIKE ?
                    )
                """
                params.extend([like] * 5)
            sql += ' ORDER BY id ASC'
            rows = conn.execute(sql, params).fetchall()
            return jsonify([dict(row) for row in rows])
        except sqlite3.Error as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()
