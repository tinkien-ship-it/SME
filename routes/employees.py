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

    @app.route('/api/employees/departments', methods=['GET'])
    @login_required
    def api_employees_departments():
        from Services.employee_payroll_helpers import list_department_options
        return jsonify({'success': True, 'data': list_department_options()})

    @app.route('/api/employees/manage', methods=['GET'])
    @login_required
    def api_employees_manage():
        from Services.employee_payroll_helpers import (
            department_label,
            ensure_employee_allowance_columns,
            expense_account_for_department,
            normalize_department,
        )

        q = (request.args.get('q') or '').strip()
        status = (request.args.get('status') or '').strip()
        department = (request.args.get('department') or '').strip()

        conn = get_db_connection()
        try:
            ensure_employee_allowance_columns(conn, commit=True)
            sql = """
                SELECT
                    id, fullname, position, id_card, base_salary, salary_rate,
                    status, phone, join_date, created_at, address,
                    dependents, self_deduction, dependent_deduction, attendance_code,
                    COALESCE(department, 'ADMIN') AS department
                FROM employees
                WHERE 1=1
            """
            params = []
            if status in ('0', '1'):
                sql += ' AND CAST(status AS TEXT) = ?'
                params.append(status)
            if department:
                sql += " AND COALESCE(NULLIF(TRIM(department), ''), 'ADMIN') = ?"
                params.append(normalize_department(department))
            if q:
                like = f'%{q}%'
                sql += """
                    AND (
                        COALESCE(fullname, '') LIKE ?
                        OR COALESCE(id_card, '') LIKE ?
                        OR COALESCE(phone, '') LIKE ?
                        OR COALESCE(position, '') LIKE ?
                        OR COALESCE(address, '') LIKE ?
                        OR COALESCE(department, '') LIKE ?
                    )
                """
                params.extend([like] * 6)
            sql += ' ORDER BY id ASC'
            rows = conn.execute(sql, params).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                dept = normalize_department(item.get('department'))
                item['department'] = dept
                item['department_label'] = department_label(dept)
                item['expense_account'] = expense_account_for_department(dept)
                out.append(item)
            return jsonify(out)
        except sqlite3.Error as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()
