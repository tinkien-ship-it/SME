"""Vai trò Kế toán SME theo chế độ TT58 / TT99.

Role mới (gán trong Settings):
  accountantSME58 / managerSME58 / adminSME58
  accountantSME99 / managerSME99 / adminSME99

Role vận hành sản xuất / mua hàng (gán trên tenant SME):
  purchasing_manager — Trưởng Phòng Mua (MRP)
  chief_accountant — Kế Toán Trưởng (MRP)
  production_manager — Trưởng Phòng Sản Xuất (MES)

Role cũ accountantSME / managerSME / adminSME vẫn nhận diện được
và đi theo chế độ kế toán của tenant.
"""
from __future__ import annotations

from functools import wraps
from typing import Iterable

from flask import flash, jsonify, redirect, request, session, url_for
from flask_login import current_user

REGIME_TT58 = 'SME_MICRO_TT58'
REGIME_TT99 = 'SME_TT99'

LEGACY_SME_ROLES = frozenset({'accountantSME', 'managerSME', 'adminSME'})
SME58_ROLES = frozenset({'accountantSME58', 'managerSME58', 'adminSME58'})
SME99_ROLES = frozenset({'accountantSME99', 'managerSME99', 'adminSME99'})
SME_ACCOUNTING_ROLES = frozenset(LEGACY_SME_ROLES | SME58_ROLES | SME99_ROLES)

# Vai trò chuyên môn vận hành (MRP / MES) — login vào hub SME
SME_OPS_ROLES = frozenset({
    'purchasing_manager',
    'chief_accountant',
    'production_manager',
})
SME_TENANT_ROLES = frozenset(SME_ACCOUNTING_ROLES | SME_OPS_ROLES)

SME_ADMIN_ROLES = frozenset({'adminSME', 'adminSME58', 'adminSME99'})
SME_MANAGER_ROLES = frozenset({'managerSME', 'managerSME58', 'managerSME99'})
SME_ACCOUNTANT_ROLES = frozenset({'accountantSME', 'accountantSME58', 'accountantSME99'})

ADMIN_OR_MASTER_ROLES = frozenset({
    'admin', 'admin*', 'adminFB', 'master',
}) | SME_ADMIN_ROLES

# Admin cửa hàng + admin SME + master — được vào cả MRP và MES
MRP_MES_ADMIN_ROLES = frozenset(ADMIN_OR_MASTER_ROLES)

# MRP: kế hoạch NVL / đề xuất mua — Trưởng mua, Kế toán trưởng, Admin
MRP_ACCESS_ROLES = frozenset({
    'purchasing_manager',
    'chief_accountant',
}) | MRP_MES_ADMIN_ROLES

# MES: điều hành xưởng realtime — Trưởng SX, Admin
MES_ACCESS_ROLES = frozenset({
    'production_manager',
}) | MRP_MES_ADMIN_ROLES

STORE_SETUP_ALLOWED_ROLES = frozenset(ADMIN_OR_MASTER_ROLES | SME_MANAGER_ROLES)

PERMISSION_BYPASS_ROLES = frozenset({
    'master', 'admin', 'adminFB',
}) | SME_ADMIN_ROLES

POS_HKD_ASSIGNABLE_ROLES = (
    'staff', 'staff_field', 'staff*', 'staff**',
    'employee',
    'accountant',
    'manager', 'manager*', 'managerFB',
    'admin', 'admin*', 'adminFB',
)

SME_OPS_ASSIGNABLE_ROLES = (
    'purchasing_manager',
    'chief_accountant',
    'production_manager',
)

ROLE_LABELS = {
    'employee': 'Nhân viên (ESS)',
    'staff': 'NV Bán hàng (quầy)',
    'staff_field': 'NV Bán hàng thị trường',
    'staff*': 'NV Lưu trú',
    'staff**': 'NV F&B',
    'accountant': 'Kế toán',
    'manager': 'Quản lý POS',
    'manager*': 'Quản lý Lưu trú',
    'managerFB': 'Quản lý F&B',
    'admin': 'Quản trị POS',
    'admin*': 'Quản trị Lưu trú',
    'adminFB': 'Quản trị F&B',
    'master': 'Master',
    'accountantSME': 'Kế toán SME',
    'managerSME': 'Quản lý SME',
    'adminSME': 'Quản trị SME',
    'accountantSME58': 'Kế toán SME (TT58)',
    'managerSME58': 'Quản lý SME (TT58)',
    'adminSME58': 'Quản trị SME (TT58)',
    'accountantSME99': 'Kế toán SME (TT99)',
    'managerSME99': 'Quản lý SME (TT99)',
    'adminSME99': 'Quản trị SME (TT99)',
    'purchasing_manager': 'Trưởng Phòng Mua (MRP)',
    'chief_accountant': 'Kế Toán Trưởng',
    'production_manager': 'Trưởng Phòng Sản Xuất (MES)',
}

# Permission bổ sung (POS/HRM) — lưu CSV trong users.permissions
ESS_PORTAL_PERMISSION = 'ess_portal'
VIEW_MRP_PERMISSION = 'view_mrp'
VIEW_MES_PERMISSION = 'view_mes'
# Role Settings → "Nhân viên — Cổng ESS (HRM)" — ESS chấm công (không gặp KH)
ESS_PORTAL_ROLE = 'employee'
# NV bán hàng thị trường — home ESS + CRM leads (không POS)
FIELD_SALES_ROLE = 'staff_field'
# Role đăng nhập vào ESS và bị whitelist path (không vào /sale)
ESS_HOME_ROLES = frozenset({ESS_PORTAL_ROLE, FIELD_SALES_ROLE})
# User được HR liên kết ESS (employees.user_id)
ESS_LINKABLE_ROLES = frozenset({ESS_PORTAL_ROLE, FIELD_SALES_ROLE})

# Chính sách: DN siêu nhỏ (TT58) có thể phát sinh mọi nghiệp vụ như DN TT99.
TT99_ONLY_ENDPOINTS = frozenset()

TT99_ONLY_PATH_PREFIXES = ()

TT99_ONLY_PATH_MARKERS = ()

TT58_ONLY_ENDPOINTS = frozenset({
    'SME_dnsn_books', 'SME_dnsn_book', 'SME_dnsn_book_print',
})

TT58_ONLY_PATH_PREFIXES = (
    '/api/sme/tt58-tax-method',
    '/api/sme/tt58-tax-rates',
    '/SME_dnsn_book',
    '/SME_dnsn_books',
)


def normalize_sme_regime(value) -> str | None:
    raw = str(value or '').strip().upper()
    if not raw:
        return None
    if 'TT58' in raw or 'MICRO' in raw:
        return REGIME_TT58
    if 'TT99' in raw or raw.startswith('SME'):
        return REGIME_TT99
    return None


def is_sme_role(role) -> bool:
    """Role thuộc tenant SME (kế toán hoặc vận hành MRP/MES)."""
    return str(role or '').strip() in SME_TENANT_ROLES


def is_sme_accounting_role(role) -> bool:
    return str(role or '').strip() in SME_ACCOUNTING_ROLES


def is_sme_ops_role(role) -> bool:
    return str(role or '').strip() in SME_OPS_ROLES


def is_sme_admin_role(role) -> bool:
    return str(role or '').strip() in SME_ADMIN_ROLES


def is_sme_manager_role(role) -> bool:
    return str(role or '').strip() in SME_MANAGER_ROLES


def current_session_role() -> str:
    try:
        if current_user and getattr(current_user, 'is_authenticated', False):
            return str(getattr(current_user, 'role', '') or '').strip()
    except Exception:
        pass
    try:
        return str(session.get('role') or '').strip()
    except RuntimeError:
        return ''


def role_in(role: str | None, allowed: Iterable[str]) -> bool:
    r = str(role or '').strip()
    return bool(r) and r in frozenset(allowed)


def can_access_mrp(role: str | None = None, permissions=None) -> bool:
    r = str(role if role is not None else current_session_role() or '').strip()
    if role_in(r, MRP_ACCESS_ROLES):
        return True
    from auth import normalize_permissions
    if permissions is None:
        try:
            permissions = (session.get('user') or {}).get('permissions')
        except RuntimeError:
            permissions = None
    return VIEW_MRP_PERMISSION in normalize_permissions(permissions)


def can_access_mes(role: str | None = None, permissions=None) -> bool:
    r = str(role if role is not None else current_session_role() or '').strip()
    if role_in(r, MES_ACCESS_ROLES):
        return True
    from auth import normalize_permissions
    if permissions is None:
        try:
            permissions = (session.get('user') or {}).get('permissions')
        except RuntimeError:
            permissions = None
    return VIEW_MES_PERMISSION in normalize_permissions(permissions)


def require_mrp_access(view_func):
    """Chỉ Trưởng Phòng Mua / Kế Toán Trưởng / Admin (và perm view_mrp)."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not can_access_mrp():
            msg = 'Bạn không có quyền truy cập MRP (Kế hoạch nhu cầu nguyên vật liệu).'
            if request.path.startswith('/api/') or request.is_json:
                return jsonify({'success': False, 'error': msg}), 403
            flash(msg, 'warning')
            try:
                return redirect(url_for('SME_dashboard'))
            except Exception:
                return redirect('/SME_dashboard')
        return view_func(*args, **kwargs)
    return wrapped


def require_mes_access(view_func):
    """Chỉ Trưởng Phòng Sản Xuất / Admin (và perm view_mes)."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not can_access_mes():
            msg = 'Bạn không có quyền truy cập MES (Điều hành sản xuất).'
            if request.path.startswith('/api/') or request.is_json:
                return jsonify({'success': False, 'error': msg}), 403
            flash(msg, 'warning')
            try:
                return redirect(url_for('SME_dashboard'))
            except Exception:
                return redirect('/SME_dashboard')
        return view_func(*args, **kwargs)
    return wrapped


def sme_role_regime(role) -> str | None:
    """TT58/TT99 gắn trên role mới; role cũ / ops trả None (đi theo tenant)."""
    r = str(role or '').strip()
    if r in SME58_ROLES:
        return REGIME_TT58
    if r in SME99_ROLES:
        return REGIME_TT99
    return None


def owner_role_for_regime(accounting_regime) -> str:
    return 'managerSME58' if normalize_sme_regime(accounting_regime) == REGIME_TT58 else 'managerSME99'


def support_role_for_regime(accounting_regime) -> str:
    return 'adminSME58' if normalize_sme_regime(accounting_regime) == REGIME_TT58 else 'adminSME99'


def assignable_sme_roles(accounting_regime) -> tuple[str, ...]:
    regime = normalize_sme_regime(accounting_regime)
    if regime == REGIME_TT58:
        return (
            'accountantSME58', 'managerSME58', 'adminSME58',
        ) + SME_OPS_ASSIGNABLE_ROLES
    if regime == REGIME_TT99:
        return (
            'accountantSME99', 'managerSME99', 'adminSME99',
        ) + SME_OPS_ASSIGNABLE_ROLES
    return ()


def assignable_roles(accounting_regime, *, actor_is_master: bool = False) -> frozenset[str]:
    allowed = set(POS_HKD_ASSIGNABLE_ROLES)
    allowed.update(assignable_sme_roles(accounting_regime))
    if actor_is_master:
        allowed.add('master')
    return frozenset(allowed)


def validate_assignable_role(
    role,
    accounting_regime,
    *,
    actor_is_master: bool = False,
    existing_role: str | None = None,
) -> tuple[bool, str]:
    r = str(role or '').strip()
    if not r:
        return False, 'Vai trò không được để trống'
    allowed = assignable_roles(accounting_regime, actor_is_master=actor_is_master)
    if r in allowed:
        return True, ''
    # Cho phép giữ role SME cũ khi sửa user đã tồn tại.
    if existing_role and r == existing_role and r in LEGACY_SME_ROLES:
        return True, ''
    if r in SME_TENANT_ROLES:
        regime = normalize_sme_regime(accounting_regime)
        if regime == REGIME_TT58:
            return False, 'Tenant đang dùng Kế toán SME (TT58) — chỉ gán vai trò TT58 / vận hành.'
        if regime == REGIME_TT99:
            return False, 'Tenant đang dùng Kế toán SME (TT99) — chỉ gán vai trò TT99 / vận hành.'
        return False, 'Không gán vai trò Kế toán SME / MRP-MES trên tenant Hộ kinh doanh / POS.'
    return False, f'Vai trò không hợp lệ: {r}'


def _path_matches(path: str, prefixes: Iterable[str]) -> bool:
    p = (path or '').split('?', 1)[0]
    for prefix in prefixes:
        if p == prefix or p.startswith(prefix + '/'):
            return True
    return False


def sme_request_allowed(endpoint: str | None, path: str | None, accounting_regime) -> tuple[bool, str]:
    """True nếu trang/API được phép với chế độ tenant hiện tại."""
    regime = normalize_sme_regime(accounting_regime)
    if regime is None:
        return True, ''
    ep = str(endpoint or '')
    pth = str(path or '')

    if regime == REGIME_TT58:
        if ep in TT99_ONLY_ENDPOINTS or _path_matches(pth, TT99_ONLY_PATH_PREFIXES):
            return False, (
                'Trang này thuộc chế độ Kế toán SME (TT99). '
                'Tài khoản đang làm việc trên Kế toán SME (TT58).'
            )
        if any(m in pth for m in TT99_ONLY_PATH_MARKERS):
            return False, (
                'Chức năng này thuộc chế độ Kế toán SME (TT99). '
                'Tài khoản đang làm việc trên Kế toán SME (TT58).'
            )
    elif regime == REGIME_TT99:
        if ep in TT58_ONLY_ENDPOINTS or _path_matches(pth, TT58_ONLY_PATH_PREFIXES):
            return False, (
                'Trang này thuộc chế độ Kế toán SME (TT58) — sổ DNSN. '
                'Tài khoản đang làm việc trên Kế toán SME (TT99).'
            )
    return True, ''
