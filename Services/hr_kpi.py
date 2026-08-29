# -*- coding: utf-8 -*-
"""Thiết lập KPI theo bộ phận / nhân viên (Nhân sự & tiền lương)."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from db_utils import sqlite_commit
from Services.employee_payroll_helpers import (
    department_label,
    list_department_options,
    normalize_department,
)

_SCHEMA_FLAG = 'hr_kpi_schema_v1'

_DEFAULT_KPIS = (
    ('SALES_REV', 'Doanh số bán hàng', 'VNĐ', 'higher', 'Doanh thu / doanh số thực hiện trong kỳ', 10),
    ('TASK_DONE', 'Tỷ lệ hoàn thành công việc', '%', 'higher', 'Khối lượng công việc đạt hạn', 20),
    ('ATTEND', 'Chấm công đúng giờ', '%', 'higher', 'Tỷ lệ ngày đi làm đúng giờ', 30),
    ('CSAT', 'Mức độ hài lòng khách hàng', 'điểm', 'higher', 'Điểm khảo sát / phản hồi', 40),
    ('COST_CTRL', 'Kiểm soát chi phí', 'VNĐ', 'lower', 'Chi phí phát sinh so với ngân sách', 50),
)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_hr_kpi_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    from db_utils import sqlite_is_ready, sqlite_mark_ready

    if sqlite_is_ready(conn, _SCHEMA_FLAG):
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hr_kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            unit TEXT DEFAULT '',
            direction TEXT DEFAULT 'higher',
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hr_kpi_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_id INTEGER NOT NULL,
            scope TEXT NOT NULL,
            department_code TEXT,
            employee_id INTEGER,
            period_year INTEGER NOT NULL,
            period_month INTEGER,
            target_value REAL NOT NULL DEFAULT 0,
            weight REAL NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_hr_kpi_targets
        ON hr_kpi_targets (
            kpi_id, scope,
            COALESCE(department_code, ''),
            COALESCE(employee_id, 0),
            period_year,
            COALESCE(period_month, 0)
        )
        """
    )

    count = conn.execute('SELECT COUNT(*) FROM hr_kpi_definitions').fetchone()[0]
    if not count:
        ts = _now()
        for code, name, unit, direction, desc, sort_order in _DEFAULT_KPIS:
            conn.execute(
                """
                INSERT INTO hr_kpi_definitions
                    (code, name, description, unit, direction, is_active, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (code, name, desc, unit, direction, sort_order, ts, ts),
            )

    sqlite_mark_ready(conn, _SCHEMA_FLAG)
    # Schema + seed cần commit để không mất khi connection đóng
    try:
        sqlite_commit(conn, label='hr_kpi_schema')
    except Exception:
        if commit:
            raise



def _slug_code(raw: str) -> str:
    text = str(raw or '').strip().upper()
    text = re.sub(r'[^A-Z0-9_]+', '_', text)
    text = re.sub(r'_+', '_', text).strip('_')
    return text[:40] or 'KPI'


def list_kpis(conn: sqlite3.Connection, *, active_only: bool = False) -> list[dict[str, Any]]:
    ensure_hr_kpi_schema(conn)
    sql = """
        SELECT id, code, name, description, unit, direction, is_active, sort_order,
               created_at, updated_at
        FROM hr_kpi_definitions
    """
    if active_only:
        sql += ' WHERE COALESCE(is_active, 1) = 1'
    sql += ' ORDER BY sort_order, id'
    return [dict(r) for r in conn.execute(sql).fetchall()]


def upsert_kpi(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    ensure_hr_kpi_schema(conn)
    kpi_id = data.get('id')
    name = str(data.get('name') or '').strip()
    if not name:
        raise ValueError('Tên KPI bắt buộc')
    code = _slug_code(data.get('code') or name)
    unit = str(data.get('unit') or '').strip()[:40]
    direction = str(data.get('direction') or 'higher').strip().lower()
    if direction not in ('higher', 'lower'):
        direction = 'higher'
    description = str(data.get('description') or '').strip()
    is_active = 1 if data.get('is_active', True) in (True, 1, '1', 'true', 'True') else 0
    try:
        sort_order = int(data.get('sort_order') or 0)
    except (TypeError, ValueError):
        sort_order = 0
    ts = _now()

    if kpi_id:
        row = conn.execute(
            'SELECT id, code FROM hr_kpi_definitions WHERE id = ?', (int(kpi_id),)
        ).fetchone()
        if not row:
            raise ValueError('Không tìm thấy KPI')
        if data.get('code'):
            code = _slug_code(data.get('code'))
        else:
            code = row['code'] if isinstance(row, sqlite3.Row) else row[1]
        dup = conn.execute(
            'SELECT id FROM hr_kpi_definitions WHERE code = ? AND id <> ?',
            (code, int(kpi_id)),
        ).fetchone()
        if dup:
            raise ValueError(f'Mã KPI đã tồn tại: {code}')
        conn.execute(
            """
            UPDATE hr_kpi_definitions
            SET code = ?, name = ?, description = ?, unit = ?, direction = ?,
                is_active = ?, sort_order = ?, updated_at = ?
            WHERE id = ?
            """,
            (code, name, description, unit, direction, is_active, sort_order, ts, int(kpi_id)),
        )
        out_id = int(kpi_id)
    else:
        dup = conn.execute(
            'SELECT id FROM hr_kpi_definitions WHERE code = ?', (code,)
        ).fetchone()
        if dup:
            raise ValueError(f'Mã KPI đã tồn tại: {code}')
        cur = conn.execute(
            """
            INSERT INTO hr_kpi_definitions
                (code, name, description, unit, direction, is_active, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (code, name, description, unit, direction, is_active, sort_order, ts, ts),
        )
        out_id = int(cur.lastrowid)

    sqlite_commit(conn, label='hr_kpi_upsert')
    row = conn.execute(
        """
        SELECT id, code, name, description, unit, direction, is_active, sort_order,
               created_at, updated_at
        FROM hr_kpi_definitions WHERE id = ?
        """,
        (out_id,),
    ).fetchone()
    return dict(row)


def delete_kpi(conn: sqlite3.Connection, kpi_id: int, *, soft: bool = True) -> None:
    ensure_hr_kpi_schema(conn)
    kid = int(kpi_id)
    row = conn.execute('SELECT id FROM hr_kpi_definitions WHERE id = ?', (kid,)).fetchone()
    if not row:
        raise ValueError('Không tìm thấy KPI')
    if soft:
        conn.execute(
            'UPDATE hr_kpi_definitions SET is_active = 0, updated_at = ? WHERE id = ?',
            (_now(), kid),
        )
    else:
        conn.execute('DELETE FROM hr_kpi_targets WHERE kpi_id = ?', (kid,))
        conn.execute('DELETE FROM hr_kpi_definitions WHERE id = ?', (kid,))
    sqlite_commit(conn, label='hr_kpi_delete')


def _norm_period(year, month) -> tuple[int, int | None]:
    try:
        y = int(year)
    except (TypeError, ValueError) as exc:
        raise ValueError('Năm không hợp lệ') from exc
    if y < 2000 or y > 2100:
        raise ValueError('Năm không hợp lệ')
    if month in (None, '', 0, '0'):
        return y, None
    try:
        m = int(month)
    except (TypeError, ValueError) as exc:
        raise ValueError('Tháng không hợp lệ') from exc
    if m < 1 or m > 12:
        raise ValueError('Tháng không hợp lệ')
    return y, m


def list_dept_targets(
    conn: sqlite3.Connection,
    *,
    year: int,
    month: int | None = None,
    department: str | None = None,
) -> list[dict[str, Any]]:
    ensure_hr_kpi_schema(conn)
    y, m = _norm_period(year, month)
    sql = """
        SELECT t.id, t.kpi_id, t.scope, t.department_code, t.employee_id,
               t.period_year, t.period_month, t.target_value, t.weight, t.notes,
               k.code AS kpi_code, k.name AS kpi_name, k.unit, k.direction, k.is_active
        FROM hr_kpi_targets t
        JOIN hr_kpi_definitions k ON k.id = t.kpi_id
        WHERE t.scope = 'department'
          AND t.period_year = ?
          AND IFNULL(t.period_month, 0) = ?
    """
    params: list[Any] = [y, m or 0]
    if department:
        sql += ' AND t.department_code = ?'
        params.append(normalize_department(department))
    sql += ' ORDER BY t.department_code, k.sort_order, k.id'
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r['department_label'] = department_label(r.get('department_code'))
    return rows


def list_employee_targets(
    conn: sqlite3.Connection,
    *,
    year: int,
    month: int | None = None,
    department: str | None = None,
    employee_id: int | None = None,
) -> list[dict[str, Any]]:
    ensure_hr_kpi_schema(conn)
    y, m = _norm_period(year, month)
    sql = """
        SELECT t.id, t.kpi_id, t.scope, t.department_code, t.employee_id,
               t.period_year, t.period_month, t.target_value, t.weight, t.notes,
               k.code AS kpi_code, k.name AS kpi_name, k.unit, k.direction, k.is_active,
               e.fullname AS employee_name, e.position AS employee_position,
               COALESCE(e.department, 'ADMIN') AS emp_department
        FROM hr_kpi_targets t
        JOIN hr_kpi_definitions k ON k.id = t.kpi_id
        LEFT JOIN employees e ON e.id = t.employee_id
        WHERE t.scope = 'employee'
          AND t.period_year = ?
          AND IFNULL(t.period_month, 0) = ?
    """
    params: list[Any] = [y, m or 0]
    if department:
        sql += " AND COALESCE(NULLIF(TRIM(t.department_code), ''), COALESCE(e.department, 'ADMIN')) = ?"
        params.append(normalize_department(department))
    if employee_id:
        sql += ' AND t.employee_id = ?'
        params.append(int(employee_id))
    sql += ' ORDER BY e.fullname, k.sort_order, k.id'
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        dept = r.get('department_code') or r.get('emp_department') or 'ADMIN'
        r['department_code'] = normalize_department(dept)
        r['department_label'] = department_label(dept)
    return rows


def save_targets(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Lưu hàng loạt chỉ tiêu."""
    ensure_hr_kpi_schema(conn)
    scope = str(payload.get('scope') or '').strip().lower()
    if scope not in ('department', 'employee'):
        raise ValueError('scope phải là department hoặc employee')
    y, m = _norm_period(payload.get('year'), payload.get('month'))
    items = payload.get('items') or []
    if not isinstance(items, list):
        raise ValueError('items phải là danh sách')

    ts = _now()
    saved = 0
    for raw in items:
        try:
            kpi_id = int(raw.get('kpi_id'))
        except (TypeError, ValueError) as exc:
            raise ValueError('kpi_id không hợp lệ') from exc
        kpi = conn.execute(
            'SELECT id FROM hr_kpi_definitions WHERE id = ?', (kpi_id,)
        ).fetchone()
        if not kpi:
            raise ValueError(f'KPI #{kpi_id} không tồn tại')

        try:
            target_value = float(raw.get('target_value') or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError('Chỉ tiêu không hợp lệ') from exc
        try:
            weight = float(raw.get('weight') or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError('Trọng số không hợp lệ') from exc
        notes = str(raw.get('notes') or '').strip()

        dept = None
        emp_id = None
        if scope == 'department':
            dept = normalize_department(raw.get('department_code'))
        else:
            try:
                emp_id = int(raw.get('employee_id'))
            except (TypeError, ValueError) as exc:
                raise ValueError('employee_id bắt buộc') from exc
            emp = conn.execute(
                "SELECT id, COALESCE(department, 'ADMIN') AS department FROM employees WHERE id = ?",
                (emp_id,),
            ).fetchone()
            if not emp:
                raise ValueError(f'Nhân viên #{emp_id} không tồn tại')
            dept = normalize_department(raw.get('department_code') or emp['department'])

        clear = target_value == 0 and weight == 0 and not notes
        existing = conn.execute(
            """
            SELECT id FROM hr_kpi_targets
            WHERE kpi_id = ? AND scope = ?
              AND IFNULL(department_code, '') = IFNULL(?, '')
              AND IFNULL(employee_id, 0) = IFNULL(?, 0)
              AND period_year = ?
              AND IFNULL(period_month, 0) = ?
            """,
            (kpi_id, scope, dept, emp_id, y, m or 0),
        ).fetchone()

        if clear:
            if existing:
                conn.execute('DELETE FROM hr_kpi_targets WHERE id = ?', (existing[0],))
            continue

        if existing:
            conn.execute(
                """
                UPDATE hr_kpi_targets
                SET target_value = ?, weight = ?, notes = ?,
                    department_code = ?, employee_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (target_value, weight, notes, dept, emp_id, ts, existing[0]),
            )
        else:
            conn.execute(
                """
                INSERT INTO hr_kpi_targets
                    (kpi_id, scope, department_code, employee_id, period_year, period_month,
                     target_value, weight, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kpi_id, scope, dept, emp_id, y, m, target_value, weight, notes, ts, ts),
            )
        saved += 1

    sqlite_commit(conn, label='hr_kpi_save_targets')
    return {'saved': saved, 'year': y, 'month': m, 'scope': scope}


def list_employees_for_kpi(
    conn: sqlite3.Connection,
    *,
    department: str | None = None,
) -> list[dict[str, Any]]:
    from Services.employee_payroll_helpers import ensure_employee_allowance_columns

    ensure_employee_allowance_columns(conn, commit=False)
    sql = """
        SELECT id, fullname, position, status,
               COALESCE(department, 'ADMIN') AS department
        FROM employees
        WHERE CAST(COALESCE(status, '1') AS TEXT) IN ('1', 'true', 'True')
    """
    params: list[Any] = []
    if department:
        sql += " AND COALESCE(NULLIF(TRIM(department), ''), 'ADMIN') = ?"
        params.append(normalize_department(department))
    sql += ' ORDER BY fullname'
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r['department'] = normalize_department(r.get('department'))
        r['department_label'] = department_label(r['department'])
    return rows


def kpi_setup_bundle(
    conn: sqlite3.Connection,
    *,
    year: int,
    month: int | None = None,
    department: str | None = None,
) -> dict[str, Any]:
    ensure_hr_kpi_schema(conn)
    y, m = _norm_period(year, month)
    dept = normalize_department(department) if department else None
    return {
        'departments': list_department_options(),
        'kpis': list_kpis(conn, active_only=False),
        'employees': list_employees_for_kpi(conn, department=dept),
        'dept_targets': list_dept_targets(conn, year=y, month=m, department=dept),
        'employee_targets': list_employee_targets(conn, year=y, month=m, department=dept),
        'period': {'year': y, 'month': m},
    }
