# -*- coding: utf-8 -*-
"""Bridge KPI doanh số HR (SALES_REV) → gauge CRM."""
from __future__ import annotations

import sqlite3
from typing import Any


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _row(r) -> dict:
    if r is None:
        return {}
    if isinstance(r, dict):
        return dict(r)
    if hasattr(r, 'keys'):
        return dict(r)
    return {}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def _period_parts(period_type: str, period_key: str) -> tuple[int, list[int]]:
    """Trả (year, [months]) — quý = 3 tháng; tháng = 1 tháng."""
    period_type = (period_type or 'month').strip()
    key = (period_key or '').strip()
    if period_type == 'quarter' and '-Q' in key:
        y_s, q_s = key.split('-Q', 1)
        y = int(y_s)
        q = int(q_s)
        start = (q - 1) * 3 + 1
        return y, [start, start + 1, start + 2]
    # YYYY-MM
    parts = key.split('-')
    y = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return y, [m] if m else []


def _sales_rev_kpi_id(conn: sqlite3.Connection) -> int | None:
    if not _table_exists(conn, 'hr_kpi_definitions'):
        return None
    try:
        from Services.hr_kpi import ensure_hr_kpi_schema
        ensure_hr_kpi_schema(conn, commit=False)
    except Exception:
        pass
    row = conn.execute(
        """
        SELECT id FROM hr_kpi_definitions
        WHERE UPPER(code) = 'SALES_REV' AND COALESCE(is_active, 1) = 1
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return int(_row(row)['id'])


def _sum_targets(
    conn: sqlite3.Connection,
    *,
    kpi_id: int,
    year: int,
    months: list[int],
    scope: str,
    employee_id: int | None = None,
) -> float:
    if not _table_exists(conn, 'hr_kpi_targets'):
        return 0.0
    total = 0.0
    for m in months or [0]:
        sql = """
            SELECT COALESCE(SUM(target_value), 0) AS s
            FROM hr_kpi_targets
            WHERE kpi_id = ? AND scope = ?
              AND period_year = ?
              AND IFNULL(period_month, 0) = ?
        """
        params: list[Any] = [kpi_id, scope, year, int(m or 0)]
        if scope == 'employee' and employee_id:
            sql += ' AND employee_id = ?'
            params.append(int(employee_id))
        total += _f(_row(conn.execute(sql, params).fetchone()).get('s'))
    # nếu không có target theo tháng, thử chỉ tiêu cả năm (period_month=0)
    if total <= 0 and months and any(m > 0 for m in months):
        sql = """
            SELECT COALESCE(SUM(target_value), 0) AS s
            FROM hr_kpi_targets
            WHERE kpi_id = ? AND scope = ?
              AND period_year = ?
              AND IFNULL(period_month, 0) = 0
        """
        params = [kpi_id, scope, year]
        if scope == 'employee' and employee_id:
            sql += ' AND employee_id = ?'
            params.append(int(employee_id))
        annual = _f(_row(conn.execute(sql, params).fetchone()).get('s'))
        if annual > 0:
            # phân bổ theo số tháng của kỳ
            n = len([m for m in months if m > 0]) or 1
            total = annual * (n / 12.0)
    return total


def _find_employee_id_for_owner(conn: sqlite3.Connection, owner: str) -> int | None:
    """Map CRM owner (username) → employees.id qua fullname / users."""
    owner = (owner or '').strip()
    if not owner or not _table_exists(conn, 'employees'):
        return None
    # 1) users.username → full_name → employees.fullname
    try:
        if _table_exists(conn, 'users'):
            u = conn.execute(
                """
                SELECT full_name, username FROM users
                WHERE LOWER(TRIM(username)) = LOWER(TRIM(?))
                LIMIT 1
                """,
                (owner,),
            ).fetchone()
            ud = _row(u)
            for cand in (ud.get('full_name'), ud.get('username'), owner):
                if not cand:
                    continue
                e = conn.execute(
                    """
                    SELECT id FROM employees
                    WHERE LOWER(TRIM(COALESCE(fullname, ''))) = LOWER(TRIM(?))
                    LIMIT 1
                    """,
                    (cand,),
                ).fetchone()
                if e:
                    return int(_row(e)['id'])
    except sqlite3.Error:
        pass
    # 2) match trực tiếp owner với tên NV
    try:
        e = conn.execute(
            """
            SELECT id FROM employees
            WHERE LOWER(TRIM(COALESCE(fullname, ''))) = LOWER(TRIM(?))
            LIMIT 1
            """,
            (owner,),
        ).fetchone()
        if e:
            return int(_row(e)['id'])
    except sqlite3.Error:
        pass
    return None


def resolve_hr_sales_rev_target(
    conn: sqlite3.Connection,
    *,
    period_type: str = 'month',
    period_key: str,
    owner: str | None = None,
) -> dict[str, Any]:
    """
    Lấy mục tiêu SALES_REV từ hr_kpi_targets.
    - Có owner: target employee (map username → NV), không có thì 0 + note.
    - Không owner: tổng target department; nếu trống thì tổng employee.
    """
    kpi_id = _sales_rev_kpi_id(conn)
    if not kpi_id:
        return {
            'target': 0.0,
            'found': False,
            'source': None,
            'detail': 'Chưa có KPI SALES_REV trong Thiết lập KPI nhân sự',
        }
    try:
        year, months = _period_parts(period_type, period_key)
    except (TypeError, ValueError):
        return {
            'target': 0.0,
            'found': False,
            'source': None,
            'detail': f'Kỳ không hợp lệ: {period_key}',
        }

    owner_key = (owner or '').strip()
    if owner_key:
        emp_id = _find_employee_id_for_owner(conn, owner_key)
        if not emp_id:
            return {
                'target': 0.0,
                'found': False,
                'source': None,
                'detail': f'Không map được owner «{owner_key}» sang nhân viên HR',
            }
        amt = _sum_targets(
            conn, kpi_id=kpi_id, year=year, months=months,
            scope='employee', employee_id=emp_id,
        )
        return {
            'target': round(amt, 0),
            'found': amt > 0,
            'source': 'hr_kpi_employee' if amt > 0 else None,
            'detail': f'SALES_REV NV#{emp_id}' if amt > 0 else 'NV chưa có chỉ tiêu SALES_REV kỳ này',
            'employee_id': emp_id,
            'kpi_id': kpi_id,
        }

    # Công ty: ưu tiên tổng phòng ban, rồi tổng NV
    dept_sum = _sum_targets(
        conn, kpi_id=kpi_id, year=year, months=months, scope='department',
    )
    if dept_sum > 0:
        return {
            'target': round(dept_sum, 0),
            'found': True,
            'source': 'hr_kpi_department',
            'detail': 'Tổng SALES_REV các phòng ban (HR KPI)',
            'kpi_id': kpi_id,
        }
    emp_sum = _sum_targets(
        conn, kpi_id=kpi_id, year=year, months=months, scope='employee',
    )
    if emp_sum > 0:
        return {
            'target': round(emp_sum, 0),
            'found': True,
            'source': 'hr_kpi_employee_sum',
            'detail': 'Tổng SALES_REV tất cả nhân viên (HR KPI)',
            'kpi_id': kpi_id,
        }
    return {
        'target': 0.0,
        'found': False,
        'source': None,
        'detail': 'Chưa đặt chỉ tiêu SALES_REV cho kỳ này trên HR KPI',
        'kpi_id': kpi_id,
    }


def prefer_hr_kpi(conn: sqlite3.Connection) -> bool:
    try:
        from Services.crm_ops import get_setting
        return (get_setting(conn, 'kpi_prefer_hr', '1') or '1').strip() not in (
            '0', 'false', 'no', 'off',
        )
    except Exception:
        return True
