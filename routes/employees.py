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
        from Services.hrm.ess_access import session_may_manage_ess_link
        from Services.hrm.schema import ensure_hrm_schema

        q = (request.args.get('q') or '').strip()
        status = (request.args.get('status') or '').strip()
        department = (request.args.get('department') or '').strip()

        conn = get_db_connection()
        try:
            ensure_hrm_schema(conn, commit=False)
            ensure_employee_allowance_columns(conn, commit=True)
            sql = """
                SELECT
                    e.id, e.fullname, e.position, e.id_card, e.base_salary, e.salary_rate,
                    e.status, e.phone, e.join_date, e.birth_date, e.created_at, e.address,
                    e.dependents, e.self_deduction, e.dependent_deduction, e.attendance_code,
                    COALESCE(e.department, 'ADMIN') AS department,
                    COALESCE(e.employee_code, '') AS employee_code,
                    COALESCE(e.allowance_position, 0) AS allowance_position,
                    COALESCE(e.allowance_responsibility, 0) AS allowance_responsibility,
                    COALESCE(e.allowance_seniority, 0) AS allowance_seniority,
                    COALESCE(e.allowance_lunch, 0) AS allowance_lunch,
                    COALESCE(e.allowance_uniform, 0) AS allowance_uniform,
                    COALESCE(e.allowance_phone, 0) AS allowance_phone,
                    e.user_id AS ess_user_id,
                    COALESCE(e.ess_enabled, 0) AS ess_enabled,
                    u.username AS ess_username,
                    u.full_name AS ess_user_fullname
                FROM employees e
                LEFT JOIN users u ON u.id = e.user_id
                WHERE 1=1
            """
            params = []
            if status in ('0', '1'):
                sql += ' AND CAST(e.status AS TEXT) = ?'
                params.append(status)
            if department:
                sql += " AND COALESCE(NULLIF(TRIM(e.department), ''), 'ADMIN') = ?"
                params.append(normalize_department(department))
            if q:
                like = f'%{q}%'
                sql += """
                    AND (
                        COALESCE(e.fullname, '') LIKE ?
                        OR COALESCE(e.id_card, '') LIKE ?
                        OR COALESCE(e.phone, '') LIKE ?
                        OR COALESCE(e.position, '') LIKE ?
                        OR COALESCE(e.address, '') LIKE ?
                        OR COALESCE(e.department, '') LIKE ?
                        OR COALESCE(e.employee_code, '') LIKE ?
                    )
                """
                params.extend([like] * 7)
            sql += ' ORDER BY e.id ASC'
            rows = conn.execute(sql, params).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                dept = normalize_department(item.get('department'))
                item['department'] = dept
                item['department_label'] = department_label(dept)
                item['expense_account'] = expense_account_for_department(dept)
                out.append(item)
            return jsonify({
                'success': True,
                'items': out,
                'can_manage_ess_link': session_may_manage_ess_link(),
            })
        except sqlite3.Error as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()
