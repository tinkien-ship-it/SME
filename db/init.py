"""Khoi tao va migrate schema SQLite."""
import sqlite3

from db_utils import get_db_connection, get_main_db_connection

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
    ('is_active', 'INTEGER DEFAULT 0'),
    ('updated_at', 'TEXT'),
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
    ('hkd_sector_code', 'TEXT'),
    ('is_subscription_plan', 'INTEGER DEFAULT 0'),
    ('has_einvoice', 'INTEGER DEFAULT 0'),
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
    ('sale', 'email', 'TEXT'),
    ('sale', 'company_name', 'TEXT'),
    ('sale', 'sale_no', 'TEXT'),
    ('sale', 'customer_phone', 'TEXT'),
    ('sale', 'address', 'TEXT'),
    ('sale', 'tax_code', 'TEXT'),
    ('sale', 'budget_unit_code', 'TEXT'),
    ('sale', 'passport_no', 'TEXT'),
    ('customers', 'company_name', 'TEXT'),
    ('customers', 'phone', 'TEXT'),
    ('customers', 'address', 'TEXT'),
    ('customers', 'tax_code', 'TEXT'),
    ('customers', 'email', 'TEXT'),
    ('customers', 'budget_unit_code', 'TEXT'),
    ('customers', 'passport_no', 'TEXT'),
    ('business_info', 'email', 'TEXT'),
    ('business_info', 'accounting_regime', "TEXT DEFAULT 'HKD'"),
    ('business_info', 'revenue_tier_declared', 'TEXT'),
    ('business_info', 'revenue_tier_effective', 'TEXT'),
    ('business_info', 'default_hkd_sector', "TEXT DEFAULT 'G1'"),
    ('business_info', 'filing_period', "TEXT DEFAULT 'quarterly'"),
    ('users', 'must_change_password', 'INTEGER DEFAULT 0'),
    ('users', 'is_support_account', 'INTEGER DEFAULT 0'),
    ('users', 'email', 'TEXT'),
    ('import', 'from_invoice_id', 'INTEGER'),
    ('import', 'doc_type', "TEXT DEFAULT 'stock'"),
    ('import_details', 'product_name', 'TEXT'),
    ('import_details', 'product_code', 'TEXT'),
    ('import_details', 'unit', 'TEXT'),
    ('import_details', 'line_type', "TEXT DEFAULT 'goods'"),
    ('employees', 'attendance_code', 'TEXT'),
]


def _table_has_column(cursor, table, column):
    try:
        cursor.execute(f'PRAGMA table_info({table})')
        return column in {r[1] for r in cursor.fetchall()}
    except Exception:
        return False


def ensure_products_schema(conn):
    """Migrate cột products / sale_items trên DB tenant."""
    c = conn.cursor()
    c.execute('PRAGMA table_info(products)')
    columns = {col[1] for col in c.fetchall()}
    if columns:
        for col, col_type in _PRODUCTS_COLS:
            if col not in columns:
                try:
                    c.execute(f'ALTER TABLE products ADD COLUMN {col} {col_type}')
                except sqlite3.OperationalError as e:
                    print(f'[MIGRATE] products.{col}: {e}')
    c.execute('PRAGMA table_info(sale_items)')
    si_columns = {col[1] for col in c.fetchall()}
    if si_columns and 'hkd_sector_code' not in si_columns:
        try:
            c.execute('ALTER TABLE sale_items ADD COLUMN hkd_sector_code TEXT')
        except sqlite3.OperationalError as e:
            print(f'[MIGRATE] sale_items.hkd_sector_code: {e}')
    conn.commit()


def ensure_tenant_db_schema(conn):
    """Migrate schema đầy đủ cho DB tenant (gọi lần đầu khi tenant truy cập)."""
    c = conn.cursor()
    ensure_products_schema(conn)
    ensure_invoice_settings_schema(conn)
    for table, col, col_type in _TENANT_TABLE_EXTRAS:
        if _table_has_column(c, table, col):
            continue
        try:
            c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}')
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
    conn.commit()


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
    conn = get_db_connection()
    c = conn.cursor()
    """
    Thêm cột nếu cần. Tránh lỗi ALTER TABLE khi cột đã tồn tại.
    """
    # helper: check column exists
    def has_column(table, column):
        try:
            c.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in c.fetchall()]
            return column in cols
        except Exception:
            return False
    # columns to ensure
    extras = [
        ('import', 'bill_no', "TEXT"),
        ('suppliers', 'tax_code', "TEXT"),
        ('sale', 'invoice_number', "TEXT"),
        ('sale', 'invoice_provider', "TEXT"),
        ('return_import', 'cost_price', "REAL"),
        ('return_import', 'refund_amount', "REAL"),
        ('users', 'permissions', "TEXT"),
        ('products', 'product_type', "TEXT DEFAULT 'goods'"),
        ('products', 'hkd_sector_code', "TEXT"),
        ('products', 'is_subscription_plan', "INTEGER DEFAULT 0"),
        ('products', 'has_einvoice', "INTEGER DEFAULT 0"),
        ('sale_items', 'hkd_sector_code', "TEXT"),
        ('sale', 'business_line', "TEXT"),
        ('sale', 'email', "TEXT"),
        ('sale', 'company_name', "TEXT"),
        ('sale', 'sale_no', "TEXT"),
        ('sale', 'customer_phone', "TEXT"),
        ('sale', 'address', "TEXT"),
        ('sale', 'tax_code', "TEXT"),
        ('sale', 'budget_unit_code', "TEXT"),
        ('sale', 'passport_no', "TEXT"),
        ('customers', 'company_name', "TEXT"),
        ('customers', 'phone', "TEXT"),
        ('customers', 'address', "TEXT"),
        ('customers', 'tax_code', "TEXT"),
        ('customers', 'email', "TEXT"),
        ('customers', 'budget_unit_code', "TEXT"),
        ('customers', 'passport_no', "TEXT"),
        ('business_info', 'email', "TEXT"),
        ('business_info', 'accounting_regime', "TEXT DEFAULT 'HKD'"),
        ('business_info', 'revenue_tier_declared', 'TEXT'),
        ('business_info', 'revenue_tier_effective', 'TEXT'),
        ('business_info', 'default_hkd_sector', "TEXT DEFAULT 'G1'"),
        ('business_info', 'filing_period', "TEXT DEFAULT 'quarterly'"),
        ('users', 'must_change_password', "INTEGER DEFAULT 0"),
        ('users', 'is_support_account', "INTEGER DEFAULT 0"),
        ('users', 'email', "TEXT"),
        ('import', 'from_invoice_id', 'INTEGER'),
        ('import', 'doc_type', "TEXT DEFAULT 'stock'"),
        ('import_details', 'product_name', 'TEXT'),
        ('import_details', 'product_code', 'TEXT'),
        ('import_details', 'unit', 'TEXT'),
        ('import_details', 'line_type', "TEXT DEFAULT 'goods'"),
        ('employees', 'attendance_code', 'TEXT'),
    ]
    ensure_invoice_settings_schema(conn)

    for table, col, col_type in extras:
        if not has_column(table, col):
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as e:
                print(f"[MIGRATE] Không thể thêm {col} vào {table}: {e}")
    conn.commit()
    try:
        from Services.inward_invoice_helpers import (
            ensure_import_service_schema,
            migrate_import_details_for_service,
            migrate_import_for_service,
        )
        ensure_import_service_schema(conn)
        conn.commit()
    except Exception as e:
        print(f"[MIGRATE] import service columns: {e}")
    try:
        from Services.import_line_helpers import ensure_warehouse_schema
        from Services.fixed_assets_helpers import ensure_fixed_assets_schema
        ensure_warehouse_schema(conn)
        ensure_fixed_assets_schema(conn)
        conn.commit()
    except Exception as e:
        print(f"[MIGRATE] warehouse/fixed_assets schema: {e}")
    try:
        from Services.payment_bank import ensure_bank_transactions_table
        ensure_bank_transactions_table(conn)
        conn.commit()
    except Exception as e:
        print(f"[MIGRATE] bank_transactions table: {e}")
    conn.close()
    try:
        from Services.audit_log import ensure_audit_table
        conn2 = get_main_db_connection()
        ensure_audit_table(conn2)
        conn2.execute("""
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
        print(f"[MIGRATE] audit_log table: {e}")
    try:
        from Services.attendance_helpers import ensure_attendance_schema
        conn3 = get_main_db_connection()
        ensure_attendance_schema(conn3)
        conn3.commit()
        conn3.close()
    except Exception as e:
        print(f"[MIGRATE] attendance schema: {e}")
    try:
        from Services.subscription_service import ensure_subscription_products
        ensure_subscription_products()
    except Exception as e:
        print(f"[MIGRATE] Seed gói DV001-DV004: {e}")

