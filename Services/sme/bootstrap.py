"""Bootstrap schema/seed kế toán SME trên DB tenant."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


def ensure_sme_accounting_ready(
    conn: sqlite3.Connection,
    *,
    accounting_regime: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Đảm bảo COA TT99 + quy tắc định khoản + schema nhật ký sẵn sàng.

    TT58 siêu nhỏ hiện dùng cùng hệ thống TK kép TT99 (thực tế phần mềm);
    ``ledger_profile`` được ghi vào meta để UI/filing phân biệt kỳ kê khai.
    """
    from Services.sme.coa_service import ensure_sme_coa_ready
    from Services.sme.journal_engine import ensure_sme_journal_ready
    from Services.sme.purchase_order import ensure_purchase_order_schema
    from Services.sme.vouchers import ensure_sme_voucher_schema
    from Services.sme.payroll import ensure_sme_payroll_schema
    from Services.sme.landed_cost import ensure_sme_landed_cost_schema
    from Services.tenant_profile import is_sme_regime, normalize_accounting_regime

    coa = ensure_sme_coa_ready(conn, commit=False)
    rules = ensure_sme_journal_ready(conn, commit=False)
    ensure_purchase_order_schema(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    ensure_sme_payroll_schema(conn, commit=False)
    ensure_sme_landed_cost_schema(conn, commit=False)

    regime = normalize_accounting_regime(accounting_regime or 'SME_TT99')
    if not is_sme_regime(regime):
        regime = 'SME_TT99'
    profile = 'sme_tt58' if 'TT58' in regime.upper() else 'sme_tt99'
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sme_coa_seed_meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
            """
        )
        for key, value in (
            ('ledger_profile', profile),
            ('accounting_regime', regime),
        ):
            conn.execute(
                """
                INSERT INTO sme_coa_seed_meta(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
    except sqlite3.Error:
        pass

    if commit:
        conn.commit()
    return {'coa': coa, 'rules': rules, 'ledger_profile': profile, 'accounting_regime': regime}
