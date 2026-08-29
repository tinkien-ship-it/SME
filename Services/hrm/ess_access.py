# -*- coding: utf-8 -*-
"""ESS — phân quyền, liên kết user↔NV, chống IDOR."""
from __future__ import annotations

import sqlite3

from auth import normalize_permissions
from flask import session

ESS_PERMISSION = 'ess_portal'

from Services.sme_roles import (
    ESS_HOME_ROLES,
    ESS_LINKABLE_ROLES,
    ESS_PORTAL_ROLE,
    FIELD_SALES_ROLE,
)

# Role NV thường được cấp ESS (kèm permission ess_portal khuyến nghị)
ESS_EMPLOYEE_ROLES = frozenset({
    'employee',
    'staff',
    'staff_field',
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

    urow = conn.execute(
        'SELECT role, username FROM users WHERE id = ?',
        (uid,),
    ).fetchone()
    if not urow:
        raise ValueError('Tài khoản đăng nhập không tồn tại')
    urole = str(urow['role'] if hasattr(urow, 'keys') else urow[0] or '').strip()
    if urole not in ESS_LINKABLE_ROLES:
        raise ValueError(
            'Chỉ gán user role Nhân viên ESS (employee) hoặc '
            'NV Bán hàng thị trường (staff_field). '
            'Tạo user đúng role tại Settings → Users.'
        )

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
    if enable:
        ensure_user_ess_portal(conn, uid)
    if commit:
        sqlite_commit(conn, label='ess_link')


def unlink_employee_ess(
    conn: sqlite3.Connection,
    employee_id: int,
    *,
    commit: bool = True,
) -> None:
    """HR gỡ liên kết user ↔ NV."""
    from db_utils import sqlite_commit
    from Services.hrm.schema import ensure_hrm_schema

    ensure_hrm_schema(conn)
    conn.execute(
        'UPDATE employees SET user_id = NULL, ess_enabled = 0 WHERE id = ?',
        (int(employee_id),),
    )
    if commit:
        sqlite_commit(conn, label='ess_unlink')


def hr_may_manage_ess_link(role, permissions=None) -> bool:
    from auth import normalize_permissions
    from Services.sme_roles import (
        ADMIN_OR_MASTER_ROLES,
        SME_ACCOUNTANT_ROLES,
        SME_MANAGER_ROLES,
    )
    r = str(role or '').strip()
    if r in ADMIN_OR_MASTER_ROLES or r in SME_MANAGER_ROLES or r in SME_ACCOUNTANT_ROLES:
        return True
    # Quản lý POS / lưu trú / F&B / kế toán POS
    if r in {'manager', 'manager*', 'managerFB', 'accountant'}:
        return True
    # HR có quyền sửa dữ liệu (thường thao tác danh sách NV / HĐLĐ)
    if 'edit_data' in normalize_permissions(permissions):
        return True
    return False


def is_ess_portal_only_user(role) -> bool:
    """User home ESS + whitelist path (employee hoặc staff_field) — không vào POS."""
    return str(role or '').strip() in ESS_HOME_ROLES


def is_field_sales_user(role) -> bool:
    return str(role or '').strip() == FIELD_SALES_ROLE


def can_ess_customer_visits(role) -> bool:
    """Gặp khách trên ESS: thị trường / quầy — không dành cho employee thuần."""
    r = str(role or '').strip()
    if r == ESS_PORTAL_ROLE:
        return False
    return r in ESS_EMPLOYEE_ROLES or r == FIELD_SALES_ROLE


def ess_portal_path_allowed(path: str, role: str | None = None) -> bool:
    """Route được phép khi đăng nhập employee / staff_field."""
    p = (path or '').split('?', 1)[0].rstrip('/') or '/'
    if p.startswith('/static') or p in ('/favicon.ico',):
        return True
    r = str(role or '').strip()
    allowed = ['/hrm/ess', '/api/hrm/ess', '/logout', '/login']
    if r == FIELD_SALES_ROLE:
        allowed.extend([
            '/crm', '/api/crm',
            '/ess',  # alias
        ])
    for pref in allowed:
        if p == pref or p.startswith(pref + '/'):
            return True
    return False


def session_may_manage_ess_link() -> bool:
    from flask import session
    user = session.get('user') or {}
    uid = session.get('user_id') or user.get('id')
    username = user.get('username') or session.get('username')
    if not uid and not username:
        return False
    r = str(session.get('role') or user.get('role') or '').strip()
    perms = user.get('permissions')
    if r in ESS_HOME_ROLES:
        return False
    if r in ESS_EMPLOYEE_ROLES:
        from auth import normalize_permissions
        return 'edit_data' in normalize_permissions(perms)
    return True


def ensure_user_ess_portal(conn: sqlite3.Connection, user_id: int) -> bool:
    """Bổ sung permission ess_portal khi HR gán ESS (nếu chưa có)."""
    from auth import normalize_permissions
    row = conn.execute(
        'SELECT permissions FROM users WHERE id = ?',
        (int(user_id),),
    ).fetchone()
    if not row:
        return False
    raw = row['permissions'] if hasattr(row, 'keys') else row[0]
    perms = normalize_permissions(raw)
    if ESS_PERMISSION in perms:
        return False
    perms.append(ESS_PERMISSION)
    conn.execute(
        'UPDATE users SET permissions = ? WHERE id = ?',
        (','.join(perms), int(user_id)),
    )
    return True


def list_ess_linkable_users(
    conn: sqlite3.Connection,
    *,
    employee_id: int | None = None,
) -> list[dict]:
    """User role employee / staff_field từ Settings — HR chọn gán NV."""
    from auth import normalize_permissions
    from Services.hrm.schema import ensure_hrm_schema
    from Services.sme_roles import ROLE_LABELS

    ensure_hrm_schema(conn)
    eid = int(employee_id) if employee_id else None

    current_uid = None
    if eid:
        row = conn.execute(
            'SELECT user_id FROM employees WHERE id = ? LIMIT 1',
            (eid,),
        ).fetchone()
        if row:
            raw = row['user_id'] if hasattr(row, 'keys') else row[0]
            if raw:
                current_uid = int(raw)

    linked_by_user: dict[int, dict] = {}
    for r in conn.execute(
        'SELECT id, user_id, fullname, employee_code FROM employees WHERE user_id IS NOT NULL'
    ).fetchall():
        emp = dict(r) if hasattr(r, 'keys') else {
            'id': r[0], 'user_id': r[1], 'fullname': r[2], 'employee_code': r[3],
        }
        uid = int(emp.get('user_id') or 0)
        if uid:
            linked_by_user[uid] = {
                'employee_id': int(emp['id']),
                'fullname': emp.get('fullname') or '',
                'employee_code': emp.get('employee_code') or '',
            }

    placeholders = ','.join('?' for _ in ESS_LINKABLE_ROLES)
    out: list[dict] = []
    for r in conn.execute(
        f"""
        SELECT id, username, full_name, role, email, phone, permissions
        FROM users
        WHERE TRIM(COALESCE(role, '')) IN ({placeholders})
        ORDER BY
          CASE TRIM(COALESCE(role, '')) WHEN 'staff_field' THEN 0 ELSE 1 END,
          COALESCE(NULLIF(TRIM(full_name), ''), username), username
        """,
        tuple(ESS_LINKABLE_ROLES),
    ).fetchall():
        u = dict(r) if hasattr(r, 'keys') else {
            'id': r[0], 'username': r[1], 'full_name': r[2], 'role': r[3],
            'email': r[4], 'phone': r[5], 'permissions': r[6],
        }
        uid = int(u['id'])
        role = str(u.get('role') or '').strip()
        perms = normalize_permissions(u.get('permissions'))
        link = linked_by_user.get(uid)
        linked_elsewhere = bool(
            link and (not eid or int(link['employee_id']) != int(eid))
        )
        ess_ready = ESS_PERMISSION in perms or role in ESS_LINKABLE_ROLES
        out.append({
            'id': uid,
            'username': u.get('username') or '',
            'full_name': u.get('full_name') or '',
            'email': u.get('email') or '',
            'phone': u.get('phone') or '',
            'role': role,
            'role_label': ROLE_LABELS.get(role, role),
            'has_ess_portal': ESS_PERMISSION in perms,
            'ess_ready': ess_ready,
            'selectable': not linked_elsewhere,
            'linked_employee_id': link['employee_id'] if link else None,
            'linked_employee_name': link['fullname'] if link else None,
            'linked_employee_code': link.get('employee_code', '') if link else None,
            'is_current': uid == current_uid,
        })
    return out
