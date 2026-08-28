# -*- coding: utf-8 -*-
"""ESS — phân quyền, liên kết user↔NV, chống IDOR."""
from __future__ import annotations

import sqlite3

from auth import normalize_permissions
from flask import session

ESS_PERMISSION = 'ess_portal'

# Role NV thường được cấp ESS (kèm permission ess_portal khuyến nghị)
ESS_EMPLOYEE_ROLES = frozenset({
    'employee',
    'staff',
    'staff*',
    'staff**',
})


class EssAccessDenied(PermissionError):
    """Từ chối truy cập ESS — message hiển thị cho user."""


def session_may_use_ess(*, role: str | None = None, permissions=None) -> bool:
    role = str(role if role is not None else session.get('role') or '').strip()
    perms = normalize_permissions(
        permissions if permissions is not None else (session.get('user') or {}).get('permissions')
    )
    if ESS_PERMISSION in perms:
        return True
    return role in ESS_EMPLOYEE_ROLES


def resolve_ess_employee(
    conn: sqlite3.Connection,
    *,
    user_id: int | None = None,
    role: str | None = None,
    permissions=None,
) -> dict:
    """
    NV được phép ESS khi:
    - Tài khoản có quyền ess_portal (hoặc role NV)
    - employees.user_id khớp session
    - employees.ess_enabled = 1
    """
    from Services.hrm.schema import ensure_hrm_schema

    ensure_hrm_schema(conn)
    uid = user_id if user_id is not None else session.get('user_id')
    if not uid:
        raise EssAccessDenied('Chưa đăng nhập')

    if not session_may_use_ess(role=role, permissions=permissions):
        raise EssAccessDenied(
            'Tài khoản chưa được cấp quyền Cổng nhân viên (permission: ess_portal). '
            'Liên hệ HR/Quản trị.'
        )

    row = conn.execute(
        'SELECT * FROM employees WHERE user_id = ? LIMIT 1',
        (int(uid),),
    ).fetchone()
    if not row:
        raise EssAccessDenied(
            'Chưa liên kết tài khoản với hồ sơ nhân viên (employees.user_id). '
            'Liên hệ HR để được gán.'
        )

    emp = dict(row) if hasattr(row, 'keys') else {}
    if not int(emp.get('ess_enabled') or 0):
        raise EssAccessDenied(
            'Nhân viên chưa được bật ESS (ess_enabled). Liên hệ HR.'
        )
    return emp


def bind_ess_employee_id(data: dict, emp: dict) -> dict:
    """Ép employee_id từ NV đã xác thực — chống IDOR."""
    out = dict(data or {})
    out['employee_id'] = int(emp['id'])
    return out


def link_employee_ess(
    conn: sqlite3.Connection,
    employee_id: int,
    user_id: int,
    *,
    enable: bool = True,
    commit: bool = True,
) -> None:
    """HR gán user ↔ NV và bật ESS."""
    from db_utils import sqlite_commit
    from Services.hrm.schema import ensure_hrm_schema

    ensure_hrm_schema(conn)
    eid, uid = int(employee_id), int(user_id)
    dup = conn.execute(
        'SELECT id, fullname FROM employees WHERE user_id = ? AND id != ? LIMIT 1',
        (uid, eid),
    ).fetchone()
    if dup:
        name = dup['fullname'] if hasattr(dup, 'keys') else dup[1]
        raise ValueError(f'User đã liên kết NV khác: {name}')

    conn.execute(
        'UPDATE employees SET user_id = ?, ess_enabled = ? WHERE id = ?',
        (uid, 1 if enable else 0, eid),
    )
    if commit:
        sqlite_commit(conn, label='ess_link')
