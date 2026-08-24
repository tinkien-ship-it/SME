"""Khởi tạo dữ liệu tenant mới.

Tenant dùng thử tự đăng ký phải có đầy đủ schema nhưng không được kế thừa dữ
liệu kinh doanh hoặc thông tin tích hợp từ database.db mẫu.
"""
from __future__ import annotations

import sqlite3
from db_utils import sqlite_commit


# Chỉ gồm dữ liệu nghiệp vụ / cấu hình riêng của tenant. Các bảng tham chiếu
# hệ thống (chart_of_accounts, salary_regions, ...) được giữ lại.
TRIAL_BUSINESS_TABLES = (
    # Bán hàng, mua hàng, kho
    "return_sales",
    "return_import",
    "sale_items",
    "sale",
    "import_details",
    "import_payments",
    "import",
    "orders",
    "stock_moves",
    "inventory_transactions",
    "inventory",
    "product_bom_items",
    "product_bom",
    "production_order_materials",
    "production_orders",
    "product_aliases",
    "products",
    "customers",
    "suppliers",
    "supplier_invoice",
    # Chứng từ, công nợ, ngân hàng
    "chi_tiet_phieu_nhap_kho",
    "phieu_nhap_kho",
    "phieu_xuat_kho",
    "phieu_thu",
    "phieu_chi",
    "cong_no",
    "loans",
    "bank_transactions",
    "bank_payment_log",
    "accounting_transaction_detail",
    "accounting_transaction",
    "account_balance",
    # Hóa đơn
    "outward_invoice_details",
    "outward_invoices",
    "invoice_settings",
    "matbao_webhooks",
    # Nhân sự, tiền lương, bảo hiểm
    "salary_detail",
    "salary_history",
    "bang_luong",
    "so_theo_doi_tien_luong",
    "attendance_logs",
    "attendance_devices",
    "employees",
    "staff",
    # TSCĐ, CCDC, thuế
    "fixed_assets",
    "tools_supplies",
    "tax_declarations",
    "tax_rate_schedules",
    "thue_khac",
    "hkd_nganh_nghe",
    "operating_cost",
    # Phòng trọ, F&B
    "renters",
    "rooms",
    "menu_recipes",
    "menu",
    "ingredients",
    "draft_inventory",
    "tables",
    "areas",
    # Sổ và dữ liệu phát sinh
    "so_chi_tiet_doanh_thu",
    "so_chi_tiet_hang_hoa",
    "so_quy_tien_mat",
    "so_tien_gui_ngan_hang",
    # Nhật ký/dữ liệu hỗ trợ sao chép từ DB mẫu
    "sale_audit_log",
    "login_history",
    "audit_log",
    "assistant_chat_logs",
    "assistant_faq_dynamic",
    "assistant_zalo_sessions",
    "knowledge_articles",
    "knowledge_sync_meta",
    # Cấu hình riêng có thể chứa khóa API/bí mật của DB mẫu
    "settings",
    # Xóa kho tùy chỉnh từ DB mẫu; migration sẽ tạo kho mặc định cần thiết.
    "warehouses",
)


def _existing_tables(cursor: sqlite3.Cursor) -> dict[str, str]:
    rows = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]).lower(): str(row[0]) for row in rows}


def clear_trial_business_data(conn: sqlite3.Connection) -> list[str]:
    """Xóa dữ liệu mẫu, giữ schema và các danh mục tham chiếu hệ thống."""
    cursor = conn.cursor()
    # PRAGMA foreign_keys chỉ có hiệu lực ngoài transaction — commit trước khi tắt FK.
    try:
        sqlite_commit(conn, label='tenant_db_bootstrap')
    except sqlite3.Error:
        pass
    cursor.execute('PRAGMA foreign_keys=OFF')
    existing = _existing_tables(cursor)
    cleared: list[str] = []

    try:
        for requested in TRIAL_BUSINESS_TABLES:
            actual = existing.get(requested.lower())
            if not actual:
                continue
            cursor.execute(f'DELETE FROM "{actual}"')
            cleared.append(actual)

        import_seq = existing.get("import_sequence")
        if import_seq:
            cursor.execute(f'DELETE FROM "{import_seq}"')
            cursor.execute(
                f'INSERT INTO "{import_seq}" (id, current_seq) VALUES (1, 0)'
            )

        voucher_seq = existing.get("voucher_seq")
        if voucher_seq:
            cursor.execute(f'DELETE FROM "{voucher_seq}"')
            cursor.executemany(
                f'INSERT INTO "{voucher_seq}" (type, seq) VALUES (?, 0)',
                (("PT",), ("PC",), ("PN",), ("PX",)),
            )

        sqlite_sequence = existing.get("sqlite_sequence")
        if sqlite_sequence:
            reset_names = {
                name.lower()
                for name in cleared
            } | {"users", "business_info"}
            cursor.executemany(
                f'DELETE FROM "{sqlite_sequence}" WHERE LOWER(name) = ?',
                ((name,) for name in reset_names),
            )
    finally:
        cursor.execute('PRAGMA foreign_keys=ON')

    return cleared
