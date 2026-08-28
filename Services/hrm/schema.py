# -*- coding: utf-8 -*-
"""HRM schema — contracts, shifts, OT, leave, ESS, compliance, formulas."""
from __future__ import annotations

import sqlite3

_SCHEMA_FLAG = 'hrm_modular_schema_v1'
_ID_CARD_CLEANUP_FLAG = 'hrm_id_card_empty_null_v1'
_ESS_INDEX_FLAG = 'hrm_ess_indexes_v1'

DDL = [
    """
    CREATE TABLE IF NOT EXISTS hrm_employment_contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        contract_no TEXT,
        contract_type TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT,
        probation_end_date TEXT,
        base_salary REAL DEFAULT 0,
        insurance_salary REAL DEFAULT 0,
        position TEXT,
        department TEXT,
        status TEXT DEFAULT 'active',
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_shifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        break_minutes INTEGER DEFAULT 0,
        is_night INTEGER DEFAULT 0,
        crosses_midnight INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_employee_shifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        shift_id INTEGER NOT NULL,
        work_date TEXT NOT NULL,
        UNIQUE(employee_id, work_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_ot_policies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        day_type TEXT NOT NULL,
        multiplier REAL NOT NULL,
        night_extra REAL DEFAULT 0.3,
        ot_night_extra REAL DEFAULT 0.2,
        is_active INTEGER DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_leave_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        leave_type TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        days REAL DEFAULT 1,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        reviewer TEXT,
        reviewed_at TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_attendance_explain (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        work_date TEXT NOT NULL,
        explain_type TEXT,
        note TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_payroll_formulas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        expression TEXT NOT NULL,
        output_field TEXT DEFAULT 'bonus',
        is_active INTEGER DEFAULT 1,
        version INTEGER DEFAULT 1,
        notes TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_salary_effective (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        effective_from TEXT NOT NULL,
        effective_to TEXT,
        base_salary REAL NOT NULL,
        insurance_salary REAL,
        allowance_fund REAL DEFAULT 0,
        allowance_other REAL DEFAULT 0,
        reason TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_compliance_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        severity TEXT DEFAULT 'warning',
        employee_id INTEGER,
        title TEXT NOT NULL,
        detail TEXT,
        period_key TEXT,
        is_resolved INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_mobile_checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        check_type TEXT DEFAULT 'in',
        lat REAL,
        lng REAL,
        accuracy REAL,
        photo_path TEXT,
        device_info TEXT,
        punched_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hrm_webhook_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL,
        payload TEXT,
        target_url TEXT,
        status_code INTEGER,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
]

DEFAULT_OT = (
    ('OT_NORMAL', 'Tăng ca ngày thường', 'normal', 1.5, 0.3, 0.2),
    ('OT_WEEKEND', 'Tăng ca ngày nghỉ hàng tuần', 'weekend', 2.0, 0.3, 0.2),
    ('OT_HOLIDAY', 'Tăng ca ngày lễ/Tết', 'holiday', 3.0, 0.3, 0.2),
)

DEFAULT_SHIFTS = (
    ('CA_SANG', 'Ca sáng', '08:00', '17:00', 60, 0, 0),
    ('CA_CHIEU', 'Ca chiều', '14:00', '22:00', 45, 0, 0),
    ('CA_DEM', 'Ca đêm', '22:00', '06:00', 45, 1, 1),
)

DEFAULT_FORMULAS = (
    (
        'BONUS_KPI',
        'Thưởng KPI cơ bản',
        'Gross_Salary * (KPI_Score / 100) * 0.1',
        'bonus',
    ),
    (
        'ALLOW_ATTEND',
        'Phụ cấp chuyên cần',
        'IF(Actual_Working_Days >= Standard_Days, 500000, 0)',
        'allowance_other',
    ),
)


def _ensure_ess_indexes(conn: sqlite3.Connection) -> None:
    """Index phục vụ ESS / liên kết user_id (Postgres + SQLite)."""
    from db_utils import sqlite_is_ready, sqlite_mark_ready

    if sqlite_is_ready(conn, _ESS_INDEX_FLAG):
        return
    stmts = [
        'CREATE INDEX IF NOT EXISTS idx_employees_user_id ON employees(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_employees_ess_enabled ON employees(ess_enabled)',
        'CREATE INDEX IF NOT EXISTS idx_hrm_leave_employee ON hrm_leave_requests(employee_id)',
        'CREATE INDEX IF NOT EXISTS idx_hrm_checkin_employee ON hrm_mobile_checkins(employee_id)',
        'CREATE INDEX IF NOT EXISTS idx_hrm_checkin_punched ON hrm_mobile_checkins(punched_at)',
    ]
    try:
        from Services.employee_payroll_helpers import resolve_salary_detail_table
        sd = resolve_salary_detail_table(conn)
        stmts.append(
            f'CREATE INDEX IF NOT EXISTS idx_payroll_emp_period '
            f'ON {sd}(employee_id, year, month)'
        )
    except Exception:
        pass
    for sql in stmts:
        try:
            conn.execute(sql)
        except Exception:
            pass
    sqlite_mark_ready(conn, _ESS_INDEX_FLAG)


def ensure_hrm_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    from db_utils import sqlite_is_ready, sqlite_mark_ready, sqlite_commit
    from db.schema_helpers import add_column_if_missing

    if not sqlite_is_ready(conn, _SCHEMA_FLAG):
        for ddl in DDL:
            conn.execute(ddl)
        # seed OT policies
        n = conn.execute('SELECT COUNT(*) FROM hrm_ot_policies').fetchone()[0]
        if not n:
            for row in DEFAULT_OT:
                conn.execute(
                    """
                    INSERT INTO hrm_ot_policies
                      (code, name, day_type, multiplier, night_extra, ot_night_extra)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
        n = conn.execute('SELECT COUNT(*) FROM hrm_shifts').fetchone()[0]
        if not n:
            for row in DEFAULT_SHIFTS:
                conn.execute(
                    """
                    INSERT INTO hrm_shifts
                      (code, name, start_time, end_time, break_minutes, is_night, crosses_midnight)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
        n = conn.execute('SELECT COUNT(*) FROM hrm_payroll_formulas').fetchone()[0]
        if not n:
            for code, name, expr, out in DEFAULT_FORMULAS:
                conn.execute(
                    """
                    INSERT INTO hrm_payroll_formulas (code, name, expression, output_field)
                    VALUES (?, ?, ?, ?)
                    """,
                    (code, name, expr, out),
                )
        sqlite_mark_ready(conn, _SCHEMA_FLAG)

    # employee extras
    add_column_if_missing(conn, 'employees', 'insurance_salary', 'REAL')
    add_column_if_missing(conn, 'employees', 'bank_account', 'TEXT')
    add_column_if_missing(conn, 'employees', 'bank_account_enc', 'TEXT')
    add_column_if_missing(conn, 'employees', 'user_id', 'INTEGER')
    add_column_if_missing(conn, 'employees', 'ess_enabled', 'INTEGER DEFAULT 0')
    add_column_if_missing(conn, 'employees', 'birth_date', 'TEXT')
    add_column_if_missing(conn, 'hrm_employment_contracts', 'employee_birth_date', 'TEXT')
    add_column_if_missing(conn, 'hrm_employment_contracts', 'employee_id_card', 'TEXT')
    if not sqlite_is_ready(conn, _ID_CARD_CLEANUP_FLAG):
        try:
            conn.execute(
                "UPDATE employees SET id_card = NULL "
                "WHERE id_card IS NOT NULL AND TRIM(id_card) = ''"
            )
            sqlite_mark_ready(conn, _ID_CARD_CLEANUP_FLAG)
        except Exception:
            pass

    # business_info insurance caps
    add_column_if_missing(conn, 'business_info', 'bhxh_ref_salary', 'REAL')
    add_column_if_missing(conn, 'business_info', 'bhxh_cap_multiplier', 'REAL DEFAULT 20')
    add_column_if_missing(conn, 'business_info', 'bhtn_cap_multiplier', 'REAL DEFAULT 20')
    add_column_if_missing(conn, 'business_info', 'ot_cap_month_hours', 'REAL DEFAULT 40')
    add_column_if_missing(conn, 'business_info', 'ot_cap_year_hours', 'REAL DEFAULT 200')
    add_column_if_missing(conn, 'business_info', 'hrm_formula_enabled', 'INTEGER DEFAULT 1')

    try:
        from Services.hrm.work_calendar import ensure_work_calendar_schema
        ensure_work_calendar_schema(conn)
    except Exception:
        pass

    _ensure_ess_indexes(conn)

    if commit:
        try:
            sqlite_commit(conn, label='hrm_schema')
        except Exception:
            pass
