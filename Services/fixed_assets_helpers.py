"""TSCĐ (fixed_assets) và CCDC (tools_supplies) — tách khỏi tồn kho bán hàng."""
import logging
import sqlite3

FIXED_ASSETS_TABLE = 'fixed_assets'
TOOLS_TABLE = 'tools_supplies'
LEGACY_TABLE = 'tai_san_co_dinh'

STATUS_IN_STOCK = 'InStock'
STATUS_ACTIVE = 'Active'
STATUS_DISPOSED = 'Disposed'


def _table_exists(c, name):
    row = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _columns(c, table):
    c.execute(f'PRAGMA table_info({table})')
    return {row[1] for row in c.fetchall()}


def _add_col(c, table, col, col_type):
    cols = _columns(c, table)
    if col not in cols:
        try:
            c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}')
        except sqlite3.OperationalError as exc:
            logging.warning('add column %s.%s: %s', table, col, exc)


def ensure_fixed_assets_schema(conn):
    """Đổi tên tai_san_co_dinh → fixed_assets và bổ sung cột liên kết import."""
    c = conn.cursor()

    if _table_exists(c, LEGACY_TABLE) and not _table_exists(c, FIXED_ASSETS_TABLE):
        c.execute(f'ALTER TABLE {LEGACY_TABLE} RENAME TO {FIXED_ASSETS_TABLE}')

    if not _table_exists(c, FIXED_ASSETS_TABLE):
        c.execute(f"""
            CREATE TABLE {FIXED_ASSETS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ma_tai_san TEXT NOT NULL,
                ten_tai_san TEXT NOT NULL,
                voucher_no TEXT,
                ngay_chung_tu TEXT,
                gia_mua_chua_thue REAL DEFAULT 0,
                nguyen_gia_tinh_khau_hao REAL DEFAULT 0,
                thue_gtgt REAL DEFAULT 0,
                co_duoc_khau_tru_thue INTEGER DEFAULT 0,
                ngay_bat_dau_su_dung TEXT,
                so_thang_khau_hao INTEGER DEFAULT 36,
                stock_move_id INTEGER,
                tinh_trang TEXT DEFAULT '{STATUS_IN_STOCK}',
                product_id INTEGER,
                import_id INTEGER,
                import_detail_id INTEGER,
                warehouse_code TEXT,
                so_luong REAL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    for col, typ in (
        ('product_id', 'INTEGER'),
        ('import_id', 'INTEGER'),
        ('import_detail_id', 'INTEGER'),
        ('warehouse_code', "TEXT DEFAULT 'KHO_001'"),
        ('so_luong', 'REAL DEFAULT 1'),
        ('created_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
        ('branch_code', 'TEXT'),
        ('expense_account', "TEXT DEFAULT '642'"),
    ):
        _add_col(c, FIXED_ASSETS_TABLE, col, typ)

    if not _table_exists(c, TOOLS_TABLE):
        c.execute(f"""
            CREATE TABLE {TOOLS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ma_ccdc TEXT NOT NULL,
                ten_ccdc TEXT NOT NULL,
                voucher_no TEXT,
                ngay_nhap TEXT,
                gia_mua_chua_thue REAL DEFAULT 0,
                nguyen_gia REAL DEFAULT 0,
                thue_gtgt REAL DEFAULT 0,
                so_luong REAL DEFAULT 1,
                so_thang_phan_bo INTEGER DEFAULT 12,
                ngay_bat_dau_su_dung TEXT,
                tinh_trang TEXT DEFAULT '{STATUS_IN_STOCK}',
                product_id INTEGER,
                import_id INTEGER,
                import_detail_id INTEGER,
                warehouse_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    for col, typ in (
        ('product_id', 'INTEGER'),
        ('import_id', 'INTEGER'),
        ('import_detail_id', 'INTEGER'),
        ('warehouse_code', "TEXT DEFAULT 'KHO_001'"),
        ('created_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
        ('branch_code', 'TEXT'),
        ('so_thang_phan_bo', 'INTEGER DEFAULT 12'),
        ('ngay_bat_dau_su_dung', 'TEXT'),
        ('expense_account', "TEXT DEFAULT '642'"),
    ):
        _add_col(c, TOOLS_TABLE, col, typ)


def register_fixed_asset_from_import(
    c,
    *,
    import_id,
    import_detail_id,
    product_id,
    product_code,
    product_name,
    import_no,
    import_date,
    warehouse_code,
    qty,
    buyprice,
    tax_amount,
    discount_amount,
    line_total,
    subtotal,
    so_thang_khau_hao: int | None = None,
    ngay_bat_dau_su_dung: str | None = None,
    capitalized_cost=None,
):
    """Ghi nhận TSCĐ khi nhập kho — không qua tồn POS.

    Nguyên giá tính khấu hao = giá vốn hóa (CIF − CK + thuế NK + TTĐB),
    không gồm VAT đầu vào khấu trừ.
    """
    ensure_fixed_assets_schema(c.connection)
    ma_ts = (product_code or f'TSCD{product_id:04d}').strip()
    qty_f = float(qty or 1) or 1.0
    unit_ex_vat = float(buyprice or 0)
    base_val = float(subtotal or 0) - float(discount_amount or 0)
    # Ưu tiên giá vốn hóa từ phiếu nhập (đã gồm thuế NK / TTĐB)
    if capitalized_cost is not None and float(capitalized_cost or 0) > 0:
        nguyen_gia = round(float(capitalized_cost), 2)
    elif unit_ex_vat > 0:
        nguyen_gia = round(unit_ex_vat * qty_f, 2)
    elif base_val > 0:
        nguyen_gia = round(base_val, 2)
    else:
        # line_total đôi khi gồm VAT — trừ đi nếu có
        lt = float(line_total or 0)
        vat = float(tax_amount or 0)
        nguyen_gia = round(max(0.0, lt - vat) if vat > 0 and lt >= vat else lt, 2)
    start_date = (ngay_bat_dau_su_dung or import_date or '')[:10]
    if not start_date:
        from datetime import date as _date
        start_date = _date.today().isoformat()
    months = int(so_thang_khau_hao or 36)
    if months <= 0:
        months = 36

    branch = 'HQ'
    try:
        from Services.sme.branches import get_warehouse_branch_code
        branch = get_warehouse_branch_code(c.connection, warehouse_code or '')
    except Exception:
        pass

    cols = _columns(c, FIXED_ASSETS_TABLE)
    data = {
        'ma_tai_san': ma_ts,
        'ten_tai_san': product_name,
        'voucher_no': import_no,
        'ngay_chung_tu': import_date,
        'gia_mua_chua_thue': float(buyprice or 0),
        'nguyen_gia_tinh_khau_hao': nguyen_gia,
        'thue_gtgt': float(tax_amount or 0),
        'ngay_bat_dau_su_dung': start_date,
        'so_thang_khau_hao': months,
        'tinh_trang': STATUS_IN_STOCK,
        'product_id': product_id,
        'import_id': import_id,
        'import_detail_id': import_detail_id,
        'warehouse_code': warehouse_code or 'KHO_001',
        'so_luong': float(qty or 1),
        'branch_code': branch,
    }
    # Một số DB cũ dùng so_chung_tu_kho thay voucher_no
    if 'voucher_no' not in cols and 'so_chung_tu_kho' in cols:
        data['so_chung_tu_kho'] = data.pop('voucher_no')
    fields = [k for k in data if k in cols]
    placeholders = ','.join('?' for _ in fields)
    c.execute(
        f"INSERT INTO {FIXED_ASSETS_TABLE} ({','.join(fields)}) VALUES ({placeholders})",
        [data[k] for k in fields],
    )
    return c.lastrowid


def register_tool_from_import(
    c,
    *,
    import_id,
    import_detail_id,
    product_id,
    product_code,
    product_name,
    import_no,
    import_date,
    warehouse_code,
    qty,
    buyprice,
    tax_amount,
    line_total,
    subtotal,
    discount_amount,
    capitalized_cost=None,
):
    """Ghi nhận CCDC khi nhập kho — nguyên giá = giá vốn hóa (gồm NK/TTĐB, không VAT)."""
    ensure_fixed_assets_schema(c.connection)
    ma = (product_code or f'CCDC{product_id:04d}').strip()
    qty_f = float(qty or 1) or 1.0
    unit_ex_vat = float(buyprice or 0)
    base_val = float(subtotal or 0) - float(discount_amount or 0)
    if capitalized_cost is not None and float(capitalized_cost or 0) > 0:
        nguyen_gia = round(float(capitalized_cost), 2)
    elif unit_ex_vat > 0:
        nguyen_gia = round(unit_ex_vat * qty_f, 2)
    elif base_val > 0:
        nguyen_gia = round(base_val, 2)
    else:
        lt = float(line_total or 0)
        vat = float(tax_amount or 0)
        nguyen_gia = round(max(0.0, lt - vat) if vat > 0 and lt >= vat else lt, 2)
    branch = 'HQ'
    try:
        from Services.sme.branches import get_warehouse_branch_code
        branch = get_warehouse_branch_code(c.connection, warehouse_code or '')
    except Exception:
        pass

    c.execute(f"""
        INSERT INTO {TOOLS_TABLE} (
            ma_ccdc, ten_ccdc, voucher_no, ngay_nhap,
            gia_mua_chua_thue, nguyen_gia, thue_gtgt,
            so_luong, so_thang_phan_bo, tinh_trang, product_id, import_id, import_detail_id,
            warehouse_code, branch_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ma,
        product_name,
        import_no,
        import_date,
        float(buyprice or 0),
        nguyen_gia,
        float(tax_amount or 0),
        float(qty or 1),
        12,
        STATUS_IN_STOCK,
        product_id,
        import_id,
        import_detail_id,
        warehouse_code or 'KHO_001',
        branch,
    ))
    return c.lastrowid


def fixed_assets_table(conn):
    ensure_fixed_assets_schema(conn)
    return FIXED_ASSETS_TABLE


def delete_assets_by_import_id(c, import_id):
    """Xóa bản ghi TSCĐ/CCDC gắn với phiếu nhập (chỉ InStock — chưa kích hoạt)."""
    ensure_fixed_assets_schema(c.connection)
    c.execute(f"""
        DELETE FROM {FIXED_ASSETS_TABLE}
        WHERE import_id = ? AND tinh_trang = ?
    """, (import_id, STATUS_IN_STOCK))
    fa_deleted = c.rowcount
    c.execute(f"""
        DELETE FROM {TOOLS_TABLE}
        WHERE import_id = ? AND tinh_trang = ?
    """, (import_id, STATUS_IN_STOCK))
    tools_deleted = c.rowcount
    return fa_deleted, tools_deleted


def count_active_assets_by_import_id(c, import_id):
    """Đếm TSCĐ/CCDC đã kích hoạt — không cho xóa phiếu nhập."""
    ensure_fixed_assets_schema(c.connection)
    c.execute(f"""
        SELECT COUNT(*) FROM {FIXED_ASSETS_TABLE}
        WHERE import_id = ? AND tinh_trang = ?
    """, (import_id, STATUS_ACTIVE))
    fa_active = c.fetchone()[0] or 0
    c.execute(f"""
        SELECT COUNT(*) FROM {TOOLS_TABLE}
        WHERE import_id = ? AND tinh_trang = ?
    """, (import_id, STATUS_ACTIVE))
    tools_active = c.fetchone()[0] or 0
    return fa_active, tools_active
