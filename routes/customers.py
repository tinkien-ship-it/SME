"""Routes quản lý danh mục khách hàng."""
import sqlite3

from flask import jsonify, render_template, request

from auth import login_required
from db_utils import get_db_connection


def register_customers_routes(app):

    @app.route('/customers')
    @login_required
    def customers_page():
        return render_template('customers.html')

    @app.route('/api/customers', methods=['GET', 'POST', 'PUT', 'DELETE'])
    @login_required
    def api_customers():
        conn = get_db_connection()
        c = conn.cursor()
        try:
            if request.method == 'GET':
                q = request.args.get('q', '').strip()
                if q:
                    like = f'%{q}%'
                    c.execute(
                        """
                        SELECT * FROM customers
                        WHERE CAST(id AS TEXT) LIKE ?
                           OR COALESCE(name, '') LIKE ?
                           OR COALESCE(company_name, '') LIKE ?
                           OR COALESCE(phone, '') LIKE ?
                           OR COALESCE(tax_code, '') LIKE ?
                           OR COALESCE(email, '') LIKE ?
                        ORDER BY id ASC
                        """,
                        (like, like, like, like, like, like),
                    )
                else:
                    c.execute("SELECT * FROM customers ORDER BY id ASC")
                return jsonify([dict(row) for row in c.fetchall()])

            data = request.get_json() or {}

            if request.method == 'POST':
                name = (data.get('name') or '').strip()
                if not name:
                    return jsonify({'error': 'Tên khách hàng không được để trống'}), 400
                c.execute(
                    """
                    INSERT INTO customers
                        (name, company_name, phone, email, address,
                         tax_code, budget_unit_code, passport_no)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        (data.get('company_name') or '').strip(),
                        (data.get('phone') or '').strip(),
                        (data.get('email') or '').strip(),
                        (data.get('address') or '').strip(),
                        (data.get('tax_code') or '').strip(),
                        (data.get('budget_unit_code') or '').strip(),
                        (data.get('passport_no') or '').strip(),
                    ),
                )
                conn.commit()
                new_id = c.lastrowid
                return jsonify({'success': True, 'id': new_id})

            id_ = data.get('id')
            if not id_:
                return jsonify({'error': 'Thiếu ID'}), 400

            if request.method == 'PUT':
                name = (data.get('name') or '').strip()
                if not name:
                    return jsonify({'error': 'Tên khách hàng không được để trống'}), 400
                c.execute(
                    """
                    UPDATE customers
                    SET name = ?, company_name = ?, phone = ?, email = ?,
                        address = ?, tax_code = ?, budget_unit_code = ?, passport_no = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        (data.get('company_name') or '').strip(),
                        (data.get('phone') or '').strip(),
                        (data.get('email') or '').strip(),
                        (data.get('address') or '').strip(),
                        (data.get('tax_code') or '').strip(),
                        (data.get('budget_unit_code') or '').strip(),
                        (data.get('passport_no') or '').strip(),
                        id_,
                    ),
                )
                conn.commit()
                return jsonify({'success': True})

            c.execute('SELECT COUNT(*) FROM sale WHERE customer_id = ?', (id_,))
            if c.fetchone()[0] > 0:
                return jsonify({
                    'error': 'Không thể xóa: khách hàng đã có đơn hàng liên kết',
                }), 400
            c.execute('DELETE FROM customers WHERE id = ?', (id_,))
            conn.commit()
            return jsonify({'success': True})

        except sqlite3.IntegrityError:
            return jsonify({'error': 'Dữ liệu bị trùng hoặc không hợp lệ'}), 409
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()
