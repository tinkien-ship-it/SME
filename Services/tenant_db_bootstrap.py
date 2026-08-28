"""Khởi tạo / xóa dữ liệu nghiệp vụ tenant.

Tenant dùng thử tự đăng ký phải có đầy đủ schema nhưng không được kế thừa dữ
liệu kinh doanh hoặc thông tin tích hợp từ database.db mẫu.
"""
from __future__ import annotations

import logging
import sqlite3

from db_utils import sqlite_commit

logger = logging.getLogger(__name__)

# Giữ lại: tài khoản, hồ sơ DN, danh mục tham chiếu / hạt giống kế toán.
TENANT_PRESERVE_TABLES = frozenset({
    'users',
    'business_info',
    'chart_of_accounts',
    'salary_regions',
    'sme_chart_of_accounts',
    'sme_coa_seed_meta',
    'sme_posting_rules',
    'sme_account_roles',
    'sme_account_roles_meta',
    'sme_journal_seed_meta',
    'sme_tt58_tax_rates',
})

# Bảng registry — không thuộc tenant shop (nếu lẫn từ DB mẫu thì bỏ qua).
_TENANT_SKIP_TABLES = frozenset({
    'tenants',
    'user_tenant_mapping',
    'user_trusted_devices',
    'sqlite_sequence',
    'sqlite_stat1',
})

# Thứ tự ưu tiên (con → cha) — bổ sung thêm auto-quét mọi bảng còn lại.
TRIAL_BUSINESS_TABLES = (
    # SME — sổ kép / chứng từ (con trước)
    'sme_journal_lines',
    'sme_journal_entries',
    'sme_account_balances',
    'sme_vouchers',
    'sme_bank_reconcile_matches',
    'sme_bank_reconciliations',
    'sme_fx_revaluation_lines',
    'sme_fx_revaluations',
    'sme_landed_cost_lines',
    'sme_landed_cost_docs',
    'sme_lc_settlements',
    'sme_lc_docs',
    'sme_advance_lines',
    'sme_advance_docs',
    'sme_consign_event_lines',
    'sme_consign_events',
    'sme_agent_delivery_lines',
    'sme_agent_deliveries',
    'sme_labor_sheet_lines',
    'sme_labor_sheets',
    'sme_labor_contract_settlements',
    'sme_labor_contracts',
    'sme_material_allocation_lines',
    'sme_material_allocations',
    'sme_material_remaining_lines',
    'sme_material_remaining',
    'sme_stock_count_lines',
    'sme_stock_counts',
    'sme_stock_inspection_lines',
    'sme_stock_inspections',
    'sme_stock_transfer_lines',
    'sme_stock_transfers',
    'sme_gold_sheet_lines',
    'sme_gold_sheets',
    'sme_fa_inventory_lines',
    'sme_fa_docs',
    'sme_fa_disposals',
    'sme_insurance_pay_alloc',
    'sme_import_advances',
    'sme_export_doc_discounts',
    'sme_export_costs',
    'sme_sale_advances',
    'sme_loan_interest',
    'sme_production_cost_entries',
    'sme_purchase_order_lines',
    'sme_purchase_orders',
    'sme_payroll_runs',
    'sme_auto_asset_postings',
    'sme_cit_provisions',
    'sme_capital_docs',
    'sme_accrual_docs',
    'sme_cash_counts',
    'sme_cash_listings',
    'sme_customs_declarations',
    'sme_deposits',
    'sme_loans',
    'sme_prepaid_expenses',
    'sme_purchase_02_tndn',
    'sme_b09_narratives',
    'sme_bctc_opening',
    'sme_book_reopens',
    'sme_filing_closes',
    'sme_period_locks',
    'sme_ledger_ops',
    'sme_tax_payments',
    'sme_branches',
    'user_branches',
    'hr_kpi_targets',
    'hr_kpi_definitions',
    'hrm_employment_contracts',
    'hrm_employee_shifts',
    'hrm_shifts',
    'hrm_ot_policies',
    'hrm_leave_requests',
    'hrm_attendance_explain',
    'hrm_payroll_formulas',
    'hrm_salary_effective',
    'hrm_compliance_events',
    'hrm_mobile_checkins',
    'hrm_webhook_logs',
    # Bán hàng, mua hàng, kho
    'return_sales',
    'return_import',
    'sale_items',
    'sale',
    'import_details',
    'import_payments',
    'import',
    'orders',
    'stock_moves',
    'inventory_lot_consumptions',
    'inventory_lots',
    'inventory_transactions',
    'inventory',
    'production_fg_receipts',
    'product_bom_items',
    'product_bom',
    'production_order_materials',
    'production_orders',
    'product_aliases',
    'products',
    'crm_notifications',
    'crm_surveys',
    'crm_ticket_events',
    'crm_tickets',
    'crm_contracts',
    'crm_targets',
    'crm_campaigns',
    'crm_assign_state',
    'crm_settings',
    'crm_quote_items',
    'crm_quotes',
    'crm_activities',
    'crm_opportunities',
    'crm_leads',
    'customers',
    'suppliers',
    'supplier_invoice',
    # Chứng từ, công nợ, ngân hàng
    'chi_tiet_phieu_nhap_kho',
    'phieu_nhap_kho',
    'phieu_xuat_kho',
    'phieu_thu',
    'phieu_chi',
    'cong_no',
    'loans',
    'bank_transactions',
    'bank_payment_log',
    'accounting_transaction_detail',
    'accounting_transaction',
    'accounting_jobs',
    'accounting_rule',
    'account_balance',
    'scheduler_runs',
    # Hóa đơn
    'outward_invoice_details',
    'outward_invoices',
    'invoice_settings',
    'matbao_webhooks',
    # Nhân sự, tiền lương, bảo hiểm
    'salary_detail',
    'salary_history',
    'bang_luong',
    'so_theo_doi_tien_luong',
    'attendance_logs',
    'attendance_devices',
    'employees',
    'staff',
    # TSCĐ, CCDC, thuế
    'fixed_assets',
    'tools_supplies',
    'tax_declarations',
    'tax_rate_schedules',
    'thue_khac',
    'hkd_nganh_nghe',
    'operating_cost',
    # Phòng trọ, F&B
    'renters',
    'rooms',
    'menu_recipes',
    'menu',
    'ingredients',
    'draft_inventory',
    'tables',
    'areas',
    # Sổ và dữ liệu phát sinh
    'so_chi_tiet_doanh_thu',
    'so_chi_tiet_hang_hoa',
    'so_quy_tien_mat',
    'so_tien_gui_ngan_hang',
    # Nhật ký / tích hợp / cấu hình tenant
    'sale_audit_log',
    'login_history',
    'audit_log',
    'assistant_chat_logs',
    'assistant_faq_dynamic',
    'assistant_zalo_sessions',
    'knowledge_articles',
    'knowledge_sync_meta',
    'settings',
    'warehouses',
)


def _existing_tables(cursor: sqlite3.Cursor) -> dict[str, str]:
    rows = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]).lower(): str(row[0]) for row in rows}


def _delete_table(cursor: sqlite3.Cursor, actual: str) -> bool:
    try:
        cursor.execute(f'DELETE FROM "{actual}"')
        return True
    except sqlite3.Error as exc:
        logger.warning('purge skip table %s: %s', actual, exc)
        return False


def _reset_business_sequences(cursor: sqlite3.Cursor, existing: dict[str, str], cleared: list[str]) -> None:
    import_seq = existing.get('import_sequence')
    if import_seq:
        cursor.execute(f'DELETE FROM "{import_seq}"')
        try:
            cursor.execute(
                f'INSERT INTO "{import_seq}" (id, current_seq) VALUES (1, 0)'
            )
        except sqlite3.Error:
            pass

    voucher_seq = existing.get('voucher_seq')
    if voucher_seq:
        cursor.execute(f'DELETE FROM "{voucher_seq}"')
        try:
            cursor.executemany(
                f'INSERT INTO "{voucher_seq}" (type, seq) VALUES (?, 0)',
                (('PT',), ('PC',), ('PN',), ('PX',)),
            )
        except sqlite3.Error:
            pass

    sqlite_sequence = existing.get('sqlite_sequence')
    if sqlite_sequence:
        reset_names = {name.lower() for name in cleared}
        cursor.executemany(
            f'DELETE FROM "{sqlite_sequence}" WHERE LOWER(name) = ?',
            ((name,) for name in reset_names),
        )


def purge_tenant_business_data(conn: sqlite3.Connection) -> list[str]:
    """Xóa toàn bộ dữ liệu nghiệp vụ tenant; giữ users, business_info, danh mục tham chiếu."""
    cursor = conn.cursor()
    try:
        sqlite_commit(conn, label='purge_tenant_pre')
    except sqlite3.Error:
        pass

    cursor.execute('PRAGMA foreign_keys=OFF')
    existing = _existing_tables(cursor)
    cleared: list[str] = []
    seen: set[str] = set()

    try:
        for requested in TRIAL_BUSINESS_TABLES:
            actual = existing.get(requested.lower())
            if not actual or actual.lower() in seen:
                continue
            if _delete_table(cursor, actual):
                cleared.append(actual)
                seen.add(actual.lower())

        preserve = TENANT_PRESERVE_TABLES | _TENANT_SKIP_TABLES
        for name_lower, actual in sorted(existing.items()):
            if name_lower in seen or name_lower in preserve:
                continue
            if name_lower.startswith('sqlite_'):
                continue
            if _delete_table(cursor, actual):
                cleared.append(actual)
                seen.add(name_lower)

        _reset_business_sequences(cursor, existing, cleared)
    finally:
        cursor.execute('PRAGMA foreign_keys=ON')

    try:
        sqlite_commit(conn, label='purge_tenant')
    except sqlite3.Error:
        pass

    return cleared


def reseed_tenant_defaults(conn: sqlite3.Connection) -> None:
    """Tạo lại kho/chi nhánh mặc định sau khi xóa dữ liệu."""
    try:
        from db.init import ensure_tenant_db_schema
        ensure_tenant_db_schema(conn)
    except Exception as exc:
        logger.warning('reseed ensure_tenant_db_schema: %s', exc)
    try:
        from Services.import_line_helpers import ensure_warehouse_schema
        ensure_warehouse_schema(conn)
    except Exception as exc:
        logger.warning('reseed ensure_warehouse_schema: %s', exc)
    try:
        sqlite_commit(conn, label='reseed_tenant_defaults')
    except sqlite3.Error:
        pass


def clear_trial_business_data(conn: sqlite3.Connection) -> list[str]:
    """Xóa dữ liệu mẫu khi tạo tenant mới (alias purge đầy đủ)."""
    return purge_tenant_business_data(conn)
