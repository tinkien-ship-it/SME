"""Side-effect HKD vs SME — tránh ghi phiếu thu/chi HKD trên tenant DN."""
from __future__ import annotations

from typing import Any


def write_hkd_cash_vouchers(
    accounting_regime: str | None = None,
    *,
    profile: dict[str, Any] | None = None,
) -> bool:
    """
    True → được phép INSERT phieu_thu / phieu_chi (sổ HKD).
    False → tenant SME (TT58/TT99): chỉ dùng sme_journal + sme_vouchers.
    """
    from Services.tenant_profile import is_sme_regime

    if profile is not None:
        return not is_sme_regime(profile.get('accounting_regime'))
    return not is_sme_regime(accounting_regime)
