# -*- coding: utf-8 -*-
"""Hợp đồng lao động (HĐLĐ) — thử việc / XĐTH / KXĐTH / học việc + phụ cấp pháp lý."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

CONTRACT_TYPES = {
    'probation': 'Thử việc',
    'definite': 'Xác định thời hạn',
    'indefinite': 'Không xác định thời hạn',
    'apprentice': 'Học việc',
}

CONTRACT_NO_PREFIX = 'HĐLĐ-'
CONTRACT_NO_DIGITS = 6
_CONTRACT_NO_RE = re.compile(r'^(?:HĐLĐ|HDLD)-(\d+)$', re.IGNORECASE)

ALLOWANCE_FIELDS = (
    'allowance_position',
    'allowance_responsibility',
    'allowance_seniority',
    'allowance_lunch',
    'allowance_uniform',
    'allowance_phone',
)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _row(r) -> dict:
    return dict(r) if r and hasattr(r, 'keys') else {}


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_birth_date(raw: str | None) -> str | None:
    s = (raw or '').strip()
    if not s:
        return None
    return s[:10] if len(s) >= 10 else None


def _normalize_id_card(raw: str | None) -> str | None:
    from Services.chu_ho_helpers import normalize_id_card
    s = normalize_id_card(raw)
    return s or None


def _assert_id_card_available(
    conn: sqlite3.Connection,
    id_card: str | None,
    *,
    emp_id: int | None = None,
) -> None:
    """Kiểm tra CCCD chưa dùng bởi NV khác (tránh UNIQUE constraint khi sửa nhập sai)."""
    if not id_card:
        return
    if emp_id:
        row = conn.execute(
            """
            SELECT id, fullname, employee_code FROM employees
            WHERE id_card = ? AND id != ?
            LIMIT 1
            """,
            (id_card, int(emp_id)),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id, fullname, employee_code FROM employees
            WHERE id_card = ?
            LIMIT 1
            """,
            (id_card,),
        ).fetchone()
    if not row:
        return
    r = _row(row)
    name = (r.get('fullname') or '').strip() or f'#{r.get("id")}'
    code = (r.get('employee_code') or '').strip()
    hint = f' (mã {code})' if code else ''
    raise ValueError(
        f'Số CCCD {id_card} đã được dùng cho nhân viên {name}{hint}. '
        'Vui lòng nhập CCCD khác hoặc chọn đúng nhân viên.'
    )


def _employee_snapshot_fields(data: dict) -> tuple[str | None, str | None]:
    id_card = _normalize_id_card(
        data.get('id_card') or data.get('employee_id_card'),
    )
    birth_date = _normalize_birth_date(
        data.get('birth_date') or data.get('employee_birth_date'),
    )
    return id_card, birth_date


def normalize_contract_no(raw: str | None) -> str | None:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    m = _CONTRACT_NO_RE.match(text)
    if m:
        return f'{CONTRACT_NO_PREFIX}{int(m.group(1)):0{CONTRACT_NO_DIGITS}d}'
    return text


def _max_contract_no_sequence(conn: sqlite3.Connection) -> int:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    rows = conn.execute(
        """
        SELECT contract_no FROM hrm_employment_contracts
        WHERE contract_no IS NOT NULL AND TRIM(contract_no) != ''
        """
    ).fetchall()
    max_n = 0
    for row in rows:
        code = row['contract_no'] if hasattr(row, 'keys') else row[0]
        m = _CONTRACT_NO_RE.match(str(code or '').strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def next_contract_no(conn: sqlite3.Connection) -> str:
    """Sinh số HĐLĐ tiếp theo (HĐLĐ-000001, HĐLĐ-000002, …)."""
    seq = _max_contract_no_sequence(conn)
    while True:
        seq += 1
        no = f'{CONTRACT_NO_PREFIX}{seq:0{CONTRACT_NO_DIGITS}d}'
        row = conn.execute(
            'SELECT 1 FROM hrm_employment_contracts WHERE contract_no = ? LIMIT 1',
            (no,),
        ).fetchone()
        if not row:
            return no


def _resolve_contract_no(conn: sqlite3.Connection, data: dict, *, contract_id: int | None) -> str:
    raw = (data.get('contract_no') or '').strip()
    if raw:
        return normalize_contract_no(raw) or raw
    if contract_id:
        row = conn.execute(
            'SELECT contract_no FROM hrm_employment_contracts WHERE id = ?',
            (int(contract_id),),
        ).fetchone()
        existing = (row['contract_no'] if row and hasattr(row, 'keys') else (row[0] if row else '')) or ''
        if str(existing).strip():
            return str(existing).strip()
    return next_contract_no(conn)


def list_contracts(conn: sqlite3.Connection, *, status: str | None = 'active') -> list[dict]:
    from Services.hrm.schema import ensure_hrm_schema
    from Services.hrm.legal_payroll import ensure_legal_payroll_columns
    ensure_hrm_schema(conn)
    ensure_legal_payroll_columns(conn)
    sql = """
        SELECT c.*, e.fullname AS employee_name, e.employee_code,
               e.user_id AS ess_user_id, e.ess_enabled,
               u.username AS ess_username, u.full_name AS ess_user_fullname,
               COALESCE(NULLIF(TRIM(c.employee_id_card), ''), e.id_card) AS employee_id_card,
               e.phone AS employee_phone, e.address AS employee_address,
               COALESCE(NULLIF(TRIM(c.employee_birth_date), ''), e.birth_date) AS employee_birth_date
        FROM hrm_employment_contracts c
        LEFT JOIN employees e ON e.id = c.employee_id
        LEFT JOIN users u ON u.id = e.user_id
    """
    params: list[Any] = []
    if status:
        sql += ' WHERE c.status = ?'
        params.append(status)
    sql += ' ORDER BY c.start_date DESC, c.id DESC'
    return [_row(r) for r in conn.execute(sql, params).fetchall()]


def get_contract(conn: sqlite3.Connection, contract_id: int) -> dict | None:
    from Services.hrm.schema import ensure_hrm_schema
    from Services.hrm.legal_payroll import ensure_legal_payroll_columns
    ensure_hrm_schema(conn)
    ensure_legal_payroll_columns(conn)
    row = conn.execute(
        """
        SELECT c.*, e.fullname AS employee_name, e.employee_code,
               COALESCE(NULLIF(TRIM(c.employee_id_card), ''), e.id_card) AS employee_id_card,
               e.phone AS employee_phone, e.address AS employee_address,
               COALESCE(NULLIF(TRIM(c.employee_birth_date), ''), e.birth_date) AS employee_birth_date,
               e.join_date AS employee_join_date
        FROM hrm_employment_contracts c
        LEFT JOIN employees e ON e.id = c.employee_id
        WHERE c.id = ?
        """,
        (int(contract_id),),
    ).fetchone()
    return _row(row) if row else None


def upsert_contract(conn: sqlite3.Connection, data: dict, *, commit: bool = True) -> dict:
    if commit:
        from db_utils import sqlite_run_write
        return sqlite_run_write(
            conn,
            lambda c: upsert_contract(c, data, commit=False),
            label='hrm_contract',
        )

    from Services.hrm.schema import ensure_hrm_schema
    from Services.hrm.legal_payroll import ensure_legal_payroll_columns
    from Services.hrm.employee_codes import resolve_employee_for_contract, ensure_employee_code
    ensure_hrm_schema(conn)
    ensure_legal_payroll_columns(conn)
    ctype = (data.get('contract_type') or '').strip()
    if ctype not in CONTRACT_TYPES:
        raise ValueError('Loại HĐ không hợp lệ')
    emp_id, new_code = resolve_employee_for_contract(conn, data)
    data = {**data, 'employee_id': emp_id}
    start = (data.get('start_date') or '').strip()
    if not start:
        raise ValueError('Thiếu ngày bắt đầu')
    cid = data.get('id')
    contract_no = _resolve_contract_no(conn, data, contract_id=int(cid) if cid else None)
    base = _f(data.get('base_salary'))
    ins = _f(data.get('insurance_salary'))
    if ins <= 0:
        # Mặc định lương đóng BH = lương chính + PC chịu BH
        ins = base + sum(_f(data.get(k)) for k in (
            'allowance_position', 'allowance_responsibility', 'allowance_seniority',
        ))
    allowances = {k: _f(data.get(k)) for k in ALLOWANCE_FIELDS}
    from Services.hrm.work_calendar import contract_work_defaults
    wdef = contract_work_defaults(conn, start)
    work_days_raw = data.get('work_days_month')
    if work_days_raw in (None, '') or _f(work_days_raw) <= 0:
        work_days_month = float(wdef['work_days_month'])
    else:
        work_days_month = _f(work_days_raw)
    work_hours_day = _f(data.get('work_hours_day') or wdef['work_hours_day'] or 8)
    work_start_time = (wdef['work_start_time'] or '08:00').strip()
    work_lunch_start = (wdef['work_lunch_start'] or '12:00').strip()
    work_lunch_end = (wdef['work_lunch_end'] or '13:00').strip()
    work_end_time = (wdef['work_end_time'] or '17:00').strip()
    work_weekdays_str = (
        (data.get('work_weekdays_str') or wdef.get('work_weekdays_str') or '').strip() or None
    )
    snap_id_card, snap_birth_date = _employee_snapshot_fields(data)
    fields = (
        emp_id,
        contract_no,
        ctype,
        start,
        (data.get('end_date') or '').strip() or None,
        (data.get('probation_end_date') or '').strip() or None,
        base,
        ins,
        (data.get('position') or '').strip() or None,
        (data.get('department') or '').strip() or None,
        (data.get('status') or 'active').strip(),
        (data.get('notes') or '').strip() or None,
        allowances['allowance_position'],
        allowances['allowance_responsibility'],
        allowances['allowance_seniority'],
        allowances['allowance_lunch'],
        allowances['allowance_uniform'],
        allowances['allowance_phone'],
        work_days_month,
        work_hours_day,
        work_start_time,
        work_lunch_start,
        work_lunch_end,
        work_end_time,
        work_weekdays_str,
        snap_birth_date,
        snap_id_card,
        _now(),
    )
    if cid:
        conn.execute(
            """
            UPDATE hrm_employment_contracts SET
              employee_id=?, contract_no=?, contract_type=?, start_date=?, end_date=?,
              probation_end_date=?, base_salary=?, insurance_salary=?, position=?,
              department=?, status=?, notes=?,
              allowance_position=?, allowance_responsibility=?, allowance_seniority=?,
              allowance_lunch=?, allowance_uniform=?, allowance_phone=?,
              work_days_month=?, work_hours_day=?,
              work_start_time=?, work_lunch_start=?, work_lunch_end=?, work_end_time=?,
              work_weekdays_str=?, employee_birth_date=?, employee_id_card=?, updated_at=?
            WHERE id=?
            """,
            fields + (int(cid),),
        )
        out_id = int(cid)
    else:
        cur = conn.execute(
            """
            INSERT INTO hrm_employment_contracts (
              employee_id, contract_no, contract_type, start_date, end_date,
              probation_end_date, base_salary, insurance_salary, position,
              department, status, notes,
              allowance_position, allowance_responsibility, allowance_seniority,
              allowance_lunch, allowance_uniform, allowance_phone,
              work_days_month, work_hours_day,
              work_start_time, work_lunch_start, work_lunch_end, work_end_time,
              work_weekdays_str, employee_birth_date, employee_id_card, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            fields,
        )
        out_id = int(cur.lastrowid)

    # Đồng bộ phụ cấp / lương / hồ sơ NV
    if data.get('sync_employee', True):
        ensure_employee_code(conn, emp_id, commit=False)
        fullname = (data.get('fullname') or data.get('employee_name') or '').strip()
        if not fullname:
            row = conn.execute('SELECT fullname FROM employees WHERE id = ?', (emp_id,)).fetchone()
            fullname = (row['fullname'] if row and hasattr(row, 'keys') else (row[0] if row else '')) or ''
        id_card = snap_id_card
        phone = (data.get('phone') or data.get('employee_phone') or '').strip() or None
        address = (data.get('address') or data.get('employee_address') or '').strip() or None
        birth_date = snap_birth_date
        if fullname:
            _assert_id_card_available(conn, id_card, emp_id=emp_id)
            try:
                conn.execute(
                    """
                    UPDATE employees SET
                      fullname = ?,
                      id_card = ?,
                      phone = ?,
                      address = ?,
                      birth_date = ?,
                      base_salary = ?,
                      insurance_salary = ?,
                      position = COALESCE(NULLIF(?, ''), position),
                      department = COALESCE(NULLIF(?, ''), department),
                      allowance_position = ?,
                      allowance_responsibility = ?,
                      allowance_seniority = ?,
                      allowance_lunch = ?,
                      allowance_uniform = ?,
                      allowance_phone = ?,
                      allowance_fund = ?,
                      allowance_other = ?
                    WHERE id = ?
                    """,
                    (
                        fullname,
                        id_card,
                        phone,
                        address,
                        birth_date,
                        base, ins,
                        (data.get('position') or '').strip(),
                        (data.get('department') or '').strip(),
                        allowances['allowance_position'],
                        allowances['allowance_responsibility'],
                        allowances['allowance_seniority'],
                        allowances['allowance_lunch'],
                        allowances['allowance_uniform'],
                        allowances['allowance_phone'],
                        allowances['allowance_position']
                        + allowances['allowance_responsibility']
                        + allowances['allowance_seniority'],
                        allowances['allowance_lunch']
                        + allowances['allowance_uniform']
                        + allowances['allowance_phone'],
                        emp_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if 'id_card' in str(exc).lower():
                    raise ValueError(
                        f'Số CCCD {id_card or ""} đã tồn tại trên hồ sơ nhân viên khác. '
                        'Vui lòng nhập CCCD khác.'
                    ) from exc
                raise

    out = get_contract(conn, out_id) or {}
    if new_code:
        out['employee_code_assigned'] = new_code
    return out


def delete_contract(conn: sqlite3.Connection, contract_id: int, *, commit: bool = True) -> None:
    if commit:
        from db_utils import sqlite_run_write
        sqlite_run_write(
            conn,
            lambda c: delete_contract(c, contract_id, commit=False),
            label='hrm_contract_delete',
        )
        return

    from Services.hrm.schema import ensure_hrm_schema

    ensure_hrm_schema(conn)
    cur = conn.execute(
        'DELETE FROM hrm_employment_contracts WHERE id = ?',
        (int(contract_id),),
    )
    if cur.rowcount <= 0:
        raise ValueError('Không tìm thấy hợp đồng')


def probation_overdue(conn: sqlite3.Connection, *, as_of: str | None = None) -> list[dict]:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    today = as_of or datetime.now().strftime('%Y-%m-%d')
    rows = conn.execute(
        """
        SELECT c.*, e.fullname AS employee_name
        FROM hrm_employment_contracts c
        LEFT JOIN employees e ON e.id = c.employee_id
        WHERE c.status = 'active'
          AND c.contract_type = 'probation'
          AND c.probation_end_date IS NOT NULL
          AND TRIM(c.probation_end_date) != ''
          AND date(c.probation_end_date) < date(?)
        ORDER BY c.probation_end_date
        """,
        (today,),
    ).fetchall()
    return [_row(r) for r in rows]


def contracts_expiring_soon(conn: sqlite3.Connection, *, days: int = 30) -> list[dict]:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    today = datetime.now().date()
    until = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT c.*, e.fullname AS employee_name
        FROM hrm_employment_contracts c
        LEFT JOIN employees e ON e.id = c.employee_id
        WHERE c.status = 'active'
          AND c.end_date IS NOT NULL AND TRIM(c.end_date) != ''
          AND date(c.end_date) BETWEEN date(?) AND date(?)
        ORDER BY c.end_date
        """,
        (today.isoformat(), until),
    ).fetchall()
    return [_row(r) for r in rows]
