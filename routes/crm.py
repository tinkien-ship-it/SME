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


def _conn():
    return get_db_connection()


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
        return render_template('crm/settings.html')

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
        except Exception as e:
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
                crm_svc.delete_lead(conn, lead_id)
                sqlite_commit(conn, label='crm_lead_delete')
                return jsonify({'success': True})
            data = request.get_json() or {}
            crm_svc.upsert_lead(conn, data, lead_id=lead_id)
            sqlite_commit(conn, label='crm_lead_update')
            return jsonify({'success': True, 'id': lead_id})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
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

    @app.route('/api/crm/contracts/<int:cid>', methods=['PUT', 'DELETE'])
    @login_required
    def api_crm_contract_one(cid):
        from Services import crm_ops
        conn = _conn()
        try:
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
                token = crm_ops.ensure_inbound_token(conn)
                sqlite_commit(conn, label='crm_settings_token')
                return jsonify({
                    'inbound_token': token,
                    'assign_owners': crm_ops.get_assign_owners(conn),
                    'inbound_url': '/api/crm/inbound-lead',
                    'kpi_prefer_hr': prefer_hr_kpi(conn),
                    'kpi_bridge': _kpi_bridge_payload(),
                })
            data = request.get_json() or {}
            if 'assign_owners' in data:
                owners = data.get('assign_owners') or []
                if isinstance(owners, str):
                    owners = [x.strip() for x in owners.split(',') if x.strip()]
                crm_ops.set_assign_owners(conn, owners)
            if 'kpi_prefer_hr' in data:
                from Services.crm_ops import set_setting
                val = data.get('kpi_prefer_hr')
                on = str(val).strip().lower() in ('1', 'true', 'yes', 'on')
                set_setting(conn, 'kpi_prefer_hr', '1' if on else '0')
            if data.get('rotate_token'):
                from Services.crm_ops import set_setting
                import secrets
                set_setting(conn, 'inbound_token', secrets.token_urlsafe(24))
            sqlite_commit(conn, label='crm_settings')
            return jsonify({'success': True, **{
                'inbound_token': crm_ops.ensure_inbound_token(conn),
                'assign_owners': crm_ops.get_assign_owners(conn),
                'kpi_prefer_hr': prefer_hr_kpi(conn),
                'kpi_bridge': _kpi_bridge_payload(),
            }})
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

    @app.route('/api/crm/inbound-lead', methods=['POST'])
    def api_crm_inbound_lead():
        """Webhook công khai — xác thực bằng X-CRM-Token hoặc ?token=."""
        from Services import crm_ops
        conn = _conn()
        try:
            expected = crm_ops.ensure_inbound_token(conn)
            got = (
                request.headers.get('X-CRM-Token')
                or request.args.get('token')
                or (request.get_json(silent=True) or {}).get('token')
                or ''
            ).strip()
            if not got or got != expected:
                return jsonify({'error': 'Unauthorized'}), 401
            data = request.get_json(silent=True) or {}
            # also accept form fields
            if not data:
                data = {k: request.form.get(k) for k in request.form}
            result = crm_ops.create_inbound_lead(conn, data, auto_assign=True)
            sqlite_commit(conn, label='crm_inbound')
            return jsonify({'success': True, **result})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()
