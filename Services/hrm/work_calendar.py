# -*- coding: utf-8 -*-
"""Lịch làm việc, ngày nghỉ tuần, ngày lễ & phân loại giờ tăng ca từ chấm công."""
from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

# Python weekday(): T2=0 … T7=5, CN=6
WEEKDAY_LABELS = (
    ('0', 'Thứ 2'),
    ('1', 'Thứ 3'),
    ('2', 'Thứ 4'),
    ('3', 'Thứ 5'),
    ('4', 'Thứ 6'),
    ('5', 'Thứ 7'),
    ('6', 'Chủ nhật'),
)

DEFAULT_WORK_WEEKDAYS = (0, 1, 2, 3, 4)  # T2–T6
SAT_WEEKDAY = 5
SUN_WEEKDAY = 6

# Giờ ca chuẩn công ty (HĐLĐ + lịch làm việc)
COMPANY_STANDARD_WORK_START = '08:00'
COMPANY_STANDARD_LUNCH_START = '12:00'
COMPANY_STANDARD_LUNCH_END = '13:00'
COMPANY_STANDARD_WORK_END = '17:00'
LEGACY_WORK_TIME_BUNDLE = (
    ('07:30', '11:30', '13:30', '17:30'),
    ('7:30', '11:30', '13:30', '17:30'),
)

# 12 ngày nghỉ hưởng nguyên lương/năm (Điều 112 BLĐ 2019 + Ngày Văn hóa VN từ 01/7/2026)
# Nguồn: Thông báo 9441/TB-BNV/2025, Nghị quyết Quốc hội về Ngày Văn hóa Việt Nam
VN_PAID_HOLIDAYS_BY_YEAR: dict[int, list[tuple[str, str]]] = {
    2025: [
        ('2025-01-01', 'Tết Dương lịch'),
        ('2025-01-25', 'Tết Âm lịch (28/12 ÂL)'),
        ('2025-01-26', 'Tết Âm lịch (29/12 ÂL)'),
        ('2025-01-27', 'Tết Âm lịch (Mùng 1)'),
        ('2025-01-28', 'Tết Âm lịch (Mùng 2)'),
        ('2025-01-29', 'Tết Âm lịch (Mùng 3)'),
        ('2025-04-07', 'Giỗ Tổ Hùng Vương'),
        ('2025-04-30', 'Ngày Chiến thắng'),
        ('2025-05-01', 'Quốc tế Lao động'),
        ('2025-09-02', 'Quốc khánh'),
        ('2025-09-03', 'Quốc khánh (nghỉ liền kề)'),
    ],
    2026: [
        ('2026-01-01', 'Tết Dương lịch'),
        ('2026-02-16', 'Tết Âm lịch (29/12 ÂL)'),
        ('2026-02-17', 'Tết Âm lịch (Mùng 1 Bính Ngọ)'),
        ('2026-02-18', 'Tết Âm lịch (Mùng 2)'),
        ('2026-02-19', 'Tết Âm lịch (Mùng 3)'),
        ('2026-02-20', 'Tết Âm lịch (Mùng 4)'),
        ('2026-04-26', 'Giỗ Tổ Hùng Vương (10/3 ÂL)'),
        ('2026-04-30', 'Ngày Chiến thắng'),
        ('2026-05-01', 'Quốc tế Lao động'),
        ('2026-09-01', 'Quốc khánh (nghỉ liền kề)'),
        ('2026-09-02', 'Quốc khánh'),
        ('2026-11-24', 'Ngày Văn hóa Việt Nam'),
    ],
    2027: [
        ('2027-01-01', 'Tết Dương lịch'),
        ('2027-02-05', 'Tết Âm lịch (29/12 ÂL)'),
        ('2027-02-06', 'Tết Âm lịch (Mùng 1)'),
        ('2027-02-07', 'Tết Âm lịch (Mùng 2)'),
        ('2027-02-08', 'Tết Âm lịch (Mùng 3)'),
        ('2027-02-09', 'Tết Âm lịch (Mùng 4)'),
        ('2027-04-16', 'Giỗ Tổ Hùng Vương (10/3 ÂL)'),
        ('2027-04-30', 'Ngày Chiến thắng'),
        ('2027-05-01', 'Quốc tế Lao động'),
        ('2027-09-02', 'Quốc khánh'),
        ('2027-09-03', 'Quốc khánh (nghỉ liền kề)'),
        ('2027-11-24', 'Ngày Văn hóa Việt Nam'),
    ],
}

# Fallback cố định dương lịch (11 ngày — chưa có Ngày Văn hóa VN)
VN_PAID_HOLIDAYS_SOLAR_FALLBACK = (
    (1, 1, 'Tết Dương lịch'),
    (4, 30, 'Ngày Chiến thắng'),
    (5, 1, 'Quốc tế Lao động'),
    (9, 2, 'Quốc khánh'),
)


def _f(v, d=0.0) -> float:
    try:
        if v is None or v == '':
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _parse_int_list(raw: str | None, default: tuple[int, ...]) -> list[int]:
    if not raw or not str(raw).strip():
        return list(default)
    out: list[int] = []
    for part in str(raw).replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if 0 <= n <= 6:
                out.append(n)
        except ValueError:
            continue
    return sorted(set(out)) if out else list(default)


def ensure_work_calendar_schema(conn: sqlite3.Connection) -> None:
    from db.schema_helpers import add_column_if_missing

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hrm_holidays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            holiday_date TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            year INTEGER,
            is_paid INTEGER DEFAULT 1,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    for col, ddl in (
        ('payroll_work_weekdays', "TEXT DEFAULT '0,1,2,3,4'"),
        ('payroll_hours_per_day', 'REAL DEFAULT 8'),
        ('payroll_work_start', "TEXT DEFAULT '08:00'"),
        ('payroll_lunch_start', "TEXT DEFAULT '12:00'"),
        ('payroll_lunch_end', "TEXT DEFAULT '13:00'"),
        ('payroll_work_end', "TEXT DEFAULT '17:00'"),
        ('payroll_mult_normal', 'REAL DEFAULT 1.5'),
        ('payroll_mult_weekend', 'REAL DEFAULT 2.0'),
        ('payroll_mult_sat', 'REAL DEFAULT 1.5'),
        ('payroll_mult_holiday', 'REAL DEFAULT 3.0'),
    ):
        add_column_if_missing(conn, 'business_info', col, ddl)
    try:
        _migrate_legacy_work_times(conn)
    except sqlite3.OperationalError:
        pass  # DB đang bị process khác giữ — chuẩn hóa khi đọc


_LEGACY_WORK_TIMES_FLAG = 'work_calendar_legacy_times_v1'


def _normalize_work_times(
    work_start: str | None,
    lunch_start: str | None,
    lunch_end: str | None,
    work_end: str | None,
) -> tuple[str, str, str, str]:
    """Trả về giờ ca chuẩn công ty; ghi đè bộ 07:30 cũ."""
    ws = (work_start or '').strip()
    ls = (lunch_start or '').strip()
    le = (lunch_end or '').strip()
    we = (work_end or '').strip()
    if (ws, ls, le, we) in LEGACY_WORK_TIME_BUNDLE or not ws:
        return (
            COMPANY_STANDARD_WORK_START,
            COMPANY_STANDARD_LUNCH_START,
            COMPANY_STANDARD_LUNCH_END,
            COMPANY_STANDARD_WORK_END,
        )
    return (
        ws or COMPANY_STANDARD_WORK_START,
        ls or COMPANY_STANDARD_LUNCH_START,
        le or COMPANY_STANDARD_LUNCH_END,
        we or COMPANY_STANDARD_WORK_END,
    )


def _migrate_legacy_work_times(conn: sqlite3.Connection) -> None:
    """Nâng giờ ca cũ (07:30–17:30) lên chuẩn công ty 08:00–17:00."""
    from db_utils import sqlite_is_ready, sqlite_mark_ready

    if sqlite_is_ready(conn, _LEGACY_WORK_TIMES_FLAG):
        return
    row = conn.execute(
        """
        SELECT payroll_work_start, payroll_lunch_start, payroll_lunch_end, payroll_work_end
        FROM business_info LIMIT 1
        """
    ).fetchone()
    if row:
        ws = (row['payroll_work_start'] or '').strip()
        ls = (row['payroll_lunch_start'] or '').strip()
        le = (row['payroll_lunch_end'] or '').strip()
        we = (row['payroll_work_end'] or '').strip()
        cur = (ws, ls, le, we)
        if cur in LEGACY_WORK_TIME_BUNDLE or not ws:
            conn.execute(
                """
                UPDATE business_info SET
                  payroll_work_start = ?,
                  payroll_lunch_start = ?,
                  payroll_lunch_end = ?,
                  payroll_work_end = ?
                """,
                (
                    COMPANY_STANDARD_WORK_START,
                    COMPANY_STANDARD_LUNCH_START,
                    COMPANY_STANDARD_LUNCH_END,
                    COMPANY_STANDARD_WORK_END,
                ),
            )
    try:
        conn.execute('SELECT 1 FROM hrm_employment_contracts LIMIT 1')
    except sqlite3.Error:
        return
    for legacy in LEGACY_WORK_TIME_BUNDLE:
        conn.execute(
            """
            UPDATE hrm_employment_contracts SET
              work_start_time = ?,
              work_lunch_start = ?,
              work_lunch_end = ?,
              work_end_time = ?
            WHERE work_start_time = ? AND work_lunch_start = ? AND work_lunch_end = ? AND work_end_time = ?
            """,
            (
                COMPANY_STANDARD_WORK_START,
                COMPANY_STANDARD_LUNCH_START,
                COMPANY_STANDARD_LUNCH_END,
                COMPANY_STANDARD_WORK_END,
                *legacy,
            ),
        )
    sqlite_mark_ready(conn, _LEGACY_WORK_TIMES_FLAG)


def get_work_calendar_config(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_work_calendar_schema(conn)
    row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(row) if row else {}
    work_days = _parse_int_list(info.get('payroll_work_weekdays'), DEFAULT_WORK_WEEKDAYS)
    rest_days = [d for d in range(7) if d not in work_days]
    wd_labels = [label for wid, label in WEEKDAY_LABELS if int(wid) in work_days]
    ws, ls, le, we = _normalize_work_times(
        info.get('payroll_work_start'),
        info.get('payroll_lunch_start'),
        info.get('payroll_lunch_end'),
        info.get('payroll_work_end'),
    )
    return {
        'work_weekdays': work_days,
        'rest_weekdays': rest_days,
        'work_weekdays_str': ','.join(str(d) for d in work_days),
        'work_weekdays_labels': wd_labels,
        'work_weekdays_display': ', '.join(wd_labels) if wd_labels else '—',
        'hours_per_day': _f(info.get('payroll_hours_per_day'), 8.0),
        'work_start': ws,
        'lunch_start': ls,
        'lunch_end': le,
        'work_end': we,
        'mult_normal': _f(info.get('payroll_mult_normal'), 1.5),
        'mult_weekend': _f(info.get('payroll_mult_weekend'), 2.0),
        'mult_sat': _f(info.get('payroll_mult_sat'), 1.5),
        'mult_holiday': _f(info.get('payroll_mult_holiday'), 3.0),
        'weekday_labels': [{'id': k, 'label': v} for k, v in WEEKDAY_LABELS],
    }


def save_work_calendar_config(
    conn: sqlite3.Connection,
    *,
    work_weekdays: list[int] | str | None = None,
    hours_per_day: float | None = None,
    work_start: str | None = None,
    lunch_start: str | None = None,
    lunch_end: str | None = None,
    work_end: str | None = None,
    mult_normal: float | None = None,
    mult_weekend: float | None = None,
    mult_sat: float | None = None,
    mult_holiday: float | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    from db_utils import sqlite_commit

    ensure_work_calendar_schema(conn)
    row = conn.execute('SELECT id FROM business_info LIMIT 1').fetchone()
    if not row:
        raise ValueError('Chưa có business_info')
    cur = get_work_calendar_config(conn)
    if work_weekdays is not None:
        if isinstance(work_weekdays, str):
            days_str = work_weekdays
        else:
            days_str = ','.join(str(int(d)) for d in sorted(set(work_weekdays)))
    else:
        days_str = cur['work_weekdays_str']
    conn.execute(
        """
        UPDATE business_info SET
            payroll_work_weekdays = ?,
            payroll_hours_per_day = ?,
            payroll_work_start = ?,
            payroll_lunch_start = ?,
            payroll_lunch_end = ?,
            payroll_work_end = ?,
            payroll_mult_normal = ?,
            payroll_mult_weekend = ?,
            payroll_mult_sat = ?,
            payroll_mult_holiday = ?
        """,
        (
            days_str,
            _f(hours_per_day, cur['hours_per_day']),
            (work_start or cur.get('work_start') or '08:00').strip(),
            (lunch_start or cur.get('lunch_start') or '12:00').strip(),
            (lunch_end or cur.get('lunch_end') or '13:00').strip(),
            (work_end or cur.get('work_end') or '17:00').strip(),
            _f(mult_normal, cur['mult_normal']),
            _f(mult_weekend, cur['mult_weekend']),
            _f(mult_sat, cur['mult_sat']),
            _f(mult_holiday, cur['mult_holiday']),
        ),
    )
    if commit:
        sqlite_commit(conn, label='work_calendar_config')
    return get_work_calendar_config(conn)


def get_vn_paid_holidays(year: int) -> list[dict[str, Any]]:
    """Danh sách ngày nghỉ lễ hưởng nguyên lương theo năm (BLĐ Điều 112)."""
    year = int(year)
    if year in VN_PAID_HOLIDAYS_BY_YEAR:
        return [
            {'holiday_date': ds, 'name': name, 'year': year, 'is_paid': 1}
            for ds, name in VN_PAID_HOLIDAYS_BY_YEAR[year]
        ]
    out: list[dict[str, Any]] = []
    for month, day, name in VN_PAID_HOLIDAYS_SOLAR_FALLBACK:
        ds = f'{year:04d}-{month:02d}-{day:02d}'
        out.append({'holiday_date': ds, 'name': name, 'year': year, 'is_paid': 1})
    # Quốc khánh 2 ngày: thêm ngày liền kề 01/09
    out.append({
        'holiday_date': f'{year:04d}-09-01',
        'name': 'Quốc khánh (nghỉ liền kề)',
        'year': year,
        'is_paid': 1,
    })
    if year >= 2026:
        out.append({
            'holiday_date': f'{year:04d}-11-24',
            'name': 'Ngày Văn hóa Việt Nam',
            'year': year,
            'is_paid': 1,
        })
    return out


def seed_default_holidays(conn: sqlite3.Connection, year: int, *, commit: bool = False) -> int:
    """Nạp ngày lễ hưởng lương chuẩn VN — INSERT OR REPLACE theo năm."""
    ensure_work_calendar_schema(conn)
    items = get_vn_paid_holidays(year)
    added = 0
    for it in items:
        try:
            conn.execute(
                """
                INSERT INTO hrm_holidays (holiday_date, name, year, is_paid)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(holiday_date) DO UPDATE SET
                    name = excluded.name,
                    year = excluded.year,
                    is_paid = 1
                """,
                (it['holiday_date'], it['name'], year),
            )
            added += 1
        except sqlite3.Error:
            pass
    if commit:
        from db_utils import sqlite_commit
        sqlite_commit(conn, label='seed_holidays')
    return added


def list_holidays(conn: sqlite3.Connection, year: int | None = None) -> list[dict]:
    ensure_work_calendar_schema(conn)
    if year:
        rows = conn.execute(
            """
            SELECT * FROM hrm_holidays
            WHERE year = ? OR holiday_date LIKE ?
            ORDER BY holiday_date
            """,
            (year, f'{year}-%'),
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM hrm_holidays ORDER BY holiday_date'
        ).fetchall()
    return [dict(r) for r in rows]


def save_holidays(
    conn: sqlite3.Connection,
    items: list[dict],
    *,
    year: int | None = None,
    replace_year: bool = False,
    commit: bool = True,
) -> list[dict]:
    from db_utils import sqlite_commit

    ensure_work_calendar_schema(conn)
    if replace_year and year:
        conn.execute(
            'DELETE FROM hrm_holidays WHERE year = ? OR holiday_date LIKE ?',
            (year, f'{year}-%'),
        )
    for it in items or []:
        ds = (it.get('holiday_date') or it.get('date') or '').strip()[:10]
        name = (it.get('name') or '').strip() or 'Ngày lễ'
        if not ds:
            continue
        yr = year or int(ds[:4])
        conn.execute(
            """
            INSERT INTO hrm_holidays (holiday_date, name, year, is_paid, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(holiday_date) DO UPDATE SET
                name = excluded.name,
                year = excluded.year,
                is_paid = excluded.is_paid,
                notes = excluded.notes
            """,
            (
                ds,
                name,
                yr,
                1 if it.get('is_paid', 1) else 0,
                it.get('notes') or '',
            ),
        )
    if commit:
        sqlite_commit(conn, label='save_holidays')
    return list_holidays(conn, year)


def holiday_dates_set(conn: sqlite3.Connection, year: int) -> set[str]:
    return {h['holiday_date'][:10] for h in list_holidays(conn, year)}


def contract_work_defaults(
    conn: sqlite3.Connection,
    start_date: str | None = None,
) -> dict[str, Any]:
    """Mặc định thời giờ & công chuẩn HĐLĐ — từ lịch làm việc công ty."""
    cfg = get_work_calendar_config(conn)
    month, year = datetime.now().month, datetime.now().year
    if start_date and str(start_date).strip():
        try:
            d = datetime.strptime(str(start_date)[:10], '%Y-%m-%d')
            month, year = d.month, d.year
        except ValueError:
            pass
    std = count_standard_work_days(month, year, cfg['work_weekdays'])
    ws, ls, le, we = cfg['work_start'], cfg['lunch_start'], cfg['lunch_end'], cfg['work_end']
    shift = (
        f'Sáng {ws}–{ls}, nghỉ trưa {ls}–{le}, Chiều {le}–{we}'
    )
    return {
        **cfg,
        'work_days_month': std,
        'work_hours_day': int(_f(cfg['hours_per_day'], 8)),
        'work_start_time': ws,
        'work_lunch_start': ls,
        'work_lunch_end': le,
        'work_end_time': we,
        'work_shift_text': shift,
        'standard_month': month,
        'standard_year': year,
    }


def count_standard_work_days(
    month: int,
    year: int,
    work_weekdays: list[int] | None = None,
    holidays: set[str] | None = None,
) -> int:
    """Số ngày công chuẩn theo lịch DN (gồm ngày lễ hưởng lương trên ngày làm việc)."""
    days = work_weekdays if work_weekdays is not None else list(DEFAULT_WORK_WEEKDAYS)
    n_days = calendar.monthrange(year, month)[1]
    return sum(
        1 for d in range(1, n_days + 1)
        if date(year, month, d).weekday() in days
    )


def paid_holidays_in_month(
    month: int,
    year: int,
    work_weekdays: list[int] | None = None,
    holidays: set[str] | None = None,
) -> list[str]:
    """Ngày lễ hưởng lương rơi vào ngày làm việc của DN trong tháng."""
    days = set(work_weekdays if work_weekdays is not None else DEFAULT_WORK_WEEKDAYS)
    hol = holidays or set()
    n_days = calendar.monthrange(year, month)[1]
    out: list[str] = []
    for d in range(1, n_days + 1):
        dt = date(year, month, d)
        ds = dt.isoformat()
        if ds in hol and dt.weekday() in days:
            out.append(ds)
    return out


def _month_punch_bounds(month: int, year: int) -> tuple[str, str]:
    start = f'{year:04d}-{month:02d}-01'
    if month == 12:
        end = f'{year + 1:04d}-01-01'
    else:
        end = f'{year:04d}-{month + 1:02d}-01'
    return start, end


def _classify_punch_day(
    pdate: str,
    hours: float,
    *,
    work_days: set[int],
    holidays: set[str],
    hpd: float,
) -> tuple[str, float]:
    """Phân loại 1 ngày chấm công → bucket OT + giờ."""
    ds = str(pdate)[:10]
    try:
        wd = datetime.strptime(ds, '%Y-%m-%d').weekday()
    except ValueError:
        return 'skip', 0.0
    if hours <= 0:
        return 'skip', 0.0
    if ds in holidays:
        return 'holiday', hours
    if wd not in work_days:
        if wd == SAT_WEEKDAY:
            return 'weekend_sat', hours
        return 'weekend', hours
    return 'normal', max(0.0, hours - hpd)


def compute_employee_attendance_breakdown(
    conn: sqlite3.Connection,
    employee_id: int,
    month: int,
    year: int,
    *,
    config: dict[str, Any] | None = None,
    holidays: set[str] | None = None,
) -> dict[str, float]:
    """
    Công + tăng ca từ chấm công:
    - Ngày lễ không quét thẻ → cộng công hưởng lương (như ngày thường).
    - Ngày lễ có quét thẻ → toàn bộ giờ tính TC lễ (300%), không cộng công thường.
    """
    from Services.attendance_helpers import ensure_attendance_schema

    ensure_attendance_schema(conn)
    cfg = config or get_work_calendar_config(conn)
    work_days = set(cfg['work_weekdays'])
    hpd = max(_f(cfg.get('hours_per_day'), 8.0), 1.0)
    hol = holidays if holidays is not None else holiday_dates_set(conn, year)
    start, end = _month_punch_bounds(month, year)

    rows = conn.execute(
        """
        SELECT punch_date, MIN(punch_time) AS tmin, MAX(punch_time) AS tmax
        FROM attendance_logs
        WHERE employee_id = ? AND punch_date >= ? AND punch_date < ?
        GROUP BY punch_date
        """,
        (int(employee_id), start, end),
    ).fetchall()

    out: dict[str, float] = {
        'ot_hours': 0.0,
        'ot_hours_weekend_sat': 0.0,
        'ot_hours_weekend': 0.0,
        'ot_hours_holiday': 0.0,
        'regular_work_days': 0.0,
        'paid_holiday_days': 0.0,
        'holiday_work_days': 0.0,
        'actual_working_days': 0.0,
        'has_attendance': 0.0,
    }
    holiday_punch_dates: set[str] = set()

    for r in rows:
        pdate = r['punch_date'] if hasattr(r, 'keys') else r[0]
        tmin = r['tmin'] if hasattr(r, 'keys') else r[1]
        tmax = r['tmax'] if hasattr(r, 'keys') else r[2]
        hours = _hours_from_punch_row(str(pdate), tmin, tmax)
        bucket, val = _classify_punch_day(
            str(pdate), hours, work_days=work_days, holidays=hol, hpd=hpd,
        )
        if bucket == 'skip':
            continue
        out['has_attendance'] = 1.0
        ds = str(pdate)[:10]
        if bucket == 'holiday':
            out['ot_hours_holiday'] += val
            out['holiday_work_days'] += 1
            holiday_punch_dates.add(ds)
        elif bucket == 'weekend_sat':
            out['ot_hours_weekend_sat'] += val
        elif bucket == 'weekend':
            out['ot_hours_weekend'] += val
        else:
            out['ot_hours'] += val
            out['regular_work_days'] += 1

    paid_list = paid_holidays_in_month(month, year, list(work_days), hol)
    out['paid_holiday_days'] = float(
        sum(1 for ds in paid_list if ds not in holiday_punch_dates)
    )
    out['actual_working_days'] = out['regular_work_days'] + out['paid_holiday_days']
    out['work_days'] = out['actual_working_days']

    for k in out:
        if k != 'has_attendance':
            out[k] = round(out[k], 2)
    return out


def attendance_breakdown_map(
    conn: sqlite3.Connection,
    month: int,
    year: int,
    *,
    config: dict[str, Any] | None = None,
) -> dict[int, dict[str, float]]:
    """{employee_id: breakdown} — mọi NV có chấm công trong tháng."""
    from Services.attendance_helpers import ensure_attendance_schema

    ensure_attendance_schema(conn)
    cfg = config or get_work_calendar_config(conn)
    work_days = set(cfg['work_weekdays'])
    hpd = max(_f(cfg.get('hours_per_day'), 8.0), 1.0)
    hol = holiday_dates_set(conn, year)
    start, end = _month_punch_bounds(month, year)

    rows = conn.execute(
        """
        SELECT employee_id, punch_date, MIN(punch_time) AS tmin, MAX(punch_time) AS tmax
        FROM attendance_logs
        WHERE punch_date >= ? AND punch_date < ? AND employee_id IS NOT NULL
        GROUP BY employee_id, punch_date
        """,
        (start, end),
    ).fetchall()

    out: dict[int, dict[str, float]] = {}
    holiday_punch_by_emp: dict[int, set[str]] = {}

    for r in rows:
        emp = int(r['employee_id'] if hasattr(r, 'keys') else r[0])
        pdate = r['punch_date'] if hasattr(r, 'keys') else r[1]
        tmin = r['tmin'] if hasattr(r, 'keys') else r[2]
        tmax = r['tmax'] if hasattr(r, 'keys') else r[3]
        hours = _hours_from_punch_row(str(pdate), tmin, tmax)
        bucket, val = _classify_punch_day(
            str(pdate), hours, work_days=work_days, holidays=hol, hpd=hpd,
        )
        if bucket == 'skip':
            continue
        slot = out.setdefault(
            emp,
            {
                'ot_hours': 0.0,
                'ot_hours_weekend_sat': 0.0,
                'ot_hours_weekend': 0.0,
                'ot_hours_holiday': 0.0,
                'regular_work_days': 0.0,
                'paid_holiday_days': 0.0,
                'holiday_work_days': 0.0,
                'actual_working_days': 0.0,
                'has_attendance': 1.0,
                'work_days': 0.0,
            },
        )
        ds = str(pdate)[:10]
        if bucket == 'holiday':
            slot['ot_hours_holiday'] += val
            slot['holiday_work_days'] += 1
            holiday_punch_by_emp.setdefault(emp, set()).add(ds)
        elif bucket == 'weekend_sat':
            slot['ot_hours_weekend_sat'] += val
        elif bucket == 'weekend':
            slot['ot_hours_weekend'] += val
        else:
            slot['ot_hours'] += val
            slot['regular_work_days'] += 1

    paid_list = paid_holidays_in_month(month, year, list(work_days), hol)
    for emp, slot in out.items():
        punched_hols = holiday_punch_by_emp.get(emp, set())
        slot['paid_holiday_days'] = float(
            sum(1 for ds in paid_list if ds not in punched_hols)
        )
        slot['actual_working_days'] = slot['regular_work_days'] + slot['paid_holiday_days']
        slot['work_days'] = slot['actual_working_days']
        for k in slot:
            if k != 'has_attendance':
                slot[k] = round(slot[k], 2)
    return out


def _hours_from_punch_row(pdate: str, tmin, tmax) -> float:
    try:
        d0 = datetime.strptime(f'{pdate} {str(tmin)[-8:]}'[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            d0 = datetime.strptime(f'{pdate} {str(tmin)[:5]}', '%Y-%m-%d %H:%M')
        except ValueError:
            return 0.0
    try:
        d1 = datetime.strptime(f'{pdate} {str(tmax)[-8:]}'[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            d1 = datetime.strptime(f'{pdate} {str(tmax)[:5]}', '%Y-%m-%d %H:%M')
        except ValueError:
            return 0.0
    if d1 <= d0:
        d1 += timedelta(days=1)
    hours = (d1 - d0).total_seconds() / 3600.0
    if hours > 16:
        hours = 8.0
    return round(max(hours, 0.0), 2)


def aggregate_ot_hours_for_employee(
    conn: sqlite3.Connection,
    employee_id: int,
    month: int,
    year: int,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Phân loại giờ TC + công (gồm nghỉ lễ hưởng lương) từ chấm công."""
    return compute_employee_attendance_breakdown(
        conn, employee_id, month, year, config=config,
    )


def aggregate_ot_hours_map(
    conn: sqlite3.Connection,
    month: int,
    year: int,
    *,
    config: dict[str, Any] | None = None,
) -> dict[int, dict[str, float]]:
    """{employee_id: ot + công breakdown} cho mọi NV có chấm công trong tháng."""
    return attendance_breakdown_map(conn, month, year, config=config)
