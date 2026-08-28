# -*- coding: utf-8 -*-
"""API lịch làm việc & ngày lễ — dùng chung payroll + HRM."""
from __future__ import annotations

import logging
from datetime import datetime

from flask import jsonify, request

from db_utils import get_db_connection, sqlite_commit

logger = logging.getLogger(__name__)


def work_calendar_api_response(*, bootstrap=None):
    from Services.hrm.work_calendar import (
        ensure_work_calendar_schema,
        get_work_calendar_config,
        list_holidays,
        save_holidays,
        save_work_calendar_config,
        seed_default_holidays,
    )

    conn = get_db_connection()
    try:
        if bootstrap:
            bootstrap()
        else:
            ensure_work_calendar_schema(conn)
        year = request.args.get('year', type=int) or datetime.now().year
        if request.method == 'GET':
            cfg = get_work_calendar_config(conn)
            holidays = list_holidays(conn, year)
            if not holidays:
                seed_default_holidays(conn, year, commit=True)
                holidays = list_holidays(conn, year)
            return jsonify({
                'success': True,
                'data': cfg,
                'holidays': holidays,
                'year': year,
                'paid_holiday_count': len(holidays),
            })
        data = request.get_json(silent=True) or {}
        year = int(data.get('year') or request.args.get('year') or datetime.now().year)
        work_days = data.get('work_weekdays')
        if work_days is None and data.get('work_weekdays_str'):
            work_days = data.get('work_weekdays_str')
        calendar_fields = (
            'work_weekdays', 'work_weekdays_str', 'hours_per_day',
            'work_start', 'lunch_start', 'lunch_end', 'work_end',
            'mult_normal', 'mult_weekend', 'mult_sat', 'mult_holiday',
        )
        has_calendar_update = any(data.get(k) is not None for k in calendar_fields)
        if has_calendar_update:
            cfg = save_work_calendar_config(
                conn,
                work_weekdays=work_days,
                hours_per_day=data.get('hours_per_day'),
                work_start=data.get('work_start'),
                lunch_start=data.get('lunch_start'),
                lunch_end=data.get('lunch_end'),
                work_end=data.get('work_end'),
                mult_normal=data.get('mult_normal'),
                mult_weekend=data.get('mult_weekend'),
                mult_sat=data.get('mult_sat'),
                mult_holiday=data.get('mult_holiday'),
                commit=False,
            )
        else:
            cfg = get_work_calendar_config(conn)
        if data.get('holidays') is not None:
            save_holidays(
                conn,
                data.get('holidays') or [],
                year=year,
                replace_year=bool(data.get('replace_holidays')),
                commit=False,
            )
        elif data.get('seed_holidays'):
            seed_default_holidays(conn, year, commit=False)
        sqlite_commit(conn, label='work_calendar_api')
        holidays = list_holidays(conn, year)
        if not holidays:
            seed_default_holidays(conn, year, commit=False)
            sqlite_commit(conn, label='work_calendar_seed_fallback')
            holidays = list_holidays(conn, year)
        return jsonify({
            'success': True,
            'data': cfg,
            'holidays': holidays,
            'paid_holiday_count': len(holidays),
            'message': 'Đã lưu lịch làm việc & ngày lễ',
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        conn.rollback()
        logger.exception('work_calendar_api')
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


def register_work_calendar_routes(app, *, login_required, require_sme_regime, bootstrap=None):
    """Đăng ký API lịch làm việc (2 URL tương thích payroll + HRM)."""
    boot = bootstrap

    def _handler():
        return work_calendar_api_response(bootstrap=boot)

    _handler.__name__ = 'api_work_calendar_shared'
    view = login_required(require_sme_regime(_handler))

    app.add_url_rule(
        '/api/sme/payroll/work-calendar',
        endpoint='api_sme_payroll_work_calendar',
        view_func=view,
        methods=['GET', 'POST'],
    )
    app.add_url_rule(
        '/api/hrm/work-calendar',
        endpoint='api_hrm_work_calendar',
        view_func=view,
        methods=['GET', 'POST'],
    )
