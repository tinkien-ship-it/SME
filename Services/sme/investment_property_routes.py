"""Routes BĐSĐT SME. Đăng ký bằng register_investment_property_routes(app)."""
from __future__ import annotations
import sqlite3
from flask import jsonify, render_template, request
from flask_login import current_user
from auth import login_required
from db_utils import get_db_connection, sqlite_commit, rollback_quietly, begin_immediate
from Services.sme.investment_property import (
    ensure_investment_property_e2e_schema,
    ensure_property_pos_products,
    refresh_property_balances,
    post_depreciation,
    post_impairment,
    transfer_to_fixed_asset,
    transfer_to_inventory,
    get_active_lease_plan,
    save_lease_plan,
    stop_lease_plan,
    run_due_lease_plans,
)


def _user_name():
    return getattr(current_user, 'username', None) or getattr(current_user, 'email', None) or 'user'


def _rows(conn):
    ensure_investment_property_e2e_schema(conn, commit=True)
    rows = conn.execute(
        '''SELECT id,property_code,property_name,property_type,usage_purpose,address,
                  acquisition_date,original_cost,accumulated_depreciation,impairment_amount,
                  carrying_amount,status,sale_product_id,lease_product_id,lease_deferred_product_id
           FROM sme_investment_properties
           ORDER BY id DESC'''
    ).fetchall()
    cols = [
        'id','property_code','property_name','property_type','usage_purpose','address',
        'acquisition_date','original_cost','accumulated_depreciation','impairment_amount',
        'carrying_amount','status','sale_product_id','lease_product_id','lease_deferred_product_id'
    ]
    out = []
    for row in rows:
        item = dict(zip(cols, row))
        item['lease_plan'] = get_active_lease_plan(conn, int(item['id']))
        out.append(item)
    return out


def register_investment_property_routes(app):

    @app.route('/SME/investment-properties')
    @login_required
    def SME_investment_properties():
        conn = get_db_connection()
        try:
            properties = _rows(conn)
            return render_template(
                'KeToanSME/investment_properties.html',
                properties=properties,
            )
        finally:
            conn.close()

    @app.route('/api/sme/investment-properties', methods=['GET'])
    @login_required
    def api_bdsdt_list():
        conn = get_db_connection()
        try:
            return jsonify({'success': True, 'data': _rows(conn)})
        finally:
            conn.close()

    @app.route('/api/sme/investment-properties/<int:property_id>', methods=['GET', 'PATCH'])
    @login_required
    def api_bdsdt_detail(property_id):
        conn=get_db_connection()
        try:
            ensure_investment_property_e2e_schema(conn,commit=False)
            if request.method == 'PATCH':
                data=request.get_json(silent=True) or {}
                property_name=str(data.get('property_name') or '').strip()
                address=str(data.get('address') or '').strip()
                note=str(data.get('note') or '').strip()
                if not property_name:
                    return jsonify({'success':False,'error':'Tên BĐSĐT không được để trống'}),400
                if not address:
                    return jsonify({'success':False,'error':'Địa chỉ BĐSĐT không được để trống'}),400
                begin_immediate(conn,label='bdsdt_update_info')
                row=conn.execute('SELECT id FROM sme_investment_properties WHERE id=?',(property_id,)).fetchone()
                if not row:
                    rollback_quietly(conn)
                    return jsonify({'success':False,'error':'Không tìm thấy BĐSĐT'}),404
                conn.execute(
                    '''UPDATE sme_investment_properties
                       SET property_name=?, address=?, note=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?''',
                    (property_name,address,note,property_id),
                )
                sqlite_commit(conn,label='bdsdt_update_info')
                return jsonify({'success':True,'data':{
                    'id':property_id,'property_name':property_name,'address':address,'note':note
                }})
            refresh_property_balances(conn,property_id)
            row=conn.execute('SELECT * FROM sme_investment_properties WHERE id=?',(property_id,)).fetchone()
            if not row: return jsonify({'success':False,'error':'Không tìm thấy BĐSĐT'}),404
            data=dict(row) if hasattr(row,'keys') else {'id':property_id}
            events=conn.execute('SELECT * FROM sme_investment_property_events WHERE property_id=? ORDER BY event_date DESC,id DESC',(property_id,)).fetchall()
            data['events']=[dict(x) if hasattr(x,'keys') else list(x) for x in events]
            return jsonify({'success':True,'data':data})
        except Exception as exc:
            rollback_quietly(conn)
            return jsonify({'success':False,'error':str(exc)}),400
        finally:
            conn.close()

    @app.route('/api/sme/investment-properties/<int:property_id>/pos-products', methods=['POST'])
    @login_required
    def api_bdsdt_pos_products(property_id):
        conn=get_db_connection()
        try:
            begin_immediate(conn,label='bdsdt_pos_products')
            ids=ensure_property_pos_products(conn,property_id)
            sqlite_commit(conn,label='bdsdt_pos_products')
            codes={}
            for k,pid in ids.items():
                r=conn.execute('SELECT product_code FROM products WHERE id=?',(pid,)).fetchone()
                codes[k.replace('_id','_code')]=r[0] if r else None
            return jsonify({'success':True,**ids,**codes})
        except Exception as exc:
            rollback_quietly(conn); return jsonify({'success':False,'error':str(exc)}),400

    def _action(property_id, fn, label):
        data=request.get_json(silent=True) or {}; conn=get_db_connection()
        try:
            begin_immediate(conn,label='bdsdt_'+label)
            out=fn(conn,property_id,data)
            sqlite_commit(conn,label='bdsdt_'+label)
            return jsonify({'success':True,'journal':out})
        except Exception as exc:
            rollback_quietly(conn); return jsonify({'success':False,'error':str(exc)}),400


    @app.route('/api/sme/investment-properties/<int:property_id>/lease-plan', methods=['GET', 'POST'])
    @login_required
    def api_bdsdt_lease_plan(property_id):
        conn = get_db_connection()
        try:
            ensure_investment_property_e2e_schema(conn, commit=True)
            if request.method == 'GET':
                plan = get_active_lease_plan(conn, property_id)
                return jsonify({'success': True, 'data': plan})

            data = request.get_json(silent=True) or {}
            begin_immediate(conn, label='bdsdt_lease_plan')
            plan = save_lease_plan(
                conn,
                property_id,
                data,
                created_by=_user_name(),
            )
            ids = ensure_property_pos_products(conn, property_id)
            sqlite_commit(conn, label='bdsdt_lease_plan')
            return jsonify({'success': True, 'data': plan, 'pos_products': ids})
        except Exception as exc:
            rollback_quietly(conn)
            return jsonify({'success': False, 'error': str(exc)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/investment-properties/<int:property_id>/lease-plan/stop', methods=['POST'])
    @login_required
    def api_bdsdt_lease_plan_stop(property_id):
        conn = get_db_connection()
        try:
            begin_immediate(conn, label='bdsdt_lease_plan_stop')
            out = stop_lease_plan(conn, property_id, created_by=_user_name())
            sqlite_commit(conn, label='bdsdt_lease_plan_stop')
            return jsonify({'success': True, **out})
        except Exception as exc:
            rollback_quietly(conn)
            return jsonify({'success': False, 'error': str(exc)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/investment-properties/<int:property_id>/process-due', methods=['POST'])
    @login_required
    def api_bdsdt_process_due(property_id):
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            begin_immediate(conn, label='bdsdt_process_due')
            out = run_due_lease_plans(
                conn,
                as_of=data.get('as_of'),
                property_id=property_id,
                created_by=_user_name(),
            )
            sqlite_commit(conn, label='bdsdt_process_due')
            return jsonify({'success': True, 'data': out})
        except Exception as exc:
            rollback_quietly(conn)
            return jsonify({'success': False, 'error': str(exc)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/investment-properties/<int:property_id>/depreciation', methods=['POST'])
    @login_required
    def api_bdsdt_depreciation(property_id):
        return _action(property_id,lambda c,p,d:post_depreciation(c,p,amount=d.get('amount'),posting_date=d.get('date'),created_by=_user_name(),note=d.get('note','')),'depreciation')

    @app.route('/api/sme/investment-properties/<int:property_id>/impairment', methods=['POST'])
    @login_required
    def api_bdsdt_impairment(property_id):
        return _action(property_id,lambda c,p,d:post_impairment(c,p,amount=d.get('amount'),posting_date=d.get('date'),created_by=_user_name(),note=d.get('note','')),'impairment')

    @app.route('/api/sme/investment-properties/<int:property_id>/transfer-fixed-asset', methods=['POST'])
    @login_required
    def api_bdsdt_transfer_fa(property_id):
        return _action(property_id,lambda c,p,d:transfer_to_fixed_asset(c,p,posting_date=d.get('date'),fixed_asset_account=d.get('asset_account') or '2111',fixed_asset_accum_account=d.get('accum_account') or '2141',created_by=_user_name()),'transfer_fa')

    @app.route('/api/sme/investment-properties/<int:property_id>/transfer-inventory', methods=['POST'])
    @login_required
    def api_bdsdt_transfer_inventory(property_id):
        return _action(property_id,lambda c,p,d:transfer_to_inventory(c,p,posting_date=d.get('date'),inventory_account=d.get('inventory_account') or '1567',created_by=_user_name()),'transfer_inventory')
