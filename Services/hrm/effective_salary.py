# -*- coding: utf-8 -*-
"""Effective-dated salary for back-pay."""
from __future__ import annotations

import sqlite3
from typing import Any


def set_effective_salary(conn: sqlite3.Connection, data: dict, *, commit: bool = True) -> dict:
    from Services.hrm.schema import ensure_hrm_schema
    from db_utils import sqlite_commit
    ensure_hrm_schema(conn)
    emp = int(data.get('employee_id') or 0)
    eff = (data.get('effective_from') or '').strip()
    if not emp or not eff:
        raise ValueError('Thiếu employee_id / effective_from')
    # close previous open band
    conn.execute(
        """
        UPDATE hrm_salary_effective
        SET effective_to = date(?, '-1 day')
        WHERE employee_id=? AND (effective_to IS NULL OR TRIM(effective_to)='')
          AND date(effective_from) < date(?)
        """,
        (eff, emp, eff),
    )
    cur = conn.execute(
        """
        INSERT INTO hrm_salary_effective
          (employee_id, effective_from, effective_to, base_salary, insurance_salary,
           allowance_fund, allowance_other, reason)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            emp, eff,
            (data.get('effective_to') or '').strip() or None,
            float(data.get('base_salary') or 0),
            float(data.get('insurance_salary') or 0) or None,
            float(data.get('allowance_fund') or 0),
            float(data.get('allowance_other') or 0),
            (data.get('reason') or '').strip() or None,
        ),
    )
    # sync current employees.base_salary
    conn.execute(
        'UPDATE employees SET base_salary=?, insurance_salary=COALESCE(?, insurance_salary) WHERE id=?',
        (float(data.get('base_salary') or 0), float(data.get('insurance_salary') or 0) or None, emp),
    )
    if commit:
        sqlite_commit(conn, label='hrm_salary_eff')
    return dict(conn.execute(
        'SELECT * FROM hrm_salary_effective WHERE id=?', (cur.lastrowid,)
    ).fetchone())


def salary_as_of(conn: sqlite3.Connection, employee_id: int, as_of: str) -> dict[str, Any] | None:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    row = conn.execute(
        """
        SELECT * FROM hrm_salary_effective
        WHERE employee_id=?
          AND date(effective_from) <= date(?)
          AND (effective_to IS NULL OR TRIM(effective_to)='' OR date(effective_to) >= date(?))
        ORDER BY effective_from DESC LIMIT 1
        """,
        (int(employee_id), as_of, as_of),
    ).fetchone()
    if row:
        return dict(row)
    emp = conn.execute(
        'SELECT id, base_salary, insurance_salary, allowance_fund, allowance_other FROM employees WHERE id=?',
        (int(employee_id),),
    ).fetchone()
    return dict(emp) if emp else None
