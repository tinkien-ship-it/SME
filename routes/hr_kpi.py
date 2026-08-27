# -*- coding: utf-8 -*-
"""Routes thiết lập KPI nhân sự — dùng chung SME / HKD."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from flask import jsonify, render_template, request

from auth import login_required
from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def register_hr_kpi_routes(app):
    from Services.tenant_profile import require_sme_regime

    @app.route('/kpi_settings')
    @login_required
    def kpi_settings_page():
        return render_template('hr_kpi_settings.html')

    @app.route('/SME_kpi_settings')
    @login_required
    @require_sme_regime
    def SME_kpi_settings():
        return render_template('KeToanSME/kpi_settings.html')

    @app.route('/api/hr/kpi/bundle', methods=['GET'])
    @login_required
    def api_hr_kpi_bundle():
        from Services.hr_kpi import kpi_setup_bundle

        year = request.args.get('year', datetime.now().year, type=int)
        month_raw = request.args.get('month')
        month = None
        if month_raw not in (None, '', '0', 'all'):
            try:
                month = int(month_raw)
            except (TypeError, ValueError):
                month = None
        department = (request.args.get('department') or '').strip() or None

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = kpi_setup_bundle(conn, year=year, month=month, department=department)
            return jsonify({'success': True, 'data': data})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('api_hr_kpi_bundle')
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/hr/kpi/definitions', methods=['GET', 'POST'])
    @login_required
    def api_hr_kpi_definitions():
        from Services.hr_kpi import delete_kpi, list_kpis, upsert_kpi

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            if request.method == 'GET':
                active_only = request.args.get('active_only') in ('1', 'true', 'yes')
                return jsonify({'success': True, 'data': list_kpis(conn, active_only=active_only)})

            data = request.get_json(silent=True) or {}
            action = str(data.get('action') or 'upsert').strip().lower()
            if action == 'delete':
                delete_kpi(conn, int(data.get('id')), soft=data.get('soft', True) is not False)
                return jsonify({'success': True})
            row = upsert_kpi(conn, data)
            return jsonify({'success': True, 'data': row})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('api_hr_kpi_definitions')
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/hr/kpi/targets', methods=['POST'])
    @login_required
    def api_hr_kpi_targets_save():
        from Services.hr_kpi import save_targets

        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            result = save_targets(conn, payload)
            return jsonify({'success': True, 'data': result})
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('api_hr_kpi_targets_save')
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            conn.close()
