# -*- coding: utf-8 -*-
"""Compliance scanner + persistence."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def scan_compliance(conn: sqlite3.Connection, *, year: int | None = None) -> dict[str, Any]:
    from Services.hrm.schema import ensure_hrm_schema
    from Services.hrm.contracts import probation_overdue, contracts_expiring_soon
    from db_utils import sqlite_commit

    ensure_hrm_schema(conn)
    year = int(year or datetime.now().year)
    events: list[dict] = []

    for c in probation_overdue(conn):
        events.append({
            'event_type': 'probation_overdue',
            'severity': 'danger',
            'employee_id': c.get('employee_id'),
            'title': f"Thử việc quá hạn: {c.get('employee_name') or c.get('employee_id')}",
            'detail': f"Hết hạn thử việc {c.get('probation_end_date')}",
            'period_key': str(year),
        })
    for c in contracts_expiring_soon(conn, days=30):
        events.append({
            'event_type': 'contract_expiring',
            'severity': 'warning',
            'employee_id': c.get('employee_id'),
            'title': f"HĐ sắp hết hạn: {c.get('employee_name') or c.get('employee_id')}",
            'detail': f"Ngày hết hạn {c.get('end_date')}",
            'period_key': str(year),
        })

    info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(info) if info else {}
    ot_m = _f(info.get('ot_cap_month_hours') or 40)
    ot_y = _f(info.get('ot_cap_year_hours') or 200)

    # OT hours from labor_sheets overtime lines in year
    try:
        rows = conn.execute(
            """
            SELECT s.employee_id, e.fullname,
                   SUM(CASE WHEN CAST(substr(s.sheet_date,6,2) AS INT) = CAST(strftime('%m','now') AS INT)
                            THEN COALESCE(l.quantity,0) ELSE 0 END) AS month_hrs,
                   SUM(COALESCE(l.quantity,0)) AS year_hrs
            FROM sme_labor_sheet_lines l
            JOIN sme_labor_sheets s ON s.id = l.sheet_id
            LEFT JOIN employees e ON e.id = s.employee_id
            WHERE s.sheet_type = 'overtime'
              AND substr(COALESCE(s.sheet_date, ''), 1, 4) = ?
              AND COALESCE(s.status,'') NOT IN ('void','cancelled')
            GROUP BY s.employee_id
            """,
            (str(year),),
        ).fetchall()
    except sqlite3.Error:
        # sheet may store employee on lines
        try:
            rows = conn.execute(
                """
                SELECT l.employee_id, e.fullname,
                       SUM(CASE WHEN CAST(substr(s.sheet_date,6,2) AS INT) = CAST(strftime('%m','now') AS INT)
                                THEN COALESCE(l.quantity,0) ELSE 0 END) AS month_hrs,
                       SUM(COALESCE(l.quantity,0)) AS year_hrs
                FROM sme_labor_sheet_lines l
                JOIN sme_labor_sheets s ON s.id = l.sheet_id
                LEFT JOIN employees e ON e.id = l.employee_id
                WHERE s.sheet_type = 'overtime'
                  AND substr(COALESCE(s.sheet_date, ''), 1, 4) = ?
                  AND COALESCE(s.status,'') NOT IN ('void','cancelled')
                GROUP BY l.employee_id
                """,
                (str(year),),
            ).fetchall()
        except sqlite3.Error:
            rows = []

    for r in rows:
        rd = dict(r)
        emp = rd.get('employee_id')
        name = rd.get('fullname') or emp
        mh, yh = _f(rd.get('month_hrs')), _f(rd.get('year_hrs'))
        if mh > ot_m:
            events.append({
                'event_type': 'ot_month_cap',
                'severity': 'warning',
                'employee_id': emp,
                'title': f'Tăng ca vượt trần tháng: {name}',
                'detail': f'{mh:.1f}h > {ot_m:.0f}h/tháng',
                'period_key': datetime.now().strftime('%Y-%m'),
            })
        if yh > ot_y:
            events.append({
                'event_type': 'ot_year_cap',
                'severity': 'danger',
                'employee_id': emp,
                'title': f'Tăng ca vượt trần năm: {name}',
                'detail': f'{yh:.1f}h > {ot_y:.0f}h/năm',
                'period_key': str(year),
            })

    # LTT vùng vs base salary
    region = (info.get('salary_region') or '').strip()
    region_min = 0.0
    if region:
        try:
            rr = conn.execute(
                'SELECT min_salary FROM salary_regions WHERE region_name=?', (region,)
            ).fetchone()
            if rr:
                region_min = _f(rr['min_salary'] if hasattr(rr, 'keys') else rr[0])
        except sqlite3.Error:
            pass
    if region_min > 0:
        try:
            low = conn.execute(
                """
                SELECT id, fullname, base_salary FROM employees
                WHERE COALESCE(status,1)=1 AND COALESCE(base_salary,0) > 0
                  AND COALESCE(base_salary,0) < ?
                """,
                (region_min,),
            ).fetchall()
            for e in low:
                ed = dict(e)
                events.append({
                    'event_type': 'below_min_wage',
                    'severity': 'warning',
                    'employee_id': ed.get('id'),
                    'title': f"Lương dưới LTT vùng: {ed.get('fullname')}",
                    'detail': f"{ed.get('base_salary')} < LTT {region_min}",
                    'period_key': region,
                })
        except sqlite3.Error:
            pass

    # replace open events of same types for period (simple)
    conn.execute(
        "DELETE FROM hrm_compliance_events WHERE COALESCE(is_resolved,0)=0"
    )
    for ev in events:
        conn.execute(
            """
            INSERT INTO hrm_compliance_events
              (event_type, severity, employee_id, title, detail, period_key)
            VALUES (?,?,?,?,?,?)
            """,
            (
                ev['event_type'], ev['severity'], ev.get('employee_id'),
                ev['title'], ev.get('detail'), ev.get('period_key'),
            ),
        )
    try:
        sqlite_commit(conn, label='hrm_compliance')
    except Exception:
        pass
    return {'count': len(events), 'events': events}


def list_open_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM hrm_compliance_events
        WHERE COALESCE(is_resolved,0)=0
        ORDER BY CASE severity WHEN 'danger' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]
