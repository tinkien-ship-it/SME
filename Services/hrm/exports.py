# -*- coding: utf-8 -*-
"""Xuất BHXH / OT sheet helpers + device adapters stub."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Any


def export_bhxh_csv(conn: sqlite3.Connection, month: int, year: int) -> str:
    """CSV đơn giản để import đối chiếu BHXH (không thay thế file chuẩn BHXH điện tử)."""
    from Services.hrm.legal_payroll import ensure_legal_payroll_columns

    ensure_legal_payroll_columns(conn, commit=False)
    from Services.employee_payroll_helpers import resolve_salary_detail_table
    sd_table = resolve_salary_detail_table(conn)
    rows = conn.execute(
        f"""
        SELECT e.id, e.fullname, e.id_card, e.base_salary, e.insurance_salary,
               s.time_salary, s.bhxh, s.bhyt, s.bhtn, s.insurance_salary_base,
               s.employer_bhxh, s.employer_bhyt, s.employer_bhtn
        FROM {sd_table} s
        LEFT JOIN employees e ON e.id = s.employee_id
        WHERE s.month=? AND s.year=?
        ORDER BY e.fullname COLLATE NOCASE
        """,
        (int(month), int(year)),
    ).fetchall()

    def _employer_fallback(d: dict) -> tuple[float, float, float]:
        if d.get('employer_bhxh') or d.get('employer_bhyt') or d.get('employer_bhtn'):
            return (
                float(d.get('employer_bhxh') or 0),
                float(d.get('employer_bhyt') or 0),
                float(d.get('employer_bhtn') or 0),
            )
        try:
            from Services.sme.payroll import _employer_parts_for_record
            parts = _employer_parts_for_record(conn, {
                'time_salary': d.get('time_salary'),
                'base_salary': d.get('insurance_salary') or d.get('base_salary'),
                'fullname': d.get('fullname'),
            })
            return float(parts['bhxh']), float(parts['bhyt']), float(parts['bhtn'])
        except Exception:
            return 0.0, 0.0, 0.0

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        'employee_id', 'fullname', 'id_card', 'insurance_salary', 'time_salary',
        'bhxh_nld', 'bhyt_nld', 'bhtn_nld', 'bhxh_dn', 'bhyt_dn', 'bhtn_dn',
        'period',
    ])
    period = f'{month:02d}/{year}'
    for r in rows:
        d = dict(r)
        eb, ey, et = _employer_fallback(d)
        w.writerow([
            d.get('id'), d.get('fullname'), d.get('id_card'),
            d.get('insurance_salary') or d.get('base_salary'),
            d.get('time_salary'),
            d.get('bhxh'), d.get('bhyt'), d.get('bhtn'),
            eb or '', ey or '', et or '',
            period,
        ])
    return buf.getvalue()


def parse_device_attlog(brand: str, raw: str) -> list[dict[str, Any]]:
    """Adapter đa hãng — ZKTeco đã có sẵn; Hikvision/Ronald Jack parse tối thiểu."""
    brand = (brand or 'zkteco').strip().lower()
    lines = []
    if brand in ('zkteco', 'zk'):
        from Services.attendance_helpers import parse_zkteco_attlog_line
        for line in (raw or '').splitlines():
            p = parse_zkteco_attlog_line(line)
            if p:
                lines.append(p)
        return lines
    # Hikvision / Ronald Jack: CSV user_id,datetime[,inout]
    for line in (raw or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.replace(';', ',').split(',')]
        if len(parts) < 2:
            continue
        lines.append({
            'device_user_id': parts[0],
            'punch_time': parts[1].replace('T', ' ')[:19],
            'punch_type': int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
            'brand': brand,
        })
    return lines


def emit_webhook(conn: sqlite3.Connection, event: str, payload: dict,
                 *, target_url: str | None = None) -> dict:
    from Services.hrm.schema import ensure_hrm_schema
    from db_utils import sqlite_commit
    import urllib.request

    ensure_hrm_schema(conn)
    url = (target_url or '').strip()
    if not url:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='hrm_webhook_url' LIMIT 1"
        ).fetchone() if _table_has(conn, 'app_settings') else None
        url = (row[0] if row else '') or ''
    body = json.dumps({'event': event, 'data': payload}, ensure_ascii=False).encode('utf-8')
    status = 0
    if url:
        try:
            req = urllib.request.Request(
                url, data=body, headers={'Content-Type': 'application/json'}, method='POST',
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                status = int(getattr(resp, 'status', 0) or 0)
        except Exception:
            status = 0
    conn.execute(
        """
        INSERT INTO hrm_webhook_logs (event, payload, target_url, status_code)
        VALUES (?,?,?,?)
        """,
        (event, body.decode('utf-8')[:4000], url or None, status),
    )
    try:
        sqlite_commit(conn, label='hrm_webhook')
    except Exception:
        pass
    return {'event': event, 'status_code': status, 'url': url}


def _table_has(conn, name: str) -> bool:
    try:
        r = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
        ).fetchone()
        return bool(r)
    except Exception:
        return False
