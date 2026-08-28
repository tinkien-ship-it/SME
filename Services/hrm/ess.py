# -*- coding: utf-8 -*-
"""ESS helpers + GPS check-in + employee↔user link."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


def _row(r) -> dict:
    return dict(r) if r and hasattr(r, 'keys') else {}


def find_employee_for_user(
    conn: sqlite3.Connection,
    user_id: int | None,
    username: str | None = None,
) -> dict | None:
    """Tìm NV theo user_id (ESS chỉ dùng liên kết chính thức — xem ess_access)."""
    from Services.hrm.schema import ensure_hrm_schema

    ensure_hrm_schema(conn)
    if user_id:
        r = conn.execute(
            'SELECT * FROM employees WHERE user_id = ? LIMIT 1',
            (int(user_id),),
        ).fetchone()
        if r:
            return _row(r)
    return None


def employee_payslips(
    conn: sqlite3.Connection,
    employee_id: int,
    *,
    limit: int = 12,
) -> list[dict]:
    from Services.employee_payroll_helpers import resolve_salary_detail_table

    sd = resolve_salary_detail_table(conn)
    rows = conn.execute(
        f"""
        SELECT month, year, time_salary, allowance_fund, allowance_other, bonus,
               bhxh, bhyt, bhtn, tncn_tax, total_income, total_deduct, final_amount
        FROM {sd}
        WHERE employee_id = ?
        ORDER BY year DESC, month DESC
        LIMIT ?
        """,
        (int(employee_id), int(limit)),
    ).fetchall()
    return [_row(r) for r in rows]


def create_leave_request(conn: sqlite3.Connection, data: dict, *, commit: bool = True) -> dict:
    if commit:
        from db_utils import sqlite_run_write
        return sqlite_run_write(
            conn,
            lambda c: create_leave_request(c, data, commit=False),
            label='hrm_leave',
        )

    from Services.hrm.schema import ensure_hrm_schema

    ensure_hrm_schema(conn)
    emp = int(data.get('employee_id') or 0)
    if not emp:
        raise ValueError('Thiếu employee_id')
    cur = conn.execute(
        """
        INSERT INTO hrm_leave_requests
          (employee_id, leave_type, start_date, end_date, days, reason, status)
        VALUES (?,?,?,?,?,?, 'pending')
        """,
        (
            emp,
            (data.get('leave_type') or 'annual').strip(),
            (data.get('start_date') or '').strip(),
            (data.get('end_date') or data.get('start_date') or '').strip(),
            float(data.get('days') or 1),
            (data.get('reason') or '').strip() or None,
        ),
    )
    return _row(conn.execute(
        'SELECT * FROM hrm_leave_requests WHERE id=?',
        (cur.lastrowid,),
    ).fetchone())


def list_leave(conn: sqlite3.Connection, employee_id: int | None = None) -> list[dict]:
    from Services.hrm.schema import ensure_hrm_schema

    ensure_hrm_schema(conn)
    if employee_id:
        rows = conn.execute(
            'SELECT * FROM hrm_leave_requests WHERE employee_id=? ORDER BY id DESC',
            (int(employee_id),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT l.*, e.fullname AS employee_name
            FROM hrm_leave_requests l
            LEFT JOIN employees e ON e.id = l.employee_id
            ORDER BY l.id DESC LIMIT 200
            """
        ).fetchall()
    return [_row(r) for r in rows]


def mobile_checkin(conn: sqlite3.Connection, data: dict, *, commit: bool = True) -> dict:
    if commit:
        from db_utils import sqlite_run_write
        return sqlite_run_write(
            conn,
            lambda c: mobile_checkin(c, data, commit=False),
            label='hrm_checkin',
        )

    from Services.hrm.schema import ensure_hrm_schema
    from Services.attendance_helpers import ensure_attendance_schema, upsert_attendance_log

    ensure_hrm_schema(conn)
    ensure_attendance_schema(conn)
    emp = int(data.get('employee_id') or 0)
    if not emp:
        raise ValueError('Thiếu employee_id')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur = conn.execute(
        """
        INSERT INTO hrm_mobile_checkins
          (employee_id, check_type, lat, lng, accuracy, device_info, punched_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            emp,
            (data.get('check_type') or 'in').strip(),
            data.get('lat'),
            data.get('lng'),
            data.get('accuracy'),
            (data.get('device_info') or '')[:200] or None,
            now,
        ),
    )
    try:
        upsert_attendance_log(
            conn,
            {
                'employee_id': emp,
                'device_user_id': f'gps:{emp}',
                'punch_time': now,
                'punch_date': now[:10],
                'punch_type': 1 if (data.get('check_type') or 'in') == 'in' else 0,
                'verify_mode': 'GPS',
            },
            source='mobile_gps',
            device_sn='MOBILE_GPS',
        )
    except Exception:
        pass
    return _row(conn.execute(
        'SELECT * FROM hrm_mobile_checkins WHERE id=?',
        (cur.lastrowid,),
    ).fetchone())
