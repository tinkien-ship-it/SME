"""Routes quản lý danh mục khách hàng."""
import sqlite3

from flask import jsonify, render_template, request

from auth import login_required
from db_utils import get_db_connection, sqlite_commit


def _table_exists(conn, name: str) -> bool:
    from db_utils import is_postgres

    try:
        if is_postgres():
            row = conn.execute(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_name = %s
                LIMIT 1
                """,
                (name,),
            ).fetchone()
            return bool(row)
    except Exception:
        pass
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def customer_has_purchase(conn, customer_id: int) -> bool:
    """True nếu KH đã từng có đơn bán hàng (không hủy)."""
    from Services.crm import customer_has_purchase as _has
    return _has(conn, customer_id)


def _customer_transaction_reason(conn, customer_id: int) -> str | None:
    """Chỉ chặn xóa khi đã phát sinh mua hàng (đơn bán)."""
    if customer_has_purchase(conn, customer_id):
        return 'đã phát sinh mua hàng'
    return None


def effective_crm_lifecycle(conn, customer_id: int, stored: str | None) -> str:
    from Services.crm import effective_crm_lifecycle as _eff
    return _eff(conn, customer_id, stored)


def _cleanup_customer_crm_links(conn, customer_id: int) -> None:
    """Xóa / gỡ liên kết CRM khi xóa KH chưa mua hàng."""
    cid = int(customer_id)
    soft_deletes = (
        'crm_activities',
        'crm_visit_checkins',
        'crm_tickets',
        'crm_ticket_events',
        'crm_surveys',
        'crm_quotes',
        'crm_opportunities',
        'crm_contracts',
        'crm_notifications',
    )
    for table in soft_deletes:
        if not _table_exists(conn, table):
            continue
        try:
            cols = None
            try:
                from db_utils import table_cols
                cols = set(table_cols(conn, table) or [])
            except Exception:
                cols = None
            if cols is not None and 'customer_id' not in cols:
                continue
            conn.execute(f'DELETE FROM {table} WHERE customer_id = ?', (cid,))
        except sqlite3.Error:
            continue

    if _table_exists(conn, 'crm_quote_items') and _table_exists(conn, 'crm_quotes'):
        try:
            conn.execute(
                """
                DELETE FROM crm_quote_items
                WHERE quote_id NOT IN (SELECT id FROM crm_quotes)
                """
            )
        except sqlite3.Error:
            pass

    if _table_exists(conn, 'crm_contract_items') and _table_exists(conn, 'crm_contracts'):
        try:
            conn.execute(
                """
                DELETE FROM crm_contract_items
                WHERE contract_id NOT IN (SELECT id FROM crm_contracts)
                """
            )
        except sqlite3.Error:
            pass

    if _table_exists(conn, 'crm_leads'):
        try:
            conn.execute(
                """
                UPDATE crm_leads
                SET customer_id = NULL,
                    status = CASE WHEN status = 'converted' THEN 'qualified' ELSE status END,
                    updated_at = datetime('now','localtime')
                WHERE customer_id = ?
                """,
                (cid,),
            )
        except sqlite3.Error:
            pass


def _enrich_customer_row(conn, row) -> dict:
    d = dict(row)
    cid = d.get('id')
    if not cid:
        d['can_delete'] = False
        d['delete_block_reason'] = 'thiếu mã'
        d['has_purchase'] = False
        d['crm_lifecycle'] = 'prospect'
        return d
    has_buy = customer_has_purchase(conn, cid)
    stored = d.get('crm_lifecycle')
    d['has_purchase'] = has_buy
    d['crm_lifecycle'] = effective_crm_lifecycle(conn, cid, stored)
    reason = _customer_transaction_reason(conn, cid)
    d['can_delete'] = reason is None
    d['delete_block_reason'] = reason
    return d


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
                           OR COALESCE(crm_owner, '') LIKE ?
                        ORDER BY id ASC
                        """,
                        (like, like, like, like, like, like, like),
                    )
                else:
                    c.execute("SELECT * FROM customers ORDER BY id ASC")
                return jsonify([_enrich_customer_row(conn, row) for row in c.fetchall()])

            data = request.get_json() or {}

            if request.method == 'POST':
                name = (data.get('name') or '').strip()
                if not name:
                    return jsonify({'error': 'Tên khách hàng không được để trống'}), 400
                try:
                    from Services.crm_schema import ensure_crm_schema
                    ensure_crm_schema(conn, commit=False)
                except Exception:
                    pass
                c.execute(
                    """
                    INSERT INTO customers
                        (name, company_name, phone, email, address,
                         tax_code, budget_unit_code, passport_no,
                         crm_source, crm_owner, crm_segment, crm_lifecycle,
                         crm_notes, crm_tags, crm_created_at, crm_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            datetime('now','localtime'), datetime('now','localtime'))
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
                        (data.get('crm_source') or '').strip() or None,
                        (data.get('crm_owner') or '').strip() or None,
                        (data.get('crm_segment') or 'standard').strip(),
                        (data.get('crm_lifecycle') or 'prospect').strip(),

                        (data.get('crm_notes') or '').strip() or None,
                        (data.get('crm_tags') or '').strip() or None,
                    ),
                )
                sqlite_commit(conn, label='customers')
                new_id = c.lastrowid
                return jsonify({'success': True, 'id': new_id})

            id_ = data.get('id')
            if not id_:
                return jsonify({'error': 'Thiếu ID'}), 400

            if request.method == 'PUT':
                name = (data.get('name') or '').strip()
                if not name:
                    return jsonify({'error': 'Tên khách hàng không được để trống'}), 400
                try:
                    from Services.crm_schema import ensure_crm_schema
                    ensure_crm_schema(conn, commit=False)
                except Exception:
                    pass
                c.execute(
                    """
                    UPDATE customers
                    SET name = ?, company_name = ?, phone = ?, email = ?,
                        address = ?, tax_code = ?, budget_unit_code = ?, passport_no = ?,
                        crm_source = COALESCE(?, crm_source),
                        crm_owner = COALESCE(?, crm_owner),
                        crm_segment = COALESCE(?, crm_segment),
                        crm_lifecycle = COALESCE(?, crm_lifecycle),
                        crm_updated_at = datetime('now','localtime')
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
                        (data.get('crm_source') or '').strip() or None,
                        (data.get('crm_owner') or '').strip() or None,
                        (data.get('crm_segment') or '').strip() or None,
                        (data.get('crm_lifecycle') or '').strip() or None,
                        id_,
                    ),
                )
                sqlite_commit(conn, label='customers')
                return jsonify({'success': True})

            reason = _customer_transaction_reason(conn, int(id_))
            if reason:
                return jsonify({
                    'error': f'Không thể xóa: khách hàng {reason}',
                }), 400
            _cleanup_customer_crm_links(conn, int(id_))
            c.execute('DELETE FROM customers WHERE id = ?', (id_,))
            sqlite_commit(conn, label='customers')
            return jsonify({'success': True})

        except sqlite3.IntegrityError:
            return jsonify({'error': 'Dữ liệu bị trùng hoặc không hợp lệ'}), 409
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()
