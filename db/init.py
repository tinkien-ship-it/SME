"""Khoi tao va migrate schema SQLite."""
import sqlite3

from db_utils import get_db_connection, get_main_db_connection

# Bang chi ton tai tren database he thong (registry). Neu thieu thi moi request
# deu 500 vi "no such table: tenants" — tao lai ngay khi migrate de app van boot.
REGISTRY_TABLES_DDL = {
    'tenants': """
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT UNIQUE NOT NULL,
            db_path TEXT NOT NULL,
            business_name TEXT,
            phone TEXT,
            address TEXT,
            email TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            settings TEXT DEFAULT '{}',
            master_settings TEXT DEFAULT '{}',
            expiry_date TEXT,
            is_2fa_enabled INTEGER DEFAULT 1,
            google_login_allowed INTEGER DEFAULT 1,
            business_type TEXT
        )
    """,
    'user_tenant_mapping': """
        CREATE TABLE IF NOT EXISTS user_tenant_mapping (
            username TEXT PRIMARY KEY,
            email TEXT,
            tenant_id TEXT,
            otp_secret TEXT,
            twofa_type TEXT DEFAULT 'email',
            last_2fa_at DATETIME,
            trust_device_token TEXT,
            is_active INTEGER DEFAULT 1,
            google_login_allowed INTEGER DEFAULT 1,
            is_2fa_enabled INTEGER DEFAULT 1,
            business_type TEXT
        )
    """,
    'user_trusted_devices': """
        CREATE TABLE IF NOT EXISTS user_trusted_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            device_fingerprint TEXT,
            last_login DATETIME,
            UNIQUE(username, device_fingerprint)
        )
    """,
}


def ensure_registry_tables(conn=None):
    """Tao bang registry con thieu tren main DB. Tra ve list ten bang da tao."""
    from db.dialect import is_postgres, table_exists
    from db.sql_compat import convert_sqlite_ddl

    own = conn is None
    conn = conn or get_main_db_connection()
    created = []
    try:
        for name, ddl in REGISTRY_TABLES_DDL.items():
            existed = table_exists(conn, name)
            sql = convert_sqlite_ddl(ddl) if is_postgres() else ddl
            conn.execute(sql)
            if not existed:
                created.append(name)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tenant_id ON tenants(tenant_id)')
        conn.commit()
        try:
            from Services.firm_tenant import ensure_firm_schema
            ensure_firm_schema(conn)
        except Exception:
            pass
    finally:
        if own:
            conn.close()
    return created

_INVOICE_SETTINGS_DDL = """
    CREATE TABLE IF NOT EXISTS invoice_settings (
        provider_name TEXT PRIMARY KEY,
        api_url TEXT,
        username TEXT,
        password TEXT,
        api_key TEXT,
        app_id TEXT,
        app_secret TEXT,
        serial_number TEXT,
        tax_code TEXT,
        invoice_series TEXT,
        invoice_type TEXT,
        etax_password TEXT,
        etax_cvalue TEXT,
        etax_ckey TEXT,
        esign_pin TEXT,
        sign_service_url TEXT,
        misa_has_code INTEGER DEFAULT 0,
        minvoice_cctbao_id TEXT,
        minvoice_has_code INTEGER DEFAULT 1,
        auto_issue_invoice INTEGER DEFAULT 0,
        auto_issue_schedule INTEGER DEFAULT 0,
        auto_sync_purchase INTEGER DEFAULT 1,
        purchase_api_url TEXT,
        is_active INTEGER DEFAULT 0,
        updated_at TEXT
    )
"""

_INVOICE_SETTINGS_COLS = [
    ('sign_service_url', 'TEXT'),
    ('misa_has_code', 'INTEGER DEFAULT 0'),
    ('minvoice_cctbao_id', 'TEXT'),
    ('minvoice_has_code', 'INTEGER DEFAULT 1'),
    ('app_id', 'TEXT'),
    ('api_key', 'TEXT'),
    ('esign_pin', 'TEXT'),
    ('auto_issue_invoice', 'INTEGER DEFAULT 0'),
    ('auto_issue_schedule', 'INTEGER DEFAULT 0'),
    ('auto_sync_purchase', 'INTEGER DEFAULT 1'),
    ('purchase_api_url', 'TEXT'),
    ('is_active', 'INTEGER DEFAULT 0'),
    ('updated_at', 'TEXT'),
    # PXK điện tử Mắt Bão (mẫu số 6) — ký hiệu ký tự thứ 4 = N hoặc B
    ('pxk_internal_series', 'TEXT'),
    ('pxk_agency_series', 'TEXT'),
    ('auto_issue_pxk_internal', 'INTEGER DEFAULT 0'),
    ('auto_issue_pxk_agency', 'INTEGER DEFAULT 0'),
]


def ensure_invoice_settings_schema(conn):
    """Tạo/migrate bảng invoice_settings trên DB tenant (hoặc main)."""
    c = conn.cursor()

    def has_column(table, column):
        try:
            c.execute(f"PRAGMA table_info({table})")
            return column in [r[1] for r in c.fetchall()]
        except Exception:
            return False

    c.execute(_INVOICE_SETTINGS_DDL)
    for col, col_type in _INVOICE_SETTINGS_COLS:
        if not has_column('invoice_settings', col):
            try:
                c.execute(f"ALTER TABLE invoice_settings ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as e:
                print(f"[MIGRATE] Không thể thêm {col} vào invoice_settings: {e}")
    conn.commit()


_PRODUCTS_COLS = [
    ('unit1', 'TEXT'),
    ('unit_ratio', 'INTEGER DEFAULT 1'),
    ('price', 'REAL'),
    ('sell_by_weight', 'INTEGER DEFAULT 0'),
    ('weight_plu', 'TEXT'),
    ('product_type', "TEXT DEFAULT 'goods'"),
    ('business_line', 'TEXT'),
    ('hkd_sector_code', 'TEXT'),
    ('is_subscription_plan', 'INTEGER DEFAULT 0'),
    ('has_einvoice', 'INTEGER DEFAULT 0'),
    ('updated_at', 'TEXT'),
]

_TENANT_TABLE_EXTRAS = [
    ('import', 'bill_no', 'TEXT'),
    ('suppliers', 'tax_code', 'TEXT'),
    ('sale', 'invoice_number', 'TEXT'),
    ('sale', 'invoice_provider', 'TEXT'),
    ('return_import', 'cost_price', 'REAL'),
    ('return_import', 'refund_amount', 'REAL'),
    ('users', 'permissions', 'TEXT'),
    ('sale_items', 'hkd_sector_code', 'TEXT'),
    ('sale', 'business_line', 'TEXT'),
    ('sale', 'table_id', 'INTEGER'),
    ('sale', 'email', 'TEXT'),
    ('sale', 'company_name', 'TEXT'),
    ('sale', 'sale_no', 'TEXT'),
    ('sale', 'customer_phone', 'TEXT'),
    ('sale', 'address', 'TEXT'),
    ('sale', 'tax_code', 'TEXT'),
    ('sale', 'budget_unit_code', 'TEXT'),
    ('sale', 'passport_no', 'TEXT'),
    ('sale_items', 'menu_id', 'INTEGER'),
    ('sale_items', 'use_sale_unit', 'INTEGER DEFAULT 0'),
    ('sale_items', 'item_name', 'TEXT'),
    ('sale_items', 'product_name', 'TEXT'),
    ('sale_items', 'unit', 'TEXT'),
    ('sale_items', 'line_total', 'REAL'),
    ('sale_items', 'created_at', 'TEXT'),
    ('sale_items', 'quantity_served', 'REAL DEFAULT 0'),
    ('sale_items', 'served_at', 'TEXT'),
    ('sale', 'created_at', 'TEXT'),
    ('customers', 'company_name', 'TEXT'),
    ('customers', 'phone', 'TEXT'),
    ('customers', 'address', 'TEXT'),
    ('customers', 'tax_code', 'TEXT'),
    ('customers', 'email', 'TEXT'),
    ('customers', 'budget_unit_code', 'TEXT'),
    ('customers', 'passport_no', 'TEXT'),
    ('customers', 'crm_source', 'TEXT'),
    ('customers', 'crm_owner', 'TEXT'),
    ('customers', 'crm_segment', "TEXT DEFAULT 'standard'"),
    ('customers', 'crm_lifecycle', "TEXT DEFAULT 'active'"),
    ('customers', 'crm_notes', 'TEXT'),
    ('customers', 'crm_next_contact_at', 'TEXT'),
    ('customers', 'crm_tags', 'TEXT'),
    ('customers', 'crm_created_at', 'TEXT'),
    ('customers', 'crm_updated_at', 'TEXT'),
    ('sale', 'customer_id', 'INTEGER'),
    ('business_info', 'email', 'TEXT'),
    ('business_info', 'accounting_regime', "TEXT DEFAULT 'HKD'"),
    ('business_info', 'revenue_tier_declared', 'TEXT'),
    ('business_info', 'revenue_tier_effective', 'TEXT'),
    ('business_info', 'default_hkd_sector', "TEXT DEFAULT 'G1'"),
    ('business_info', 'filing_period', "TEXT DEFAULT 'quarterly'"),
    ('business_info', 'logo_path', 'TEXT'),
    ('users', 'must_change_password', 'INTEGER DEFAULT 0'),
    ('users', 'is_support_account', 'INTEGER DEFAULT 0'),
    ('users', 'email', 'TEXT'),
    ('import', 'from_invoice_id', 'INTEGER'),
    ('import', 'doc_type', "TEXT DEFAULT 'stock'"),
    ('import_details', 'product_name', 'TEXT'),
    ('import_details', 'product_code', 'TEXT'),
    ('import_details', 'unit', 'TEXT'),
    ('import_details', 'line_type', "TEXT DEFAULT 'goods'"),
    ('import_details', 'asset_account', 'TEXT'),
    ('employees', 'attendance_code', 'TEXT'),
]


def _table_has_column(conn, table, column):
    from db.schema_helpers import column_exists
    return column_exists(conn, table, column)


def ensure_products_schema(conn):
    """Migrate cột products / sale_items trên DB tenant."""
    from db.schema_helpers import add_column_if_missing, table_cols, table_exists

    if not table_exists(conn, 'products'):
        return
    columns = table_cols(conn, 'products')
    if columns:
        for col, col_type in _PRODUCTS_COLS:
            if col not in columns:
                add_column_if_missing(conn, 'products', col, col_type)
    add_column_if_missing(conn, 'sale_items', 'hkd_sector_code', 'TEXT')
    conn.commit()


def apply_schema_migrations(conn):
    """
    Migrate schema đầy đủ cho MỘT file SQLite (main hoặc tenant).
    Gọi khi deploy, khi tenant login, hoặc sau git pull — idempotent.
    """
    c = conn.cursor()
    ensure_products_schema(conn)
    ensure_invoice_settings_schema(conn)
    try:
        from Services.fb_schema import ensure_fb_schema
        ensure_fb_schema(conn, commit=False)
    except Exception as e:
        print(f'[MIGRATE] fb schema: {e}')
    for table, col, col_type in _TENANT_TABLE_EXTRAS:
        if _table_has_column(conn, table, col):
            continue
        try:
            from db.schema_helpers import add_column_if_missing
            add_column_if_missing(conn, table, col, col_type, cursor=c)
        except sqlite3.OperationalError as e:
            print(f'[MIGRATE] Không thể thêm {table}.{col}: {e}')
    conn.commit()
    try:
        from Services.inward_invoice_helpers import ensure_import_service_schema
        ensure_import_service_schema(conn)
    except Exception as e:
        print(f'[MIGRATE] import service: {e}')
    try:
        from Services.import_line_helpers import ensure_warehouse_schema
        from Services.fixed_assets_helpers import ensure_fixed_assets_schema
        ensure_warehouse_schema(conn)
        ensure_fixed_assets_schema(conn)
    except Exception as e:
        print(f'[MIGRATE] warehouse/fixed_assets: {e}')
    try:
        from Services.payment_bank import ensure_bank_transactions_table
        ensure_bank_transactions_table(conn)
    except Exception as e:
        print(f'[MIGRATE] bank_transactions: {e}')
    try:
        from Services.attendance_helpers import ensure_attendance_schema
        ensure_attendance_schema(conn)
    except Exception as e:
        print(f'[MIGRATE] attendance: {e}')
    try:
        from Services.employee_payroll_helpers import ensure_payroll_schema
        ensure_payroll_schema(conn, commit=True)
    except Exception as e:
        print(f'[MIGRATE] payroll: {e}')
    try:
        from Services.production_costing import ensure_production_schema
        ensure_production_schema(conn)
    except Exception as e:
        print(f'[MIGRATE] production: {e}')
    try:
        from Services.pos_offline_schema import ensure_pos_offline_schema
        ensure_pos_offline_schema(conn, commit=False)
    except Exception as e:
        print(f'[MIGRATE] pos offline: {e}')
    try:
        from Services.user_branch import ensure_user_branch_schema
        ensure_user_branch_schema(conn, commit=False)
    except Exception as e:
        print(f'[MIGRATE] user_branch: {e}')
    try:
        from Services.crm_schema import ensure_crm_schema
        ensure_crm_schema(conn, commit=False)
    except Exception as e:
        print(f'[MIGRATE] crm: {e}')
    try:
        from db.dialect import is_postgres
        if not is_postgres():
            from db_utils import ensure_sqlite_wal, sqlite_db_file
            mode = ensure_sqlite_wal(conn, sqlite_db_file(conn))
            if mode and str(mode).lower() != 'wal':
                print(f'[MIGRATE] journal_mode={mode} (mong doi WAL)')
    except Exception as e:
        print(f'[MIGRATE] WAL: {e}')
    try:
        ensure_query_indexes(conn)
    except Exception as e:
        print(f'[MIGRATE] query indexes: {e}')
    conn.commit()


def ensure_query_indexes(conn) -> None:
    """Index phục vụ list API (HĐ mua, phiếu nhập, danh mục) — idempotent."""
    from db.schema_helpers import execute_ddl, table_exists

    specs = [
        ('idx_import_details_import_id', 'import_details', 'import_id'),
        ('idx_import_bill_no', 'import', 'bill_no'),
        ('idx_import_from_invoice_id', 'import', 'from_invoice_id'),
        ('idx_import_date', 'import', 'date'),
        ('idx_import_supplier_id', 'import', 'supplier_id'),
        ('idx_supplier_invoice_date', 'supplier_invoice', 'invoice_date'),
        ('idx_supplier_invoice_no', 'supplier_invoice', 'invoice_no'),
        ('idx_products_name', 'products', 'name'),
        ('idx_suppliers_name', 'suppliers', 'name'),
        ('idx_sje_posting_date', 'sme_journal_entries', 'posting_date'),
        ('idx_sjl_entry_id', 'sme_journal_lines', 'entry_id'),
        ('idx_sjl_account_code', 'sme_journal_lines', 'account_code'),
        ('idx_stock_moves_product_id', 'stock_moves', 'product_id'),
        ('idx_sale_items_sale_id', 'sale_items', 'sale_id'),
    ]
    for idx_name, table, column in specs:
        if not table_exists(conn, table):
            continue
        try:
            execute_ddl(conn, f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({column})')
        except Exception as e:
            print(f'[MIGRATE] index {idx_name}: {e}')


def ensure_tenant_db_schema(conn):
    """Alias — migrate schema tenant DB (giữ tên cũ cho middleware)."""
    apply_schema_migrations(conn)


def _discover_database_paths():
    """Liệt kê mọi file .db cần migrate: main + tenants/*.db + registry."""
    import os
    from db_utils import BASE_DIR, MAIN_DB_PATH, REGISTRY_PATH, _normalize_db_path

    paths = set()
    main = os.path.abspath(MAIN_DB_PATH)
    if os.path.isfile(main):
        paths.add(main)

    tenants_dir = os.path.join(BASE_DIR, 'tenants')
    if os.path.isdir(tenants_dir):
        for fn in os.listdir(tenants_dir):
            if fn.endswith('.db') and fn.lower() not in ('registry.db',):
                paths.add(os.path.abspath(os.path.join(tenants_dir, fn)))

    try:
        if os.path.isfile(REGISTRY_PATH):
            from db_utils import open_sqlite
            with open_sqlite(REGISTRY_PATH) as reg:
                rows = reg.execute(
                    "SELECT db_path FROM tenants WHERE db_path IS NOT NULL AND TRIM(db_path) != ''"
                ).fetchall()
            for row in rows:
                p = _normalize_db_path(row['db_path'])
                if p and os.path.isfile(p):
                    paths.add(os.path.abspath(p))
    except Exception as e:
        print(f'[MIGRATE] discover registry: {e}')

    return sorted(paths)


def _migrate_main_system_tables():
    """Bảng chỉ trên database hệ thống (registry / master)."""
    try:
        created = ensure_registry_tables()
        if created:
            print('[MIGRATE] Tao lai bang registry: %s' % ', '.join(created))
            print('[MIGRATE] Bang moi dang RONG — chay: '
                  'python scripts/repair_vps_main_db.py --apply')
    except Exception as e:
        print(f'[MIGRATE] registry tables: {e}')
    try:
        from Services.master_account import (
            count_masters,
            ensure_master_from_env,
            ensure_users_table,
        )
        conn_u = get_main_db_connection()
        try:
            changes = ensure_users_table(conn_u)
            if changes:
                print('[MIGRATE] users schema: %s' % ', '.join(changes))
            conn_u.commit()
            if count_masters(conn_u) == 0:
                action = ensure_master_from_env(conn_u)
                if action:
                    print('[MIGRATE] Da tao master tu .env (%s)' % action)
                else:
                    print('[MIGRATE] CHUA CO user master — them MASTER_PASSWORD vao .env '
                          'hoac: python scripts/ensure_master_user.py --apply --password ...')
        finally:
            conn_u.close()
    except Exception as e:
        print(f'[MIGRATE] ensure users/master: {e}')
    try:
        from Services.audit_log import ensure_audit_table
        from db.dialect import is_postgres
        from db.schema_helpers import execute_ddl

        conn2 = get_main_db_connection()
        ensure_audit_table(conn2)
        execute_ddl(conn2, """
            CREATE TABLE IF NOT EXISTS login_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login_at TEXT DEFAULT CURRENT_TIMESTAMP,
                tenant_id TEXT,
                user_id INTEGER,
                username TEXT,
                ip_address TEXT,
                location TEXT,
                device_info TEXT,
                status TEXT
            )
        """)
        conn2.commit()
        conn2.close()
    except Exception as e:
        print(f'[MIGRATE] audit_log / login_history: {e}')
    try:
        from Services.attendance_helpers import ensure_attendance_schema
        conn3 = get_main_db_connection()
        ensure_attendance_schema(conn3)
        conn3.commit()
        conn3.close()
    except Exception as e:
        print(f'[MIGRATE] main attendance: {e}')
    try:
        from Services.subscription_service import ensure_subscription_products
        ensure_subscription_products()
    except Exception as e:
        print(f'[MIGRATE] subscription products seed: {e}')
    try:
        from Services.knowledge_service import ensure_knowledge_schema, seed_default_articles
        conn_k = get_main_db_connection()
        ensure_knowledge_schema(conn_k)
        seed_default_articles(conn_k)
        conn_k.close()
    except Exception as e:
        print(f'[MIGRATE] knowledge_articles: {e}')


def migrate_all_databases(verbose=True):
    """
    Migrate schema TẤT CẢ database (main + mọi tenant).
    Chạy sau git pull trên VPS hoặc: python scripts/migrate_all_dbs.py

    Nên dừng Gunicorn (systemctl stop pos) trước khi chạy — tránh database is locked.
    """
    import os
    from db.dialect import is_postgres, pg_schema_from_db_path
    from db_utils import open_sqlite, sqlite_write_retry

    if is_postgres():
        from db.postgres_backend import ensure_pg_schema, open_pg

        ok, fail = 0, 0
        schemas = {pg_schema_from_db_path(None)}  # registry/public
        try:
            with open_pg(schema=pg_schema_from_db_path(None)) as reg:
                rows = reg.execute(
                    "SELECT tenant_id, db_path FROM tenants WHERE db_path IS NOT NULL AND TRIM(db_path) != ''"
                ).fetchall()
            for row in rows:
                tid = row['tenant_id'] if hasattr(row, 'keys') else row[0]
                dbp = row['db_path'] if hasattr(row, 'keys') else row[1]
                schemas.add(pg_schema_from_db_path(dbp, tenant_id=tid))
        except Exception as e:
            print(f'[MIGRATE] discover postgres schemas: {e!r}')
            # Fallback: suy schema từ file SQLite còn trên disk
            for path in _discover_database_paths():
                schemas.add(pg_schema_from_db_path(path))

        for schema in sorted(schemas):
            try:
                ensure_pg_schema(schema)
                with open_pg(schema=schema) as conn:
                    apply_schema_migrations(conn)
                    try:
                        conn.commit()
                    except Exception:
                        pass
                ok += 1
                if verbose:
                    print(f'[MIGRATE] OK schema {schema}')
            except Exception as e:
                fail += 1
                print(f'[MIGRATE] FAIL schema {schema}: {e!r}')
        _migrate_main_system_tables()
        if verbose:
            print(f'[MIGRATE] Done (PostgreSQL): {ok} schema(s), {fail} failed')
        return ok, fail

    paths = _discover_database_paths()
    ok, fail = 0, 0
    for path in paths:
        try:
            def _migrate_one(p=path):
                with open_sqlite(p) as conn:
                    apply_schema_migrations(conn)
                    try:
                        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    except Exception:
                        pass
                    conn.commit()

            sqlite_write_retry(_migrate_one, label='migrate:%s' % os.path.basename(path))
            ok += 1
            if verbose:
                print(f'[MIGRATE] OK {path}')
        except Exception as e:
            fail += 1
            print(f'[MIGRATE] FAIL {path}: {e!r}')
    _migrate_main_system_tables()
    if verbose:
        print(f'[MIGRATE] Done: {ok} database(s), {fail} failed')
    return ok, fail


def init_db_columns():
    conn = get_db_connection()
    try:
        ensure_tenant_db_schema(conn)
    except Exception as e:
        print("Lỗi khởi tạo cột:", e)
    finally:
        conn.close()

def init_db(bcrypt=None):
    conn = get_db_connection()
    c = conn.cursor()

# --- LƯU Ý: Cần có bảng 'products', 'customers', 'staff' để tạo khóa ngoại ---
# Dưới đây là các bảng giả định:
    c.execute("""CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS staff (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)""")

    # Bảng products
    c.execute('''CREATE TABLE IF NOT EXISTS products (
            	 id INTEGER PRIMARY KEY AUTOINCREMENT,
            	 barcode TEXT UNIQUE,
		 barcode1 TEXT UNIQUE,
                 product_code TEXT UNIQUE,
            	 name TEXT NOT NULL,
            	 unit TEXT DEFAULT 'Cái',
            	 unit1 TEXT DEFAULT 'Thùng',
		 UseSaleUnit INTEGER DEFAULT 0,
            	 buyprice REAL DEFAULT 0,
            	 base_price REAL DEFAULT 0,
              	 unit_ratio REAL DEFAULT 1,
            	 price REAL DEFAULT 0,
		 FOREIGN KEY (sale_id) REFERENCES sale(id)
            )
        ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS product_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            invoice_name TEXT NOT NULL,
            supplier_id INTEGER,
            supplier_sku TEXT,
            barcode TEXT,
            normalized_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
#Bảng Tôn Kho (Để tính giá vốn bình quân gia quyền)
    c.execute('''CREATE TABLE IF NOT EXISTS inventory (
    		 product_id INTEGER PRIMARY KEY,
    		 quantity REAL DEFAULT 0,
    		 avg_cost REAL DEFAULT 0,
    		 last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
    		 FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
	)''')
    # Bảng suppliers
    c.execute('''CREATE TABLE IF NOT EXISTS suppliers (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, name TEXT NOT NULL,
                 phone TEXT, email TEXT, address TEXT, note TEXT, tax_code TEXT)''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS Operating_Cost (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,                         -- Ngày ghi sổ (Dạng YYYY-MM-DD)
            note TEXT,                                  -- Diễn giải (Cột D)
            employee_salary REAL DEFAULT 0,             -- Chi phí nhân công (Cột 2)
            electric_cost REAL DEFAULT 0,               -- Chi phí điện (Cột 3)
            water_cost REAL DEFAULT 0,                  -- Chi phí nước (Cột 4)
            telecomunication_cost REAL DEFAULT 0,       -- Chi phí viễn thông (Cột 5)
            premise_warehouse_cost REAL DEFAULT 0,      -- Chi phí thuê mặt bằng (Cột 6)
            management_cost REAL DEFAULT 0,             -- Chi phí quản lý/VPP (Cột 7)
            other_cost REAL DEFAULT 0,                  -- Chi phí khác (Cột 8)
            total_cost REAL GENERATED ALWAYS AS (
            employee_salary + electric_cost + water_cost + 
            telecomunication_cost + premise_warehouse_cost + 
            management_cost + other_cost) VIRTUAL
        )
    ''')
    # Bảng import & import_details
    c.execute('''
        CREATE TABLE IF NOT EXISTS import (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_no TEXT UNIQUE NOT NULL,
            date TEXT,
            supplier_id INTEGER,
            bill_no TEXT,
            note TEXT,
            payment_status TEXT,
            extra_cost REAL,
            total_value REAL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS import_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty REAL NOT NULL,                    -- Số lượng nhập
            buyprice REAL DEFAULT 0,              -- Đơn giá mua chưa bao gồm thuế
            cost_price REAL DEFAULT 0,            -- Giá vốn bao gồm thuế (dùng để tính total_value trong stock_moves)
            discount REAL DEFAULT 0,              -- Chiết khấu dòng
            tax REAL DEFAULT 0,                   -- Thuế dòng
            subtotal REAL NOT NULL,               -- Tổng tiền hàng dòng (có thể là trước hoặc sau thuế tùy định nghĩa)
            payment_amt REAL NOT NULL,            -- Số tiền thực tế thanh toán cho dòng này
            FOREIGN KEY(import_id) REFERENCES import(id) ON DELETE CASCADE,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    ''')
    # === BỔ SUNG: BẢNG QUẢN LÝ SỐ THỨ TỰ PHIẾU NHẬP ===
    c.execute('''CREATE TABLE IF NOT EXISTS import_sequence (
                 id INTEGER PRIMARY KEY CHECK (id = 1),
                 current_seq INTEGER DEFAULT 0
                 )''')
    c.execute("SELECT COUNT(*) FROM import_sequence")
    if c.fetchone()[0] == 0:
        c.execute("INSERT OR REPLACE INTO import_sequence (id, current_seq) VALUES (1, 0)")

    # Bảng sale & sale_items
    c.execute('''CREATE TABLE IF NOT EXISTS sale (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, total_amount REAL, discount_pct REAL, tax_pct REAL, UseSaleUnit INTEGER DEFAULT 0,
                 payment_method TEXT, customer_name TEXT, customer_phone TEXT, status TEXT, discount_amount REAL DEFAULT 0, tax_amount REAL DEFAULT 0, 
                 invoice_number TEXT DEFAULT '', invoice_provider TEXT DEFAULT 'Tự Tạo', note TEXT DEFAULT '',
		 FOREIGN KEY (order_id) REFERENCES orders(id),
		 FOREIGN KEY (staff_id) REFERENCES staff(id),
                 FOREIGN KEY (customer_id) REFERENCES customers(id)
             )''')

    c.execute('''CREATE TABLE IF NOT EXISTS sale_items (
                 sale_id INTEGER, product_id INTEGER, quantity REAL, price REAL,
                 cost_price REAL DEFAULT 0, UseSaleUnit INTEGER DEFAULT 0, unit_ratio REAL DEFAULT 1,
                 FOREIGN KEY (sale_id) REFERENCES sale (id),
		 FOREIGN KEY (order_id) REFERENCES orders(id),
                 FOREIGN KEY (product_id) REFERENCES products (id))''')
    # Bảng trả hàng (IMPORT/SALE)
    c.execute('''CREATE TABLE IF NOT EXISTS return_import (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, import_id INTEGER,
                 product_id INTEGER, quantity REAL, reason TEXT,
                 cost_price REAL DEFAULT 0, refund_amount REAL DEFAULT 0,
                 FOREIGN KEY (import_id) REFERENCES import (id),
                 FOREIGN KEY (product_id) REFERENCES products (id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS return_sales (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, sale_id INTEGER,
                 product_id INTEGER, quantity REAL, reason TEXT,
                 FOREIGN KEY (sale_id) REFERENCES sale (id),
                 FOREIGN KEY (product_id) REFERENCES products (id))''')
    # Bảng users & settings
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                 password TEXT NOT NULL, role TEXT DEFAULT 'user', full_name TEXT,
                 permissions TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')))''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
                 key TEXT PRIMARY KEY, value TEXT)''')

    # Bảng stock_moves để lưu lịch sử nhập/xuất
    c.execute("""
        CREATE TABLE IF NOT EXISTS stock_moves (
       	    id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            type TEXT NOT NULL, -- 'IMPORT', 'SALE', 'RETURN_SALE', 'RETURN_IMPORT', 'ADJUST'
            unit TEXT,
            ref_document TEXT NOT NULL,
            ref_id INTEGER,
            ref_no TEXT,
            in_quantity REAL DEFAULT 0,
            out_quantity REAL DEFAULT 0,
            quantity REAL NOT NULL,
            avg_cost REAL DEFAULT 0,
            cost_price REAL DEFAULT 0,
            total_value REAL NOT NULL, -- Đã sửa: NOT NUL -> NOT NULL
            note TEXT,
            FOREIGN KEY (product_id) REFERENCES products(id)
      )
    """)

    # --- INDEX TỐI ƯU ---
    stock_moves_index_sql = """
    CREATE INDEX IF NOT EXISTS idx_stock_moves_product_date ON stock_moves (product_id, date);
    """

    # === BỔ SUNG: BẢNG INVENTORY TRANSACTIONS MỚI ===
    c.execute('''CREATE TABLE IF NOT EXISTS inventory_transactions (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 product_id INTEGER NOT NULL,
                 type TEXT NOT NULL CHECK(type IN ('import', 'export', 'adjust')),
		 type1 TEXT,
                 quantity INTEGER NOT NULL,
                 reference_id INTEGER,
                 reference_type TEXT,
                 cost_price REAL NOT NULL DEFAULT 0,
                 total_value REAL NOT NULL DEFAULT 0,
                 import_id INTEGER,             -- <--- ĐÃ THÊM: Liên kết tới phiếu nhập
                 sale_id INTEGER,               -- <--- ĐÃ THÊM: Liên kết tới hóa đơn bán
                 return_sale_id INTEGER,        -- <--- ĐÃ THÊM: Liên kết tới phiếu trả hàng bán
                 note TEXT,
                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                 FOREIGN KEY (product_id) REFERENCES products (id),
                 FOREIGN KEY (import_id) REFERENCES import (id),
                 FOREIGN KEY (sale_id) REFERENCES sale (id),
                 FOREIGN KEY (return_sale_id) REFERENCES return_sales (id)
    )''')

    print("Khởi tạo database thành công! Đăng nhập: admin / admin123")
    
    # Index để query nhanh
    c.execute('''CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory_transactions(product_id);''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_inventory_date ON inventory_transactions(created_at);''')
    # =================================================

   # === THÊM BẢNG MỚI – KẾ TOÁN HKD ===
    c.execute('''CREATE TABLE IF NOT EXISTS voucher_seq (
                 type TEXT PRIMARY KEY, seq INTEGER DEFAULT 0)''')
    c.execute("INSERT OR IGNORE INTO voucher_seq (type, seq) VALUES ('PT', 0), ('PC', 0), ('PN', 0), ('PX', 0)")

    # 5 CHỨNG TỪ
    c.execute('''CREATE TABLE IF NOT EXISTS phieu_thu (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
            so_phieu TEXT NOT NULL UNIQUE,
            ngay_lap DATE NOT NULL,
            nguoi_nop TEXT NOT NULL,
            dia_chi TEXT,
            ly_do_nop TEXT NOT NULL,
            so_tien INTEGER NOT NULL,
            hinh_thuc TEXT DEFAULT 'Tiền mặt',
            kem_theo TEXT,
            nguoi_lap TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT,
            customer_name TEXT,
            customer_phone TEXT,
	    payment_method TEXT,
	    discount_amount REAL DEFAULT 0,
	    tax_amount REAL DEFAULT 0,
            total_amount REAL,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	    FOREIGN KEY (staff_id) REFERENCES staff(id)
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )
    ''')

    c.execute('''CREATE TABLE IF NOT EXISTS phieu_chi (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 voucher_no TEXT UNIQUE,
                 date TEXT,
                 receiver_name TEXT,
                 address TEXT,
                 reason TEXT,
		 reference_document,
		 preparer,
                 amount REAL,
                 source_id INTEGER,
                 source_type TEXT DEFAULT 'manual')''')

    c.execute('''CREATE TABLE IF NOT EXISTS phieu_nhap_kho (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 voucher_no TEXT UNIQUE,
                 date TEXT,
                 supplier_name TEXT,
                 items_json TEXT,
                 total_amount REAL,
                 import_id INTEGER)''')

    c.execute('''CREATE TABLE IF NOT EXISTS phieu_xuat_kho (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 voucher_no TEXT UNIQUE,
                 date TEXT,
                 customer_name TEXT,
                 items_json TEXT,
                 total_amount REAL,
                 sale_id INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bang_luong (
             	id INTEGER PRIMARY KEY AUTOINCREMENT,
             	period TEXT,
            	 employee_name TEXT,
             	gross_salary REAL,
             	bhxh REAL DEFAULT 0,
             	bhyt REAL DEFAULT 0,
             	bhtn REAL DEFAULT 0,
            	 other_deductions REAL DEFAULT 0,
             	total_deductions REAL,
             	net_pay REAL,
            	 paid_date TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS so_theo_doi_tien_luong (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 period TEXT,
                 employee_name TEXT,
                 gross_salary REAL,
                 deductions REAL,
                 net_pay REAL,
                 paid_date TEXT)''')

    # 7 SỔ KẾ TOÁN
    c.execute('''CREATE TABLE IF NOT EXISTS so_chi_tiet_doanh_thu (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 period TEXT,
                 date TEXT,
                 voucher_no TEXT,
                 description TEXT,
                 revenue REAL,
                 vat REAL,
                 pit REAL,
                 total_tax REAL)''')

    c.execute('''CREATE TABLE IF NOT EXISTS so_chi_tiet_hang_hoa (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 period TEXT,
                 product_name TEXT,
                 unit TEXT,
                 begin_qty REAL,
                 import_qty REAL,
                 export_qty REAL,
                 end_qty REAL,
                 begin_value REAL,
                 end_value REAL)''')

    c.execute('''CREATE TABLE IF NOT EXISTS so_quy_tien_mat (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  period TEXT,
                  date TEXT,
                  voucher_no TEXT,
                  type TEXT,
                  amount REAL,
                  balance REAL)''')

    # Sổ tiền gửi ngân hàng (Đã thêm voucher_no và type)
    c.execute('''CREATE TABLE IF NOT EXISTS so_tien_gui_ngan_hang (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  period TEXT,
                  date TEXT,
                  voucher_no TEXT,             
                  type TEXT,                   
                  description TEXT,
                  amount REAL,
                  bank_name TEXT,
                  balance REAL)''')

    # Admin mặc định
    if bcrypt is not None:
        c.execute("SELECT COUNT(*) FROM users WHERE username='admin'")
        if c.fetchone()[0] == 0:
            pwd = bcrypt.generate_password_hash('admin123').decode('utf-8')
            c.execute("INSERT INTO users (username, password, role, full_name) VALUES (?, ?, ?, ?)",
                      ('admin', pwd, 'admin', 'Quản trị viên'))
    conn.commit()
    conn.close()

    migrate_database()

def migrate_database():
    """Migrate schema — gọi lúc khởi động app và sau deploy."""
    migrate_all_databases(verbose=True)

