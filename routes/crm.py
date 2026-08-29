# -*- coding: utf-8 -*-
"""Routes CRM — dashboard, leads, pipeline, quotes, customer 360."""
from __future__ import annotations

import sqlite3

from flask import jsonify, render_template, request, session

from auth import login_required
from db_utils import get_db_connection, sqlite_commit
from Services import crm as crm_svc


def _actor() -> str:
    return (
        session.get('user_name')
        or session.get('username')
        or (session.get('user') or {}).get('username')
        or ''
    )


def _session_role() -> str:
    return str(
        session.get('role')
        or (session.get('user') or {}).get('role')
        or ''
    ).strip()


def _crm_may_delete_leads() -> bool:
    from Services.sme_roles import FIELD_SALES_ROLE
    return _session_role() != FIELD_SALES_ROLE


def _crm_inbound_url() -> str:
    from flask import g
    tid = getattr(g, 'tenant_id', None)
    if tid:
        return f'/{tid}/api/crm/inbound-lead'
    return '/api/crm/inbound-lead'


def _conn():
    return get_db_connection()


def _api_locked(exc: Exception):
    from db_utils import locked_user_message
    msg = str(exc or '')
    if 'locked' in msg.lower():
        return jsonify({'error': locked_user_message(), 'retry': True}), 503
    return None


def register_crm_routes(app):

    # ── Pages ──────────────────────────────────────────────────────────

    @app.route('/crm')
    @login_required
    def crm_dashboard():
        return render_template('crm/dashboard.html')

    @app.route('/crm/leads')
    @login_required
    def crm_leads_page():
        return render_template('crm/leads.html')

    @app.route('/crm/pipeline')
    @login_required
    def crm_pipeline_page():
        return render_template('crm/pipeline.html')

    @app.route('/crm/quotes')
    @login_required
    def crm_quotes_page():
        return render_template('crm/quotes.html')

    @app.route('/crm/customer/<int:customer_id>')
    @login_required
    def crm_customer_360(customer_id):
        return render_template('crm/customer_360.html', customer_id=customer_id)

    @app.route('/crm/customers')
    @login_required
    def crm_customers_page():
        return render_template('crm/customers.html')

    @app.route('/crm/campaigns')
    @login_required
    def crm_campaigns_page():
        return render_template('crm/campaigns.html')

    @app.route('/crm/contracts')
    @login_required
    def crm_contracts_page():
        return render_template('crm/contracts.html')

    @app.route('/crm/tickets')
    @login_required
    def crm_tickets_page():
        return render_template('crm/tickets.html')

    @app.route('/crm/loyalty')
    @login_required
    def crm_loyalty_page():
        return render_template('crm/loyalty.html')

    @app.route('/crm/settings')
    @login_required
    def crm_settings_page():
        if _session_role() == 'staff_field':
            from flask import redirect, url_for
            return redirect(url_for('crm_leads_page'))
        return render_template('crm/settings.html')

    @app.route('/crm/visits')
    @login_required
    def crm_visits_page():
        return render_template('crm/visits.html')

    @app.route('/crm/inbound')
    @login_required
    def crm_inbound_page():
        return render_template('crm/inbound.html')

    # ── Public lead form (Website phase — không lộ Token) ─────────────

    def _public_lead_form_page():
        from Services.crm_inbound import get_public_form_settings
        from Services.crm_ops import ensure_inbound_token
        conn = _conn()
        try:
            ensure_inbound_token(conn)
            sqlite_commit(conn, label='crm_public_form_token')
            cfg = get_public_form_settings(conn)
            if not cfg.get('enabled'):
                return render_template(
                    'crm/public_lead_form.html',
                    form_disabled=True,
                    form_cfg=cfg,
                )
            # UTM từ query → hidden fields
            utm = {
                'utm_source': request.args.get('utm_source') or '',
                'utm_medium': request.args.get('utm_medium') or '',
                'utm_campaign': request.args.get('utm_campaign') or '',
                'source': request.args.get('source') or 'Website',
            }
            return render_template(
                'crm/public_lead_form.html',
                form_disabled=False,
                form_cfg=cfg,
                utm=utm,
            )
        finally:
            conn.close()

    @app.route('/lead', methods=['GET'])
    @app.route('/<tenant_id>/lead', methods=['GET'])
    def crm_public_lead_form(tenant_id=None):
        return _public_lead_form_page()

    @app.route('/api/crm/public-lead', methods=['POST'])
    @app.route('/<tenant_id>/api/crm/public-lead', methods=['POST'])
    def api_crm_public_lead(tenant_id=None):
        """Form website công khai — Token chỉ dùng phía server."""
        from Services.crm_inbound import get_public_form_settings, process_channel_inbound
        conn = _conn()
        try:
            cfg = get_public_form_settings(conn)
            if not cfg.get('enabled'):
                return jsonify({'success': False, 'error': 'Form công khai đang tắt'}), 403
            data = request.get_json(silent=True) or {}
            if not data:
                data = {k: request.form.get(k) for k in request.form}
            # Honeypot chống bot
            if (data.get('website_url') or data.get('hp_company') or '').strip():
                return jsonify({'success': True, 'id': 0, 'owner': None})
            if cfg.get('require_phone') and not (data.get('phone') or '').strip():
                return jsonify({'success': False, 'error': 'Vui lòng nhập số điện thoại'}), 400
            if not (data.get('contact_name') or data.get('name') or '').strip():
                return jsonify({'success': False, 'error': 'Vui lòng nhập họ tên'}), 400
            data.setdefault('source', 'Website')
            result = process_channel_inbound(
                conn, 'website', data, require_phone=bool(cfg.get('require_phone')),
            )
            sqlite_commit(conn, label='crm_public_lead')
            return jsonify({
                'success': True,
                'id': result.get('id'),
                'message': cfg.get('success_message'),
            })
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/visits', methods=['GET'])
    @login_required
    def api_crm_visits():
        from datetime import datetime
        from Services import crm_visits
        conn = _conn()
        try:
            owner = request.args.get('owner') or None
            cid = request.args.get('customer_id')
            vdate = request.args.get('date') or None
            items = crm_visits.list_visits(
                conn,
                owner=owner,
                customer_id=int(cid) if cid else None,
                visit_date=vdate,
                limit=int(request.args.get('limit') or 100),
            )
            payload = {'items': items}
            today = datetime.now().strftime('%Y-%m-%d')
            if request.args.get('sessions') == '1' and (not vdate or vdate == today):
                payload['sessions_today'] = crm_visits.list_visit_sessions_today(
                    conn, owner=owner,
                )
            return jsonify(payload)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Dashboard API ─────────────────────────────────────────────────

    @app.route('/api/crm/dashboard')
    @login_required
    def api_crm_dashboard():
        conn = _conn()
        try:
            return jsonify(crm_svc.dashboard_stats(conn))
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/meta')
    @login_required
    def api_crm_meta():
        return jsonify({
            'lead_statuses': list(crm_svc.LEAD_STATUSES),
            'opp_stages': list(crm_svc.OPP_STAGES),
            'opp_stage_labels': crm_svc.OPP_STAGE_LABELS,
            'lead_sources': list(crm_svc.LEAD_SOURCES),
            'activity_types': list(crm_svc.ACTIVITY_TYPES),
            'quote_statuses': list(crm_svc.QUOTE_STATUSES),
            'lifecycles': list(crm_svc.LIFECYCLES),
            'segments': list(crm_svc.SEGMENTS),
            'member_tiers': list(crm_svc.MEMBER_TIERS),
            'ticket_statuses': list(crm_svc.TICKET_STATUSES),
            'ticket_priorities': list(crm_svc.TICKET_PRIORITIES),
            'contract_statuses': list(crm_svc.CONTRACT_STATUSES),
            'campaign_statuses': list(crm_svc.CAMPAIGN_STATUSES),
        })

    # ── Leads ─────────────────────────────────────────────────────────

    @app.route('/api/crm/leads', methods=['GET', 'POST'])
    @login_required
    def api_crm_leads():
        conn = _conn()
        try:
            if request.method == 'GET':
                return jsonify(crm_svc.list_leads(
                    conn,
                    status=request.args.get('status') or None,
                    q=request.args.get('q') or '',
                    source=request.args.get('source') or None,
                ))
            data = request.get_json() or {}
            if not data.get('owner'):
                from Services.crm_ops import next_assignee
                data['owner'] = next_assignee(conn) or _actor()
            lid = crm_svc.upsert_lead(conn, data)
            sqlite_commit(conn, label='crm_lead_create')
            return jsonify({'success': True, 'id': lid, 'owner': data.get('owner')})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except sqlite3.Error as e:
            locked = _api_locked(e)
            if locked:
                return locked
            return jsonify({'error': str(e)}), 500
        except Exception as e:
            locked = _api_locked(e)
            if locked:
                return locked
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/leads/<int:lead_id>', methods=['GET', 'PUT', 'DELETE'])
    @login_required
    def api_crm_lead_one(lead_id):
        conn = _conn()
        try:
            if request.method == 'GET':
                row = crm_svc.get_lead(conn, lead_id)
                if not row:
                    return jsonify({'error': 'Không tìm thấy'}), 404
                return jsonify(row)
            if request.method == 'DELETE':
                if not _crm_may_delete_leads():
                    return jsonify({
                        'error': 'NV Bán hàng thị trường không được xóa lead. Chỉ tạo / sửa.',
                    }), 403
                crm_svc.delete_lead(conn, lead_id)
                sqlite_commit(conn, label='crm_lead_delete')
                return jsonify({'success': True})
            data = request.get_json() or {}
            crm_svc.upsert_lead(conn, data, lead_id=lead_id)
            sqlite_commit(conn, label='crm_lead_update')
            return jsonify({'success': True, 'id': lead_id})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except sqlite3.Error as e:
            locked = _api_locked(e)
            if locked:
                return locked
            return jsonify({'error': str(e)}), 500
        except Exception as e:
            locked = _api_locked(e)
            if locked:
                return locked
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/leads/<int:lead_id>/convert', methods=['POST'])
    @login_required
    def api_crm_lead_convert(lead_id):
        conn = _conn()
        try:
            result = crm_svc.convert_lead(conn, lead_id, owner=_actor())
            sqlite_commit(conn, label='crm_lead_convert')
            return jsonify({'success': True, **result})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Opportunities / pipeline ──────────────────────────────────────

    @app.route('/api/crm/opportunities', methods=['GET', 'POST'])
    @login_required
    def api_crm_opportunities():
        conn = _conn()
        try:
            if request.method == 'GET':
                cid = request.args.get('customer_id')
                return jsonify(crm_svc.list_opportunities(
                    conn,
                    stage=request.args.get('stage') or None,
                    customer_id=int(cid) if cid else None,
                ))
            data = request.get_json() or {}
            if not data.get('owner'):
                data['owner'] = _actor()
            oid = crm_svc.upsert_opportunity(conn, data)
            sqlite_commit(conn, label='crm_opp_create')
            return jsonify({'success': True, 'id': oid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/opportunities/<int:opp_id>', methods=['GET', 'PUT', 'DELETE'])
    @login_required
    def api_crm_opp_one(opp_id):
        conn = _conn()
        try:
            if request.method == 'GET':
                row = crm_svc.get_opportunity(conn, opp_id)
                if not row:
                    return jsonify({'error': 'Không tìm thấy'}), 404
                return jsonify(row)
            if request.method == 'DELETE':
                crm_svc.delete_opportunity(conn, opp_id)
                sqlite_commit(conn, label='crm_opp_delete')
                return jsonify({'success': True})
            data = request.get_json() or {}
            crm_svc.upsert_opportunity(conn, data, opp_id=opp_id)
            sqlite_commit(conn, label='crm_opp_update')
            return jsonify({'success': True, 'id': opp_id})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/pipeline')
    @login_required
    def api_crm_pipeline():
        conn = _conn()
        try:
            return jsonify(crm_svc.pipeline_summary(conn))
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Activities ────────────────────────────────────────────────────

    @app.route('/api/crm/activities', methods=['GET', 'POST'])
    @login_required
    def api_crm_activities():
        conn = _conn()
        try:
            if request.method == 'GET':
                cid = request.args.get('customer_id')
                lid = request.args.get('lead_id')
                oid = request.args.get('opportunity_id')
                return jsonify(crm_svc.list_activities(
                    conn,
                    customer_id=int(cid) if cid else None,
                    lead_id=int(lid) if lid else None,
                    opportunity_id=int(oid) if oid else None,
                    upcoming_only=request.args.get('upcoming') == '1',
                ))
            data = request.get_json() or {}
            data.setdefault('created_by', _actor())
            data.setdefault('owner', _actor())
            aid = crm_svc.add_activity(conn, data)
            sqlite_commit(conn, label='crm_activity')
            return jsonify({'success': True, 'id': aid})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/activities/<int:activity_id>', methods=['DELETE'])
    @login_required
    def api_crm_activity_delete(activity_id):
        conn = _conn()
        try:
            crm_svc.delete_activity(conn, activity_id)
            sqlite_commit(conn, label='crm_activity_delete')
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Quotes ────────────────────────────────────────────────────────

    @app.route('/api/crm/quotes', methods=['GET', 'POST'])
    @login_required
    def api_crm_quotes():
        conn = _conn()
        try:
            if request.method == 'GET':
                cid = request.args.get('customer_id')
                return jsonify(crm_svc.list_quotes(
                    conn,
                    status=request.args.get('status') or None,
                    customer_id=int(cid) if cid else None,
                ))
            data = request.get_json() or {}
            if not data.get('owner'):
                data['owner'] = _actor()
            qid = crm_svc.upsert_quote(conn, data)
            sqlite_commit(conn, label='crm_quote_create')
            return jsonify({'success': True, 'id': qid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/quotes/<int:quote_id>', methods=['GET', 'PUT', 'DELETE'])
    @login_required
    def api_crm_quote_one(quote_id):
        conn = _conn()
        try:
            if request.method == 'GET':
                row = crm_svc.get_quote(conn, quote_id)
                if not row:
                    return jsonify({'error': 'Không tìm thấy'}), 404
                return jsonify(row)
            if request.method == 'DELETE':
                crm_svc.delete_quote(conn, quote_id)
                sqlite_commit(conn, label='crm_quote_delete')
                return jsonify({'success': True})
            data = request.get_json() or {}
            crm_svc.upsert_quote(conn, data, quote_id=quote_id)
            sqlite_commit(conn, label='crm_quote_update')
            return jsonify({'success': True, 'id': quote_id})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/quotes/<int:quote_id>/convert-sale', methods=['POST'])
    @login_required
    def api_crm_quote_convert(quote_id):
        conn = _conn()
        try:
            result = crm_svc.convert_quote_to_sale(conn, quote_id)
            sqlite_commit(conn, label='crm_quote_to_sale')
            return jsonify({'success': True, **result})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except sqlite3.Error as e:
            return jsonify({'error': str(e)}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Customer 360 / CRM profile ────────────────────────────────────

    @app.route('/api/crm/customers/<int:customer_id>/360')
    @login_required
    def api_crm_customer_360(customer_id):
        conn = _conn()
        try:
            return jsonify(crm_svc.customer_360(conn, customer_id))
        except ValueError as e:
            return jsonify({'error': str(e)}), 404
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/customers/<int:customer_id>/profile', methods=['PUT'])
    @login_required
    def api_crm_customer_profile(customer_id):
        conn = _conn()
        try:
            data = request.get_json() or {}
            crm_svc.update_customer_crm(conn, customer_id, data)
            from Services import crm_ops
            loyalty_keys = (
                'crm_birthday', 'crm_member_code', 'crm_member_tier', 'crm_loyalty_points'
            )
            if any(k in data for k in loyalty_keys):
                crm_ops.update_loyalty(conn, customer_id, data)
            sqlite_commit(conn, label='crm_customer_profile')
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Analytics ─────────────────────────────────────────────────────

    @app.route('/api/crm/analytics')
    @login_required
    def api_crm_analytics():
        conn = _conn()
        try:
            from Services.crm_analytics import analytics_bundle
            return jsonify(analytics_bundle(conn))
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Campaigns ─────────────────────────────────────────────────────

    @app.route('/api/crm/campaigns', methods=['GET', 'POST'])
    @login_required
    def api_crm_campaigns():
        from Services import crm_ops
        conn = _conn()
        try:
            if request.method == 'GET':
                return jsonify(crm_ops.list_campaigns(conn, request.args.get('status')))
            data = request.get_json() or {}
            cid = crm_ops.upsert_campaign(conn, data)
            sqlite_commit(conn, label='crm_campaign')
            return jsonify({'success': True, 'id': cid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/campaigns/<int:cid>', methods=['PUT', 'DELETE'])
    @login_required
    def api_crm_campaign_one(cid):
        from Services import crm_ops
        conn = _conn()
        try:
            if request.method == 'DELETE':
                crm_ops.delete_campaign(conn, cid)
                sqlite_commit(conn, label='crm_campaign_del')
                return jsonify({'success': True})
            crm_ops.upsert_campaign(conn, request.get_json() or {}, cid=cid)
            sqlite_commit(conn, label='crm_campaign_upd')
            return jsonify({'success': True, 'id': cid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Targets ───────────────────────────────────────────────────────

    @app.route('/api/crm/targets', methods=['GET', 'POST'])
    @login_required
    def api_crm_targets():
        from Services import crm_ops
        conn = _conn()
        try:
            if request.method == 'GET':
                return jsonify(crm_ops.list_targets(conn, request.args.get('period_key')))
            tid = crm_ops.upsert_target(conn, request.get_json() or {})
            sqlite_commit(conn, label='crm_target')
            return jsonify({'success': True, 'id': tid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/targets/<int:tid>', methods=['DELETE'])
    @login_required
    def api_crm_target_del(tid):
        from Services import crm_ops
        conn = _conn()
        try:
            crm_ops.delete_target(conn, tid)
            sqlite_commit(conn, label='crm_target_del')
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Contracts ─────────────────────────────────────────────────────

    @app.route('/api/crm/contracts', methods=['GET', 'POST'])
    @login_required
    def api_crm_contracts():
        from Services import crm_ops
        conn = _conn()
        try:
            if request.method == 'GET':
                cid = request.args.get('customer_id')
                return jsonify(crm_ops.list_contracts(
                    conn, customer_id=int(cid) if cid else None
                ))
            data = request.get_json() or {}
            if not data.get('owner'):
                data['owner'] = _actor()
            cid = crm_ops.upsert_contract(conn, data)
            sqlite_commit(conn, label='crm_contract')
            return jsonify({'success': True, 'id': cid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/contracts/template', methods=['GET', 'PUT', 'DELETE'])
    @login_required
    def api_crm_contract_template():
        from Services import crm_contract_template as tpl
        conn = _conn()
        try:
            if request.method == 'GET':
                meta = tpl.get_template_meta(conn)
                return jsonify({
                    'html': meta['html'],
                    'placeholders': tpl.placeholders_guide(),
                    'used': tpl.extract_placeholders(meta['html']),
                    'is_custom': meta['is_custom'],
                    'tenant_scoped': True,
                    'scope_note': 'Mẫu chỉ lưu trong dữ liệu doanh nghiệp hiện tại; tenant khác không bị ảnh hưởng.',
                })
            if request.method == 'DELETE':
                tpl.reset_template(conn)
                sqlite_commit(conn, label='crm_contract_tpl_reset')
                return jsonify({'success': True, 'html': tpl.DEFAULT_TEMPLATE_HTML})
            data = request.get_json(silent=True) or {}
            html_body = data.get('html')
            if html_body is None and request.data:
                html_body = request.get_data(as_text=True)
            tpl.set_template_html(conn, html_body or '')
            sqlite_commit(conn, label='crm_contract_tpl')
            return jsonify({'success': True})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/contracts/template/export')
    @login_required
    def api_crm_contract_template_export():
        from flask import Response
        from Services import crm_contract_template as tpl
        conn = _conn()
        try:
            body = tpl.get_template_html(conn)
            return Response(
                body,
                mimetype='text/html; charset=utf-8',
                headers={
                    'Content-Disposition': 'attachment; filename="mau-hop-dong-mua-ban.html"'
                },
            )
        finally:
            conn.close()

    @app.route('/api/crm/contracts/template/import', methods=['POST'])
    @login_required
    def api_crm_contract_template_import():
        import html as html_mod
        import re

        from Services import crm_contract_template as tpl
        conn = _conn()
        try:
            f = request.files.get('file')
            raw = ''
            if f and f.filename:
                name = (f.filename or '').lower()
                data = f.read()
                if name.endswith(('.html', '.htm', '.txt')):
                    raw = data.decode('utf-8', errors='replace')
                elif name.endswith('.docx'):
                    import io
                    import zipfile
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        xml = zf.read('word/document.xml').decode('utf-8', errors='replace')
                    # Giữ placeholder [[...]] trong text Word
                    text = re.sub(r'</w:p>', '\n', xml)
                    text = re.sub(r'<[^>]+>', '', text)
                    text = html_mod.unescape(text)
                    # Nếu user lưu từ Word dạng HTML đầy đủ hơn nên ưu tiên .html;
                    # với docx chỉ nhận nếu còn đủ marker — bọc lại khung HTML tối thiểu
                    if '[[CONTRACT_NO]]' in text and '[[ITEMS_TABLE]]' in text:
                        raw = (
                            '<!DOCTYPE html><html lang="vi"><head><meta charset="utf-8"/>'
                            '<title>Hợp đồng</title></head><body>'
                            + '<pre style="white-space:pre-wrap;font-family:Times New Roman,serif">'
                            + html_mod.escape(text)
                            + '</pre></body></html>'
                        )
                        # Không escape placeholders
                        for k, _ in tpl.KNOWN_PLACEHOLDERS:
                            raw = raw.replace(html_mod.escape(f'[[{k}]]'), f'[[{k}]]')
                    else:
                        return jsonify({
                            'error': 'File .docx thiếu mã [[CONTRACT_NO]] / [[ITEMS_TABLE]] / [[TOTAL]]. '
                                     'Nên xuất mẫu HTML, sửa trong Word rồi Lưu dưới dạng Trang web (.html).'
                        }), 400
                else:
                    return jsonify({
                        'error': 'Chỉ nhận .html / .htm / .txt (khuyến nghị) hoặc .docx có đủ placeholder.'
                    }), 400
            else:
                raw = (request.get_json(silent=True) or {}).get('html') or ''
            tpl.set_template_html(conn, raw)
            sqlite_commit(conn, label='crm_contract_tpl_import')
            return jsonify({
                'success': True,
                'placeholders': tpl.extract_placeholders(raw),
            })
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/contracts/preview', methods=['POST'])
    @login_required
    def api_crm_contract_preview():
        from flask import Response
        from Services import crm_contract_template as tpl
        from Services import crm_ops
        conn = _conn()
        try:
            data = request.get_json() or {}
            items = data.get('items') or []
            if not isinstance(items, list):
                items = []
            subtotal, tax_amount, total = crm_ops._calc_contract_totals(items)
            data['subtotal'] = subtotal
            data['tax_amount'] = tax_amount
            data['amount'] = total if items else data.get('amount') or total
            data['items'] = items
            # bổ sung thông tin KH nếu có id
            cust_id = data.get('customer_id')
            if cust_id:
                row = conn.execute(
                    """
                    SELECT COALESCE(company_name, name) AS customer_name,
                           tax_code AS customer_tax_code, address AS customer_address,
                           phone AS customer_phone, email AS customer_email
                    FROM customers WHERE id = ?
                    """,
                    (cust_id,),
                ).fetchone()
                if row:
                    d = dict(row) if not isinstance(row, dict) else row
                    data.update(d)
            if not data.get('contract_no'):
                data['contract_no'] = 'XEM-TRUOC'
            html_out = tpl.render_contract_html(conn, data)
            return Response(html_out, mimetype='text/html; charset=utf-8')
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/contracts/<int:cid>', methods=['GET', 'PUT', 'DELETE'])
    @login_required
    def api_crm_contract_one(cid):
        from Services import crm_ops
        conn = _conn()
        try:
            if request.method == 'GET':
                row = crm_ops.get_contract(conn, cid)
                if not row:
                    return jsonify({'error': 'Không tìm thấy hợp đồng'}), 404
                return jsonify(row)
            if request.method == 'DELETE':
                crm_ops.delete_contract(conn, cid)
                sqlite_commit(conn, label='crm_contract_del')
                return jsonify({'success': True})
            crm_ops.upsert_contract(conn, request.get_json() or {}, cid=cid)
            sqlite_commit(conn, label='crm_contract_upd')
            return jsonify({'success': True, 'id': cid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/contracts/<int:cid>/export')
    @login_required
    def api_crm_contract_export(cid):
        from flask import Response
        from Services import crm_contract_template as tpl
        from Services import crm_ops
        conn = _conn()
        try:
            row = crm_ops.get_contract(conn, cid)
            if not row:
                return jsonify({'error': 'Không tìm thấy hợp đồng'}), 404
            html_out = tpl.render_contract_html(conn, row)
            fname = f"hop-dong-{(row.get('contract_no') or cid)}.html"
            return Response(
                html_out,
                mimetype='text/html; charset=utf-8',
                headers={'Content-Disposition': f'attachment; filename="{fname}"'},
            )
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/contracts/<int:cid>/print')
    @login_required
    def api_crm_contract_print(cid):
        from flask import Response
        from Services import crm_contract_template as tpl
        from Services import crm_ops
        conn = _conn()
        try:
            row = crm_ops.get_contract(conn, cid)
            if not row:
                return jsonify({'error': 'Không tìm thấy hợp đồng'}), 404
            return Response(tpl.render_contract_html(conn, row), mimetype='text/html; charset=utf-8')
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Tickets ───────────────────────────────────────────────────────

    @app.route('/api/crm/tickets', methods=['GET', 'POST'])
    @login_required
    def api_crm_tickets():
        from Services import crm_ops
        conn = _conn()
        try:
            if request.method == 'GET':
                cid = request.args.get('customer_id')
                return jsonify(crm_ops.list_tickets(
                    conn,
                    status=request.args.get('status'),
                    customer_id=int(cid) if cid else None,
                ))
            data = request.get_json() or {}
            data.setdefault('created_by', _actor())
            data.setdefault('assignee', _actor())
            tid = crm_ops.upsert_ticket(conn, data)
            sqlite_commit(conn, label='crm_ticket')
            return jsonify({'success': True, 'id': tid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/tickets/<int:tid>', methods=['GET', 'PUT', 'DELETE'])
    @login_required
    def api_crm_ticket_one(tid):
        from Services import crm_ops
        conn = _conn()
        try:
            if request.method == 'GET':
                row = crm_ops.get_ticket(conn, tid)
                if not row:
                    return jsonify({'error': 'Không tìm thấy'}), 404
                return jsonify(row)
            if request.method == 'DELETE':
                crm_ops.delete_ticket(conn, tid)
                sqlite_commit(conn, label='crm_ticket_del')
                return jsonify({'success': True})
            crm_ops.upsert_ticket(conn, request.get_json() or {}, tid=tid)
            sqlite_commit(conn, label='crm_ticket_upd')
            return jsonify({'success': True, 'id': tid})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/tickets/<int:tid>/events', methods=['POST'])
    @login_required
    def api_crm_ticket_event(tid):
        from Services import crm_ops
        conn = _conn()
        try:
            data = request.get_json() or {}
            eid = crm_ops.add_ticket_event(
                conn, tid, data.get('content') or '',
                created_by=_actor(), event_type=data.get('event_type') or 'note',
            )
            sqlite_commit(conn, label='crm_ticket_event')
            return jsonify({'success': True, 'id': eid})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    # ── Surveys / notifications / settings / inbound ──────────────────

    @app.route('/api/crm/surveys', methods=['GET', 'POST'])
    @login_required
    def api_crm_surveys():
        from Services import crm_ops
        conn = _conn()
        try:
            if request.method == 'GET':
                cid = request.args.get('customer_id')
                return jsonify(crm_ops.list_surveys(
                    conn, customer_id=int(cid) if cid else None
                ))
            sid = crm_ops.add_survey(conn, request.get_json() or {})
            sqlite_commit(conn, label='crm_survey')
            return jsonify({'success': True, 'id': sid})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/notifications')
    @login_required
    def api_crm_notifications():
        from Services import crm_ops
        conn = _conn()
        try:
            return jsonify(crm_ops.list_notifications(
                conn,
                owner=request.args.get('owner') or _actor(),
                unread_only=request.args.get('unread') == '1',
            ))
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/notifications/<int:nid>/read', methods=['POST'])
    @login_required
    def api_crm_notif_read(nid):
        from Services import crm_ops
        conn = _conn()
        try:
            crm_ops.mark_notification_read(conn, nid)
            sqlite_commit(conn, label='crm_notif_read')
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/reminders/scan', methods=['POST'])
    @login_required
    def api_crm_reminders_scan():
        from Services import crm_ops
        conn = _conn()
        try:
            result = crm_ops.scan_reminders(conn)
            sqlite_commit(conn, label='crm_reminders')
            return jsonify({'success': True, **result})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/assignable-users')
    @login_required
    def api_crm_assignable_users():
        """Danh sách NV Bán hàng (Settings → Users, role staff) — read-only."""
        from Services import crm_ops
        conn = _conn()
        try:
            staff = crm_ops.list_crm_sales_staff(conn)
            return jsonify({
                'success': True,
                'items': staff,
                'sales_staff': staff,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/settings', methods=['GET', 'PUT'])
    @login_required
    def api_crm_settings():
        from Services import crm_ops
        from Services.crm_analytics import revenue_vs_target
        from Services.crm_kpi_bridge import prefer_hr_kpi, resolve_hr_sales_rev_target
        conn = _conn()
        try:
            def _kpi_bridge_payload():
                prefer = prefer_hr_kpi(conn)
                kpi = revenue_vs_target(conn, 'month')
                hr = resolve_hr_sales_rev_target(
                    conn,
                    period_type='month',
                    period_key=kpi.get('period_key') or '',
                )
                # SME vs HKD: endpoint KPI settings
                kpi_url = '/kpi_settings'
                try:
                    from Services.tenant_profile import (
                        get_current_tenant_profile,
                        is_sme_regime,
                    )
                    profile = get_current_tenant_profile() or {}
                    if is_sme_regime(profile.get('accounting_regime')):
                        kpi_url = '/SME_kpi_settings'
                except Exception:
                    if app.view_functions.get('SME_kpi_settings'):
                        kpi_url = '/SME_kpi_settings'
                return {
                    'prefer_hr': prefer,
                    'kpi_settings_url': kpi_url,
                    'hr_sales_rev': {
                        'found': bool(hr.get('found')),
                        'target': hr.get('target') or 0,
                        'source': hr.get('source'),
                        'detail': hr.get('detail') or '',
                    },
                    'gauge': {
                        'period_key': kpi.get('period_key'),
                        'target': kpi.get('target') or 0,
                        'actual': kpi.get('actual') or 0,
                        'percent': kpi.get('percent') or 0,
                        'target_source': kpi.get('target_source') or 'none',
                        'target_detail': kpi.get('target_detail') or '',
                    },
                }

            if request.method == 'GET':
                from Services import crm_email as crm_mail
                try:
                    token = crm_ops.ensure_inbound_token(conn)
                except Exception as tok_exc:
                    app.logger.warning('crm settings token: %s', tok_exc)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    token = ''
                try:
                    owners = crm_ops.sync_assign_owners_from_staff(conn)
                    sqlite_commit(conn, label='crm_settings_token')
                except Exception as own_exc:
                    app.logger.warning('crm settings owners: %s', own_exc)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    owners = []
                from flask import g
                tid = getattr(g, 'tenant_id', None)
                try:
                    staff = crm_ops.list_crm_sales_staff(conn)
                except Exception as st_exc:
                    app.logger.warning('crm settings staff: %s', st_exc)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    staff = []
                try:
                    kpi_prefer = prefer_hr_kpi(conn)
                except Exception:
                    kpi_prefer = False
                try:
                    kpi_bridge = _kpi_bridge_payload()
                except Exception as kpi_exc:
                    # KPI SQL lỗi (PG compat) không được chặn danh sách NV bán hàng
                    app.logger.warning('crm settings kpi_bridge: %s', kpi_exc)
                    kpi_bridge = {
                        'prefer_hr': bool(kpi_prefer),
                        'kpi_settings_url': '/kpi_settings',
                        'hr_sales_rev': {
                            'found': False, 'target': 0, 'source': None, 'detail': '',
                        },
                        'gauge': {
                            'period_key': '', 'target': 0, 'actual': 0, 'percent': 0,
                            'target_source': 'none', 'target_detail': '',
                        },
                        'error': str(kpi_exc)[:200],
                    }
                try:
                    smtp = crm_mail.get_tenant_smtp_public(conn)
                except Exception:
                    smtp = {}
                return jsonify({
                    'inbound_token': token,
                    'assign_owners': owners,
                    'sales_staff': staff,
                    'assignable_users': staff,
                    'inbound_url': _crm_inbound_url(),
                    'tenant_id': tid,
                    'kpi_prefer_hr': kpi_prefer,
                    'kpi_bridge': kpi_bridge,
                    'smtp': smtp,
                })
            data = request.get_json() or {}
            if 'kpi_prefer_hr' in data:
                from Services.crm_ops import set_setting
                val = data.get('kpi_prefer_hr')
                on = str(val).strip().lower() in ('1', 'true', 'yes', 'on')
                set_setting(conn, 'kpi_prefer_hr', '1' if on else '0')
            if data.get('rotate_token'):
                from Services.crm_ops import set_setting
                import secrets
                set_setting(conn, 'inbound_token', secrets.token_urlsafe(24))
            owners = crm_ops.sync_assign_owners_from_staff(conn)
            sqlite_commit(conn, label='crm_settings')
            from Services import crm_email as crm_mail
            staff = crm_ops.list_crm_sales_staff(conn)
            return jsonify({'success': True, **{
                'inbound_token': crm_ops.ensure_inbound_token(conn),
                'assign_owners': owners,
                'sales_staff': staff,
                'assignable_users': staff,
                'inbound_url': _crm_inbound_url(),
                'kpi_prefer_hr': prefer_hr_kpi(conn),
                'kpi_bridge': _kpi_bridge_payload(),
                'smtp': crm_mail.get_tenant_smtp_public(conn),
            }})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/settings/smtp', methods=['GET', 'PUT'])
    @login_required
    def api_crm_settings_smtp():
        from Services import crm_email as crm_mail
        conn = _conn()
        try:
            if request.method == 'GET':
                return jsonify(crm_mail.get_tenant_smtp_public(conn))
            smtp = crm_mail.save_tenant_smtp(conn, request.get_json() or {})
            sqlite_commit(conn, label='crm_smtp_save')
            return jsonify({'success': True, 'smtp': smtp})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/settings/smtp/test', methods=['POST'])
    @login_required
    def api_crm_settings_smtp_test():
        from Services import crm_email as crm_mail
        conn = _conn()
        try:
            data = request.get_json() or {}
            ok, err = crm_mail.test_tenant_smtp(conn, data.get('to_email'))
            if not ok:
                return jsonify({'success': False, 'error': err or 'Gửi thử thất bại'}), 400
            return jsonify({'success': True, 'message': 'Đã gửi email thử'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/quotes/<int:quote_id>/send-email', methods=['POST'])
    @login_required
    def api_crm_quote_send_email(quote_id):
        from Services import crm_email as crm_mail
        conn = _conn()
        try:
            quote = crm_svc.get_quote(conn, quote_id)
            if not quote:
                return jsonify({'error': 'Không tìm thấy báo giá'}), 404
            data = request.get_json() or {}
            to_email = (data.get('to_email') or quote.get('customer_email') or '').strip()
            if not to_email:
                return jsonify({'error': 'Khách hàng chưa có email. Nhập email nhận hoặc cập nhật hồ sơ KH.'}), 400
            subject, text, html = crm_mail.build_quote_email(quote)
            if data.get('subject'):
                subject = str(data['subject']).strip() or subject
            ok, err, source = crm_mail.send_tenant_email(
                conn, to_email, subject, text, html_body=html,
            )
            crm_mail.log_crm_email(
                conn, kind='quote', ref_id=quote_id, to_email=to_email,
                subject=subject, status='ok' if ok else 'error', error=err,
            )
            sqlite_commit(conn, label='crm_quote_email')
            if not ok:
                return jsonify({'success': False, 'error': err}), 400
            return jsonify({'success': True, 'to_email': to_email, 'source': source})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/contracts/<int:cid>/send-email', methods=['POST'])
    @login_required
    def api_crm_contract_send_email(cid):
        from Services import crm_contract_template as tpl
        from Services import crm_email as crm_mail
        from Services import crm_ops
        conn = _conn()
        try:
            row = crm_ops.get_contract(conn, cid)
            if not row:
                return jsonify({'error': 'Không tìm thấy hợp đồng'}), 404
            data = request.get_json() or {}
            to_email = (data.get('to_email') or row.get('customer_email') or '').strip()
            if not to_email:
                # fallback customers.email
                cid_cust = row.get('customer_id')
                if cid_cust:
                    er = conn.execute(
                        'SELECT email FROM customers WHERE id = ?', (int(cid_cust),)
                    ).fetchone()
                    if er:
                        to_email = (er['email'] if hasattr(er, 'keys') else er[0] or '') or ''
                        to_email = str(to_email).strip()
            if not to_email:
                return jsonify({'error': 'Khách hàng chưa có email. Nhập email nhận hoặc cập nhật hồ sơ KH.'}), 400
            attach_html = data.get('attach_html', True)
            html_doc = tpl.render_contract_html(conn, row) if attach_html else None
            subject, text, html = crm_mail.build_contract_email(row, html_doc=html_doc)
            if data.get('subject'):
                subject = str(data['subject']).strip() or subject
            ok, err, source = crm_mail.send_tenant_email(
                conn, to_email, subject, text, html_body=html,
            )
            crm_mail.log_crm_email(
                conn, kind='contract', ref_id=cid, to_email=to_email,
                subject=subject, status='ok' if ok else 'error', error=err,
            )
            sqlite_commit(conn, label='crm_contract_email')
            if not ok:
                return jsonify({'success': False, 'error': err}), 400
            return jsonify({'success': True, 'to_email': to_email, 'source': source})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/campaigns/<int:cid>/send-email', methods=['POST'])
    @login_required
    def api_crm_campaign_send_email(cid):
        from Services import crm_email as crm_mail
        from Services import crm_ops
        conn = _conn()
        try:
            camps = crm_ops.list_campaigns(conn)
            camp = next((c for c in camps if int(c.get('id') or 0) == int(cid)), None)
            if not camp:
                return jsonify({'error': 'Không tìm thấy chiến dịch'}), 404
            data = request.get_json() or {}
            camp = dict(camp)
            if data.get('subject'):
                camp['email_subject'] = str(data['subject']).strip()
            if data.get('body'):
                camp['email_body'] = str(data['body'])
            subject, text, html = crm_mail.build_campaign_email(camp)
            ids = data.get('customer_ids')
            if ids is not None and not isinstance(ids, list):
                ids = None
            limit = min(int(data.get('limit') or 100), 200)
            recipients = crm_mail.list_customer_emails(
                conn, customer_ids=[int(x) for x in ids] if ids else None, limit=limit,
            )
            if not recipients:
                return jsonify({'error': 'Không có khách hàng nào có email'}), 400
            sent, failed = 0, 0
            errors = []
            for r in recipients:
                to_email = (r.get('email') or '').strip()
                ok, err, _src = crm_mail.send_tenant_email(
                    conn, to_email, subject, text, html_body=html,
                )
                crm_mail.log_crm_email(
                    conn, kind='campaign', ref_id=cid, to_email=to_email,
                    subject=subject, status='ok' if ok else 'error', error=err,
                )
                if ok:
                    sent += 1
                else:
                    failed += 1
                    if len(errors) < 10:
                        errors.append({'email': to_email, 'error': err})
            sqlite_commit(conn, label='crm_campaign_email')
            if sent == 0:
                return jsonify({
                    'success': False,
                    'error': (errors[0]['error'] if errors else 'Gửi thất bại'),
                    'sent': 0, 'failed': failed, 'errors': errors,
                }), 400
            return jsonify({
                'success': True,
                'sent': sent,
                'failed': failed,
                'total': len(recipients),
                'errors': errors,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/leads/<int:lead_id>/assign', methods=['POST'])
    @login_required
    def api_crm_lead_assign(lead_id):
        from Services import crm_ops
        conn = _conn()
        try:
            data = request.get_json() or {}
            owner = crm_ops.assign_lead(conn, lead_id, data.get('owner'))
            sqlite_commit(conn, label='crm_assign')
            if not owner:
                return jsonify({'error': 'Chưa cấu hình danh sách sales (CRM Settings)'}), 400
            return jsonify({'success': True, 'owner': owner})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/inbound-hub', methods=['GET'])
    @login_required
    def api_crm_inbound_hub():
        from flask import g
        from Services.crm_inbound import inbound_hub_payload
        from Services import crm_ops
        conn = _conn()
        try:
            try:
                token = crm_ops.ensure_inbound_token(conn)
                sqlite_commit(conn, label='crm_inbound_hub_token')
            except Exception as tok_exc:
                app.logger.warning('crm inbound hub token: %s', tok_exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                token = ''
            base = request.host_url.rstrip('/')
            tid = getattr(g, 'tenant_id', None)
            endpoint = base + _crm_inbound_url()
            if tid:
                public_form_url = f'{base}/{tid}/lead'
                public_api = f'{base}/{tid}/api/crm/public-lead'
            else:
                public_form_url = f'{base}/lead'
                public_api = f'{base}/api/crm/public-lead'
            hub = inbound_hub_payload(
                conn, endpoint=endpoint, token=token, base_url=base, tenant_id=tid,
            )
            hub['token'] = token
            hub['public_form_url'] = public_form_url
            hub['public_api_url'] = public_api
            return jsonify(hub)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/inbound-hub/phase', methods=['POST'])
    @login_required
    def api_crm_inbound_phase():
        from Services.crm_inbound import set_phase_done
        data = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            status = set_phase_done(
                conn,
                str(data.get('phase_id') or ''),
                bool(data.get('done')),
            )
            sqlite_commit(conn, label='crm_inbound_phase')
            return jsonify({'success': True, 'phases': status})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/inbound-hub/public-form', methods=['PUT'])
    @login_required
    def api_crm_inbound_public_form():
        from Services.crm_inbound import set_public_form_settings
        conn = _conn()
        try:
            cfg = set_public_form_settings(conn, request.get_json(silent=True) or {})
            sqlite_commit(conn, label='crm_public_form_cfg')
            return jsonify({'success': True, 'public_form': cfg})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/inbound-hub/test', methods=['POST'])
    @login_required
    def api_crm_inbound_test():
        """Tạo lead thử theo nguồn/kênh (session đăng nhập, không cần Token)."""
        from Services.crm_inbound import process_channel_inbound
        from Services.crm_inbound_adapters import CHANNEL_TO_SOURCE
        data = request.get_json(silent=True) or {}
        conn = _conn()
        try:
            source = (data.get('source') or 'Website').strip()
            channel = (data.get('channel') or '').strip().lower()
            if not channel:
                rev = {v: k for k, v in CHANNEL_TO_SOURCE.items()}
                channel = rev.get(source, 'website')
            payload = {
                'contact_name': data.get('contact_name') or f'Lead thử {source}',
                'phone': data.get('phone') or '0900000000',
                'email': data.get('email') or '',
                'source': source,
                'notes': data.get('notes') or f'Test inbound Hub — {source}',
                'utm_source': data.get('utm_source') or 'test',
                'utm_medium': data.get('utm_medium') or 'crm_hub',
                'utm_campaign': data.get('utm_campaign') or 'inbound_test',
                'external_id': data.get('external_id') or f'test_{channel}_{int(__import__("time").time())}',
            }
            # Sample native shapes for adapters
            if data.get('native'):
                if channel == 'facebook':
                    payload = {
                        'field_data': [
                            {'name': 'full_name', 'values': [payload['contact_name']]},
                            {'name': 'phone_number', 'values': [payload['phone']]},
                        ],
                        'leadgen_id': payload['external_id'],
                    }
                elif channel == 'tiktok':
                    payload = {
                        'data': [{'leads': [{
                            'lead_id': payload['external_id'],
                            'name': payload['contact_name'],
                            'phone_number': payload['phone'],
                        }]}],
                    }
                elif channel == 'zalo':
                    payload = {
                        'sender': {'id': payload['external_id'], 'name': payload['contact_name']},
                        'message': {'text': payload['notes']},
                        'phone': payload['phone'],
                    }
                elif channel == 'google':
                    payload = {
                        'user_column_data': [
                            {'column_id': 'FULL_NAME', 'string_value': payload['contact_name']},
                            {'column_id': 'PHONE_NUMBER', 'string_value': payload['phone']},
                        ],
                        'lead_id': payload['external_id'],
                    }
                elif channel == 'whatsapp':
                    payload = {
                        'entry': [{'changes': [{'value': {
                            'contacts': [{'wa_id': payload['phone'], 'profile': {'name': payload['contact_name']}}],
                            'messages': [{'id': payload['external_id'], 'from': payload['phone'],
                                         'text': {'body': payload['notes']}}],
                        }}]}],
                    }
                elif channel == 'viber':
                    payload = {
                        'sender': {'name': payload['contact_name'], 'id': payload['external_id']},
                        'message': {'text': payload['notes']},
                        'phone': payload['phone'],
                    }
                elif channel == 'hotline':
                    payload = {
                        'contact_name': payload['contact_name'],
                        'phone': payload['phone'],
                        'call_id': payload['external_id'],
                        'notes': payload['notes'],
                    }
            result = process_channel_inbound(conn, channel, payload)
            sqlite_commit(conn, label='crm_inbound_test')
            return jsonify({'success': True, **result})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/inbound-hub/logs', methods=['GET'])
    @login_required
    def api_crm_inbound_logs():
        from Services.crm_inbound import list_inbound_logs
        conn = _conn()
        try:
            return jsonify({
                'items': list_inbound_logs(
                    conn,
                    limit=int(request.args.get('limit') or 50),
                    channel=request.args.get('channel') or None,
                ),
            })
        finally:
            conn.close()

    def _check_inbound_token(conn) -> bool:
        from Services import crm_ops
        expected = crm_ops.ensure_inbound_token(conn)
        got = (
            request.headers.get('X-CRM-Token')
            or request.args.get('token')
            or (request.get_json(silent=True) or {}).get('token')
            or ''
        ).strip()
        return bool(got and got == expected)

    @app.route('/api/crm/inbound/<channel>', methods=['GET', 'POST'])
    @app.route('/<tenant_id>/api/crm/inbound/<channel>', methods=['GET', 'POST'])
    def api_crm_inbound_channel(channel, tenant_id=None):
        """Webhook theo kênh: facebook, zalo, google, tiktok, whatsapp, viber, hotline, website.

        GET (Facebook/Meta): hub.mode=subscribe + hub.verify_token + hub.challenge
        POST: JSON nền tảng hoặc Make — Header X-CRM-Token
        """
        from Services.crm_inbound import (
            get_channel_verify_token,
            process_channel_inbound,
        )
        from Services.crm_inbound_adapters import CHANNEL_SLUGS

        slug = (channel or '').strip().lower()
        if slug not in CHANNEL_SLUGS:
            return jsonify({'error': f'Kênh không hỗ trợ: {channel}'}), 404

        conn = _conn()
        try:
            if request.method == 'GET':
                # Meta webhook verification
                mode = request.args.get('hub.mode') or request.args.get('hub_mode')
                verify = (
                    request.args.get('hub.verify_token')
                    or request.args.get('hub_verify_token')
                    or request.args.get('verify_token')
                    or ''
                ).strip()
                challenge = (
                    request.args.get('hub.challenge')
                    or request.args.get('hub_challenge')
                    or request.args.get('challenge')
                    or ''
                )
                expected = get_channel_verify_token(conn, slug)
                if mode == 'subscribe' and verify and verify == expected:
                    return challenge, 200, {'Content-Type': 'text/plain'}
                # health ping
                if request.args.get('ping') == '1':
                    return jsonify({'ok': True, 'channel': slug})
                return jsonify({'error': 'Forbidden'}), 403

            if not _check_inbound_token(conn):
                return jsonify({'error': 'Unauthorized'}), 401
            data = request.get_json(silent=True) or {}
            if not data:
                data = {k: request.form.get(k) for k in request.form}
            result = process_channel_inbound(conn, slug, data)
            sqlite_commit(conn, label=f'crm_inbound_{slug}')
            return jsonify({'success': True, **{
                k: result[k] for k in ('id', 'owner', 'source', 'contact_name', 'channel', 'deduped')
                if k in result
            }})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/crm/inbound-lead', methods=['POST'])
    @app.route('/<tenant_id>/api/crm/inbound-lead', methods=['POST'])
    def api_crm_inbound_lead(tenant_id=None):
        """Webhook công khai chung — xác thực Token; tự nhận diện source."""
        from Services.crm_inbound import mark_phase_for_source, log_inbound
        from Services import crm_ops
        from Services.crm_inbound import normalize_inbound_payload
        conn = _conn()
        try:
            if not _check_inbound_token(conn):
                return jsonify({'error': 'Unauthorized'}), 401
            data = request.get_json(silent=True) or {}
            if not data:
                data = {k: request.form.get(k) for k in request.form}
            result = crm_ops.create_inbound_lead(conn, data, auto_assign=True)
            norm = normalize_inbound_payload(data)
            log_inbound(
                conn,
                channel='generic',
                status='ok',
                lead_id=result.get('id'),
                owner=result.get('owner'),
                source=result.get('source') or norm.get('source'),
                external_id=norm.get('external_id'),
                contact_name=result.get('contact_name'),
                phone=norm.get('phone'),
                payload=data,
            )
            mark_phase_for_source(conn, result.get('source') or '')
            sqlite_commit(conn, label='crm_inbound')
            return jsonify({'success': True, **result})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()
