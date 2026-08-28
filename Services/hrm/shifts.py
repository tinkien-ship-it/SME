# -*- coding: utf-8 -*-
"""Shift & OT engine — ca đêm, crossing-midnight, hệ số OT theo luật."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any


def _f(v, d=0.0) -> float:
    try:
        return float(v if v is not None and v != '' else d)
    except (TypeError, ValueError):
        return d


def _parse_hm(s: str) -> tuple[int, int]:
    parts = (s or '00:00').strip().split(':')
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def shift_duration_hours(start_time: str, end_time: str, break_minutes: int = 0,
                         crosses_midnight: bool = False) -> float:
    sh, sm = _parse_hm(start_time)
    eh, em = _parse_hm(end_time)
    start = sh * 60 + sm
    end = eh * 60 + em
    if crosses_midnight or end <= start:
        end += 24 * 60
    mins = max(0, end - start - int(break_minutes or 0))
    return round(mins / 60.0, 4)


def night_hours_in_interval(start_dt: datetime, end_dt: datetime) -> float:
    """Giờ nằm trong khung đêm 22:00–06:00."""
    if end_dt <= start_dt:
        return 0.0
    total = 0.0
    cur = start_dt
    while cur < end_dt:
        day = cur.date()
        night1_start = datetime.combine(day, datetime.strptime('22:00', '%H:%M').time())
        night1_end = datetime.combine(day + timedelta(days=1), datetime.strptime('00:00', '%H:%M').time())
        night2_start = datetime.combine(day, datetime.strptime('00:00', '%H:%M').time())
        night2_end = datetime.combine(day, datetime.strptime('06:00', '%H:%M').time())
        for a, b in ((night1_start, night1_end), (night2_start, night2_end)):
            lo = max(cur, a, start_dt)
            hi = min(end_dt, b)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600.0
        cur = datetime.combine(day + timedelta(days=1), datetime.strptime('00:00', '%H:%M').time())
        if cur <= start_dt:
            cur = start_dt + timedelta(hours=1)
    return round(total, 4)


def list_shifts(conn: sqlite3.Connection) -> list[dict]:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    return [dict(r) for r in conn.execute(
        'SELECT * FROM hrm_shifts WHERE COALESCE(is_active,1)=1 ORDER BY code'
    ).fetchall()]


def list_ot_policies(conn: sqlite3.Connection) -> list[dict]:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    return [dict(r) for r in conn.execute(
        'SELECT * FROM hrm_ot_policies WHERE COALESCE(is_active,1)=1 ORDER BY multiplier'
    ).fetchall()]


def hourly_rate(base_salary: float, standard_days: float, hours_per_day: float = 8.0) -> float:
    days = max(_f(standard_days), 1.0)
    return _f(base_salary) / days / max(hours_per_day, 1.0)


def calc_ot_pay(
    *,
    hours: float,
    day_type: str = 'normal',
    is_night: bool = False,
    base_salary: float,
    standard_days: float,
    policies: list[dict] | None = None,
) -> dict[str, float]:
    """
    OT ngày thường 150%, nghỉ 200%, lễ 300%.
    Ca đêm thường +30%; OT ca đêm thêm +20% trên đơn giá ngày tương ứng.
    """
    hrs = max(_f(hours), 0)
    rate = hourly_rate(base_salary, standard_days)
    day_type = (day_type or 'normal').strip().lower()
    mult = {'normal': 1.5, 'weekend': 2.0, 'holiday': 3.0}.get(day_type, 1.5)
    night_extra = 0.3
    ot_night_extra = 0.2
    if policies:
        for p in policies:
            if (p.get('day_type') or '').lower() == day_type:
                mult = _f(p.get('multiplier'), mult)
                night_extra = _f(p.get('night_extra'), night_extra)
                ot_night_extra = _f(p.get('ot_night_extra'), ot_night_extra)
                break
    base_ot = rate * mult * hrs
    night_allow = rate * night_extra * hrs if is_night else 0.0
    ot_night = rate * ot_night_extra * hrs if is_night else 0.0
    # Phần miễn thuế OT = phần vượt lương giờ bình thường
    tax_exempt = rate * max(mult - 1.0, 0) * hrs
    total = base_ot + night_allow + ot_night
    return {
        'hourly_rate': round(rate, 2),
        'multiplier': mult,
        'hours': hrs,
        'ot_amount': round(base_ot),
        'night_allowance': round(night_allow),
        'ot_night_extra_amount': round(ot_night),
        'total': round(total),
        'tax_exempt_ot': round(tax_exempt),
    }


def monthly_hours_from_punches(
    conn: sqlite3.Connection, month: int, year: int
) -> dict[int, dict[str, float]]:
    """
    Ước lượng giờ công từ punch: mỗi ngày MAX-MIN (hỗ trợ crossing nếu >0 và end<start +24h).
    Trả {emp_id: {work_days, work_hours, night_hours}}.
    """
    from Services.attendance_helpers import ensure_attendance_schema
    ensure_attendance_schema(conn)
    start = f'{year:04d}-{month:02d}-01'
    if month == 12:
        end = f'{year + 1:04d}-01-01'
    else:
        end = f'{year:04d}-{month + 1:02d}-01'
    rows = conn.execute(
        """
        SELECT employee_id, punch_date, MIN(punch_time) AS tmin, MAX(punch_time) AS tmax
        FROM attendance_logs
        WHERE punch_date >= ? AND punch_date < ?
          AND employee_id IS NOT NULL
        GROUP BY employee_id, punch_date
        """,
        (start, end),
    ).fetchall()
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        emp = int(r['employee_id'] if hasattr(r, 'keys') else r[0])
        pdate = r['punch_date'] if hasattr(r, 'keys') else r[1]
        tmin = r['tmin'] if hasattr(r, 'keys') else r[2]
        tmax = r['tmax'] if hasattr(r, 'keys') else r[3]
        try:
            d0 = datetime.strptime(f'{pdate} {str(tmin)[-8:]}'[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                d0 = datetime.strptime(f'{pdate} {str(tmin)[:5]}', '%Y-%m-%d %H:%M')
            except ValueError:
                continue
        try:
            d1 = datetime.strptime(f'{pdate} {str(tmax)[-8:]}'[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                d1 = datetime.strptime(f'{pdate} {str(tmax)[:5]}', '%Y-%m-%d %H:%M')
            except ValueError:
                continue
        if d1 <= d0:
            d1 += timedelta(days=1)
        hours = (d1 - d0).total_seconds() / 3600.0
        if hours > 16:
            hours = 8.0  # punch lỗi — fallback 1 ngày
        night = night_hours_in_interval(d0, d1)
        slot = out.setdefault(emp, {'work_days': 0.0, 'work_hours': 0.0, 'night_hours': 0.0})
        slot['work_days'] += 1
        slot['work_hours'] += hours
        slot['night_hours'] += night
    for v in out.values():
        v['work_hours'] = round(v['work_hours'], 2)
        v['night_hours'] = round(v['night_hours'], 2)
    return out


def preview_ot_line(
    conn: sqlite3.Connection,
    *,
    base_salary: float,
    standard_days: float,
    hours: float,
    day_type: str = 'normal',
    is_night: bool = False,
) -> dict[str, Any]:
    policies = list_ot_policies(conn)
    return calc_ot_pay(
        hours=hours,
        day_type=day_type,
        is_night=is_night,
        base_salary=base_salary,
        standard_days=standard_days,
        policies=policies,
    )
