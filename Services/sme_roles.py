"""Vai trò Kế toán SME theo chế độ TT58 / TT99.

Role mới (gán trong Settings):
  accountantSME58 / managerSME58 / adminSME58
  accountantSME99 / managerSME99 / adminSME99

Role cũ accountantSME / managerSME / adminSME vẫn nhận diện được
và đi theo chế độ kế toán của tenant.
"""
from __future__ import annotations

from typing import Iterable

REGIME_TT58 = 'SME_MICRO_TT58'
REGIME_TT99 = 'SME_TT99'

LEGACY_SME_ROLES = frozenset({'accountantSME', 'managerSME', 'adminSME'})
SME58_ROLES = frozenset({'accountantSME58', 'managerSME58', 'adminSME58'})
SME99_ROLES = frozenset({'accountantSME99', 'managerSME99', 'adminSME99'})
SME_ACCOUNTING_ROLES = frozenset(LEGACY_SME_ROLES | SME58_ROLES | SME99_ROLES)

SME_ADMIN_ROLES = frozenset({'adminSME', 'adminSME58', 'adminSME99'})
SME_MANAGER_ROLES = frozenset({'managerSME', 'managerSME58', 'managerSME99'})
SME_ACCOUNTANT_ROLES = frozenset({'accountantSME', 'accountantSME58', 'accountantSME99'})

ADMIN_OR_MASTER_ROLES = frozenset({
    'admin', 'admin*', 'adminFB', 'master',
}) | SME_ADMIN_ROLES

STORE_SETUP_ALLOWED_ROLES = frozenset(ADMIN_OR_MASTER_ROLES | SME_MANAGER_ROLES)

PERMISSION_BYPASS_ROLES = frozenset({
    'master', 'admin', 'adminFB',
}) | SME_ADMIN_ROLES

POS_HKD_ASSIGNABLE_ROLES = (
    'staff', 'staff*', 'staff**',
    'accountant',
    'manager', 'manager*', 'managerFB',
    'admin', 'admin*', 'adminFB',
)

ROLE_LABELS = {
    'accountantSME': 'Kế toán SME',
    'managerSME': 'Quản lý SME',
    'adminSME': 'Quản trị SME',
    'accountantSME58': 'Kế toán SME (TT58)',
    'managerSME58': 'Quản lý SME (TT58)',
    'adminSME58': 'Quản trị SME (TT58)',
    'accountantSME99': 'Kế toán SME (TT99)',
    'managerSME99': 'Quản lý SME (TT99)',
    'adminSME99': 'Quản trị SME (TT99)',
}

# Chính sách: DN siêu nhỏ (TT58) có thể phát sinh mọi nghiệp vụ như DN TT99.
# Do đó TT58 được mở đầy đủ nghiệp vụ mua/bán/kế toán; khác biệt chỉ ở framework
# sổ & biểu mẫu DNSN (+ cấu hình trường hợp thuế) bắt buộc theo TT58.
TT99_ONLY_ENDPOINTS = frozenset()

TT99_ONLY_PATH_PREFIXES = ()

TT99_ONLY_PATH_MARKERS = ()

# Chỉ TT58: sổ DNSN + cấu hình trường hợp thuế / tỷ lệ % (framework TT58).
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
    return str(role or '').strip() in SME_ACCOUNTING_ROLES


def is_sme_admin_role(role) -> bool:
    return str(role or '').strip() in SME_ADMIN_ROLES


def is_sme_manager_role(role) -> bool:
    return str(role or '').strip() in SME_MANAGER_ROLES


def sme_role_regime(role) -> str | None:
    """TT58/TT99 gắn trên role mới; role cũ trả None (đi theo tenant)."""
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
        return ('accountantSME58', 'managerSME58', 'adminSME58')
    if regime == REGIME_TT99:
        return ('accountantSME99', 'managerSME99', 'adminSME99')
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
    if r in SME_ACCOUNTING_ROLES:
        regime = normalize_sme_regime(accounting_regime)
        if regime == REGIME_TT58:
            return False, 'Tenant đang dùng Kế toán SME (TT58) — chỉ gán vai trò TT58.'
        if regime == REGIME_TT99:
            return False, 'Tenant đang dùng Kế toán SME (TT99) — chỉ gán vai trò TT99.'
        return False, 'Không gán vai trò Kế toán SME trên tenant Hộ kinh doanh / POS.'
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
