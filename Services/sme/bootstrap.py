"""Bootstrap schema/seed kế toán SME trên DB tenant."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from db_utils import sqlite_commit

# Cache theo đường dẫn DB — tránh chạy lại hàng chục ensure_* mỗi request (gây load chậm).
_BOOTSTRAP_VERSION = '2026-08-21congno-settle-partner'
_sme_bootstrapped: dict[str, str] = {}


def _optional_schema(conn, fn, /, **kwargs) -> None:
    """Gọi ensure_* tùy chọn — PG: rollback nếu lỗi để không kẹt transaction."""
    try:
        fn(conn, **kwargs)
    except Exception:
        try:
            from db_utils import ignore_db_error
            ignore_db_error(conn)
        except Exception:
            pass


def _conn_db_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
        if row:
            # (seq, name, file)
            path = row[2] if not isinstance(row, sqlite3.Row) else row['file']
            if path:
                return str(path)
    except sqlite3.Error:
        pass
    return f'conn:{id(conn)}'


def ensure_sme_accounting_ready(
    conn: sqlite3.Connection,
    *,
    accounting_regime: str | None = None,
    commit: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Đảm bảo COA + quy tắc định khoản + schema nhật ký sẵn sàng.

    COA seed hiện dùng bộ TT99 (sổ kép chung). Tenant TT58 ghi
    ``ledger_profile=sme_tt58`` — BCTC/biểu mẫu DNSN chọn theo profile,
    không dùng bộ B01–B09-DN của TT99.

    Sau lần đầu trong process, các lần gọi sau (cùng DB) trả về ngay — trừ ``force=True``.
    """
    db_key = _conn_db_key(conn)
    if not force and _sme_bootstrapped.get(db_key) == _BOOTSTRAP_VERSION:
        regime = (accounting_regime or 'SME_TT99')
        profile = 'sme_tt58' if 'TT58' in str(regime).upper() else 'sme_tt99'
        return {
            'coa': {'cached': True},
            'rules': {'cached': True},
            'ledger_profile': profile,
            'accounting_regime': regime,
            'cached': True,
        }

    from Services.sme.coa_service import ensure_sme_coa_ready
    from Services.sme.journal_engine import ensure_sme_journal_ready
    from Services.sme.purchase_order import ensure_purchase_order_schema
    from Services.sme.vouchers import ensure_sme_voucher_schema
    from Services.sme.payroll import ensure_sme_payroll_schema
    from Services.sme.landed_cost import ensure_sme_landed_cost_schema
    from Services.sme.advances import ensure_sme_advance_schema
    from Services.sme.cash_count import ensure_sme_cash_count_schema
    from Services.sme.bank_reconcile import ensure_sme_bank_reconcile_schema
    from Services.sme.fa_lifecycle import (
        ensure_sme_fa_docs_schema,
        ensure_sme_fa_lifecycle_schema,
    )
    from Services.sme.inventory_ops import ensure_sme_inventory_ops_schema
    from Services.sme.cit import ensure_sme_cit_schema
    from Services.sme.fx_revaluation import ensure_sme_fx_schema
    from Services.sme.loans_deposits import ensure_sme_loans_schema
    from Services.sme.letter_of_credit import ensure_sme_lc_schema
    from Services.sme.import_transit import ensure_import_transit_schema
    from Services.sme.capital import ensure_sme_capital_schema
    from Services.sme.labor_contract import ensure_sme_labor_contract_schema
    from Services.sme.labor_sheets import ensure_sme_labor_sheets_schema
    from Services.sme.stock_inspection import ensure_sme_stock_inspection_schema
    from Services.sme.material_remaining import ensure_sme_material_remaining_schema
    from Services.sme.cash_extras import ensure_sme_cash_extras_schema
    from Services.sme.branches import ensure_sme_branches_schema, backfill_asset_branches_from_warehouse
    from Services.sme.tt58_tax_rates import ensure_tt58_tax_rates_schema
    from Services.sme.ledger_ops import ensure_ledger_ops_schema
    from Services.sme.prepaid import ensure_prepaid_schema
    from Services.sme.accruals import ensure_accrual_schema
    from Services.sme.export_payment import ensure_export_sale_schema
    from Services.sme.customs_declaration import ensure_customs_declaration_schema
    from Services.sme.cong_no_ops import ensure_cong_no_schema
    from Services.sme.import_settle import ensure_import_settle_schema
    from Services.sme.service_costing import ensure_service_costing_schema
    from Services.sme.period_cost_allocation import ensure_period_cost_allocation_schema
    from Services.sme.costing_policy import ensure_costing_policy_schema
    from Services.sme.product_cost_standards import ensure_product_cost_standards_schema
    from Services.sme.deferred_revenue import ensure_deferred_revenue_schema
    from Services.tenant_profile import is_sme_regime, normalize_accounting_regime

    coa = ensure_sme_coa_ready(conn, commit=False)
    rules = ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_branches_schema(conn, commit=False)
    backfill_asset_branches_from_warehouse(conn, commit=False)
    ensure_purchase_order_schema(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    ensure_sme_payroll_schema(conn, commit=False)
    ensure_sme_landed_cost_schema(conn, commit=False)
    ensure_sme_advance_schema(conn, commit=False)
    ensure_sme_cash_count_schema(conn, commit=False)
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    ensure_sme_fa_lifecycle_schema(conn, commit=False)
    ensure_sme_fa_docs_schema(conn, commit=False)
    ensure_sme_inventory_ops_schema(conn, commit=False)
    ensure_sme_cit_schema(conn, commit=False)
    ensure_tt58_tax_rates_schema(conn, commit=False)
    ensure_ledger_ops_schema(conn, commit=False)
    ensure_prepaid_schema(conn, commit=False)
    ensure_accrual_schema(conn, commit=False)
    ensure_sme_fx_schema(conn, commit=False)
    ensure_sme_loans_schema(conn, commit=False)
    ensure_sme_lc_schema(conn, commit=False)
    ensure_import_transit_schema(conn, commit=False)
    ensure_sme_capital_schema(conn, commit=False)
    ensure_sme_labor_contract_schema(conn, commit=False)
    ensure_sme_labor_sheets_schema(conn, commit=False)
    ensure_sme_stock_inspection_schema(conn, commit=False)
    ensure_sme_material_remaining_schema(conn, commit=False)
    _optional_schema(conn, ensure_sme_cash_extras_schema, commit=False)
    _optional_schema(conn, ensure_export_sale_schema, commit=False)
    _optional_schema(conn, ensure_customs_declaration_schema, commit=False)
    _optional_schema(conn, ensure_cong_no_schema, commit=False)
    _optional_schema(conn, ensure_import_settle_schema, commit=False)
    _optional_schema(conn, ensure_service_costing_schema, commit=False)
    _optional_schema(conn, ensure_period_cost_allocation_schema, commit=False)
    _optional_schema(conn, ensure_costing_policy_schema, commit=False)
    _optional_schema(conn, ensure_product_cost_standards_schema, commit=False)
    _optional_schema(conn, ensure_deferred_revenue_schema, commit=False)

    existing_meta: dict[str, str] = {}
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
        for r in conn.execute(
            "SELECT key, value FROM sme_coa_seed_meta "
            "WHERE key IN ('ledger_profile','accounting_regime')"
        ).fetchall():
            k = r[0] if not isinstance(r, sqlite3.Row) else r['key']
            v = r[1] if not isinstance(r, sqlite3.Row) else r['value']
            if k:
                existing_meta[str(k)] = str(v or '')
    except sqlite3.Error:
        from db_utils import ignore_db_error
        ignore_db_error(conn)
        existing_meta = {}

    # Không mặc định TT99 khi caller (sale_journal, API…) bỏ trống accounting_regime —
    # lần đó từng ghi đè tenant TT58 thành TT99, làm menu/sổ DNSN “mất tiêu”.
    if accounting_regime:
        regime = normalize_accounting_regime(accounting_regime)
    else:
        regime = normalize_accounting_regime(
            existing_meta.get('accounting_regime') or 'SME_TT99'
        )
    if not is_sme_regime(regime):
        prev = existing_meta.get('accounting_regime') or ''
        regime = (
            normalize_accounting_regime(prev)
            if is_sme_regime(prev)
            else 'SME_TT99'
        )
    profile = 'sme_tt58' if 'TT58' in regime.upper() else 'sme_tt99'
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
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
        # Không tự gán Trường hợp 1 — chưa chọn thì hiện đủ sổ/BCTC cho đến khi user lưu.
    except sqlite3.Error:
        pass

    if commit:
        sqlite_commit(conn, label='bootstrap')

    _sme_bootstrapped[db_key] = _BOOTSTRAP_VERSION
    return {'coa': coa, 'rules': rules, 'ledger_profile': profile, 'accounting_regime': regime}

