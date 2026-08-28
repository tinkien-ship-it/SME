# -*- coding: utf-8 -*-
"""Mã nhân viên thống nhất NV00001 — sinh tự động khi tạo HĐLĐ / hồ sơ NV."""
from __future__ import annotations

import re
import sqlite3

EMPLOYEE_CODE_PREFIX = 'NV'
EMPLOYEE_CODE_DIGITS = 5
_CODE_RE = re.compile(r'^NV(\d+)$', re.IGNORECASE)


def normalize_employee_code(raw: str | None) -> str | None:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip().upper()
    m = _CODE_RE.match(text)
    if m:
        return f'{EMPLOYEE_CODE_PREFIX}{int(m.group(1)):0{EMPLOYEE_CODE_DIGITS}d}'
    if text.startswith(EMPLOYEE_CODE_PREFIX):
        return text
    return text


def _max_nv_sequence(conn: sqlite3.Connection) -> int:
    from Services.employee_payroll_helpers import ensure_employee_allowance_columns
    from Services.hrm.legal_payroll import ensure_legal_payroll_columns

    ensure_employee_allowance_columns(conn, commit=False)
    ensure_legal_payroll_columns(conn, commit=False)
    rows = conn.execute(
        """
        SELECT employee_code FROM employees
        WHERE employee_code IS NOT NULL AND TRIM(employee_code) != ''
        """
    ).fetchall()
    max_n = 0
    for row in rows:
        code = row['employee_code'] if hasattr(row, 'keys') else row[0]
        m = _CODE_RE.match(str(code or '').strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


def next_employee_code(conn: sqlite3.Connection) -> str:
    """Sinh mã NV tiếp theo (NV00001, NV00002, …)."""
    return f'{EMPLOYEE_CODE_PREFIX}{_max_nv_sequence(conn) + 1:0{EMPLOYEE_CODE_DIGITS}d}'


def _code_in_use(conn: sqlite3.Connection, code: str, *, exclude_id: int | None = None) -> bool:
    if exclude_id:
        row = conn.execute(
            'SELECT 1 FROM employees WHERE employee_code = ? AND id != ? LIMIT 1',
            (code, int(exclude_id)),
        ).fetchone()
    else:
        row = conn.execute(
            'SELECT 1 FROM employees WHERE employee_code = ? LIMIT 1',
            (code,),
        ).fetchone()
    return row is not None


def ensure_employee_code(
    conn: sqlite3.Connection,
    employee_id: int,
    *,
    commit: bool = False,
) -> str:
    """Gán mã NV nếu nhân viên chưa có — không ghi đè mã hiện có."""
    from db_utils import sqlite_commit

    emp_id = int(employee_id)
    row = conn.execute(
        'SELECT employee_code FROM employees WHERE id = ?',
        (emp_id,),
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy nhân viên')
    existing = normalize_employee_code(
        row['employee_code'] if hasattr(row, 'keys') else row[0]
    )
    if existing:
        if existing != (row['employee_code'] if hasattr(row, 'keys') else row[0]):
            conn.execute(
                'UPDATE employees SET employee_code = ? WHERE id = ?',
                (existing, emp_id),
            )
        if commit:
            sqlite_commit(conn, label='employee_code_normalize')
        return existing

    seq = _max_nv_sequence(conn)
    while True:
        seq += 1
        code = f'{EMPLOYEE_CODE_PREFIX}{seq:0{EMPLOYEE_CODE_DIGITS}d}'
        if not _code_in_use(conn, code, exclude_id=emp_id):
            break
    conn.execute(
        'UPDATE employees SET employee_code = ? WHERE id = ?',
        (code, emp_id),
    )
    if commit:
        sqlite_commit(conn, label='employee_code_assign')
    return code


def create_employee_for_contract(conn: sqlite3.Connection, data: dict) -> tuple[int, str]:
    """Tạo hồ sơ NV mới kèm mã NV — dùng khi lập HĐLĐ."""
    from datetime import datetime

    from Services.employee_payroll_helpers import ensure_employee_allowance_columns, normalize_department
    from Services.hrm.legal_payroll import ensure_legal_payroll_columns

    ensure_employee_allowance_columns(conn, commit=False)
    ensure_legal_payroll_columns(conn, commit=False)

    fullname = (data.get('fullname') or data.get('employee_name') or '').strip()
    if not fullname:
        raise ValueError('Thiếu họ tên nhân viên')

    code = next_employee_code(conn)
    while _code_in_use(conn, code):
        seq = int(code[len(EMPLOYEE_CODE_PREFIX):]) + 1
        code = f'{EMPLOYEE_CODE_PREFIX}{seq:0{EMPLOYEE_CODE_DIGITS}d}'

    department = normalize_department(data.get('department'))
    join_date = (data.get('start_date') or data.get('join_date') or '')[:10]
    if not join_date:
        join_date = datetime.now().strftime('%Y-%m-%d')
    id_card = (data.get('id_card') or data.get('employee_id_card') or '').strip() or None
    from Services.chu_ho_helpers import normalize_id_card
    id_card = normalize_id_card(id_card) or None
    phone = (data.get('phone') or data.get('employee_phone') or '').strip() or None
    address = (data.get('address') or data.get('employee_address') or '').strip() or None
    birth_date = (data.get('birth_date') or data.get('employee_birth_date') or '').strip()[:10] or None
    position = (data.get('position') or '').strip() or None
    try:
        base = float(data.get('base_salary') or 0)
    except (TypeError, ValueError):
        base = 0.0

    from Services.hrm.contracts import _assert_id_card_available
    _assert_id_card_available(conn, id_card)

    try:
        cur = conn.execute(
            """
            INSERT INTO employees (
                fullname, position, id_card, base_salary, salary_rate,
                phone, address, birth_date, join_date, department, employee_code, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (fullname, position, id_card, base, base or None, phone, address, birth_date, join_date, department, code),
        )
    except sqlite3.IntegrityError as exc:
        if 'id_card' in str(exc).lower():
            raise ValueError(
                f'Số CCCD {id_card or ""} đã tồn tại. Chọn nhân viên có sẵn hoặc nhập CCCD khác.'
            ) from exc
        raise
    return int(cur.lastrowid), code


def resolve_employee_for_contract(conn: sqlite3.Connection, data: dict) -> tuple[int, str | None]:
    """
    Trả (employee_id, employee_code mới nếu vừa tạo).
    - Có employee_id → dùng NV sẵn, tự gán mã nếu thiếu.
    - Không có employee_id nhưng có fullname → tạo NV + mã NV.
    """
    emp_raw = data.get('employee_id')
    try:
        emp_id = int(emp_raw) if emp_raw not in (None, '') else 0
    except (TypeError, ValueError):
        emp_id = 0

    if emp_id:
        row = conn.execute('SELECT id FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not row:
            raise ValueError(f'Không tìm thấy nhân viên id={emp_id}')
        code = ensure_employee_code(conn, emp_id, commit=False)
        return emp_id, code

    fullname = (data.get('fullname') or data.get('employee_name') or '').strip()
    if fullname or data.get('create_employee'):
        new_id, code = create_employee_for_contract(conn, data)
        return new_id, code

    raise ValueError('Chọn nhân viên có sẵn hoặc nhập họ tên để tạo mã NV mới')
