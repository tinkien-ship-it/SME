"""Mã sản phẩm theo loại hàng (HKD import)."""
import sqlite3

# Tiền tố mã khi nhập kho / tạo danh mục:
#   Hàng hóa (mua để bán) → HH    Thành phẩm → SP
#   Vật tư → VT    TSCĐ → TSCD    CCDC → CCDC    Dịch vụ → DV
# Mã cũ SP* (hàng hóa) và TP* (thành phẩm) vẫn giữ nếu đã gán.
_CODE_SPEC = {
    'materials': ('VT', 4),
    'material': ('VT', 4),
    'raw_materials': ('VT', 4),
    'nvl': ('VT', 4),
    'finished_goods': ('SP', 4),
    'finished': ('SP', 4),
    'thanh_pham': ('SP', 4),
    'thanhpham': ('SP', 4),
    'goods': ('HH', 4),
    'hang_hoa': ('HH', 4),
    'fixed_asset': ('TSCD', 4),
    'tscd': ('TSCD', 4),
    'tools': ('CCDC', 4),
    'tool': ('CCDC', 4),
    'ccdc': ('CCDC', 4),
    'service': ('DV', 3),
    'services': ('DV', 3),
    'dich_vu': ('DV', 3),
}


def product_code_spec(product_type) -> tuple[str, int] | None:
    pt = (product_type or 'goods').strip().lower()
    return _CODE_SPEC.get(pt)


def _max_seq_with_prefix(c, prefix, digit_width=4):
    px = prefix.upper()
    rows = c.execute(
        "SELECT product_code FROM products WHERE UPPER(product_code) LIKE ? ORDER BY product_code DESC",
        (f"{px}%",),
    ).fetchall()
    max_num = 0
    plen = len(px)
    for row in rows:
        code = (row[0] or '').strip().upper()
        if code.startswith(px):
            suffix = code[plen:]
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
    return f"{px}{max_num + 1:0{digit_width}d}"


def peek_next_product_code(c, product_type):
    spec = product_code_spec(product_type)
    if not spec:
        return None
    prefix, width = spec
    return _max_seq_with_prefix(c, prefix, width)


def assign_product_codes(c, product_id, product_type, unit1=None,
                         external_barcode=None, external_barcode1=None):
    """Gán product_code + barcode sau INSERT/upsert.

    Tem NSX (external_barcode) được ưu tiên; không ghi đè mã NSX đã lưu.
    Không có tem → sinh mã nội bộ (HH/SP/VT…) + đuôi 01/02.
    """
    from Services.product_barcode import (
        barcode_owned_by_other,
        canonical_scan_code,
        is_internal_barcode,
        same_scan_code,
    )

    pt = (product_type or 'goods').strip().lower()
    row = c.execute(
        "SELECT product_code, barcode, barcode1 FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    existing_code = ''
    existing_bc = ''
    existing_b1 = ''
    if row:
        existing_code = (row['product_code'] if hasattr(row, 'keys') else row[0]) or ''
        existing_bc = (row['barcode'] if hasattr(row, 'keys') else row[1]) or ''
        existing_b1 = (row['barcode1'] if hasattr(row, 'keys') else row[2]) or ''
    existing_code = str(existing_code).strip()
    existing_bc = str(existing_bc).strip()
    existing_b1 = str(existing_b1).strip()

    if existing_code:
        code = existing_code
    else:
        spec = product_code_spec(pt)
        if spec:
            code = _max_seq_with_prefix(c, spec[0], spec[1])
        else:
            code = _max_seq_with_prefix(c, 'HH', 4)

    gen_bc = code if pt in ('fixed_asset', 'tools', 'service') else f"{code}01"
    gen_b1 = f"{code}02" if unit1 and pt not in ('fixed_asset', 'tools', 'service') else None

    conn = getattr(c, 'connection', None) or c
    ext = canonical_scan_code(external_barcode)
    if ext and same_scan_code(ext, existing_bc):
        barcode = existing_bc or ext
    elif ext and same_scan_code(ext, existing_b1):
        barcode = existing_bc or ext
    elif ext:
        other = barcode_owned_by_other(conn, ext, exclude_id=product_id)
        if other:
            if existing_bc:
                barcode = existing_bc
            else:
                other_name = other['name'] if hasattr(other, 'keys') else other[1]
                other_code = (other['product_code'] if hasattr(other, 'keys') else other[4]) or other[0]
                raise ValueError(
                    f"Mã vạch '{ext}' đã gắn sản phẩm {other_code} — {other_name}"
                )
        else:
            barcode = ext
    elif existing_bc:
        barcode = existing_bc
    else:
        barcode = gen_bc

    ext1 = canonical_scan_code(external_barcode1)
    if ext1 and (same_scan_code(ext1, barcode) or ext1 == barcode):
        barcode1 = existing_b1 or None
    elif ext1 and same_scan_code(ext1, existing_b1):
        barcode1 = existing_b1 or ext1
    elif ext1:
        other = barcode_owned_by_other(conn, ext1, exclude_id=product_id)
        if other:
            if existing_b1:
                barcode1 = existing_b1
            else:
                other_name = other['name'] if hasattr(other, 'keys') else other[1]
                other_code = (other['product_code'] if hasattr(other, 'keys') else other[4]) or other[0]
                raise ValueError(
                    f"Mã vạch sỉ '{ext1}' đã gắn sản phẩm {other_code} — {other_name}"
                )
        else:
            barcode1 = ext1
    elif existing_b1:
        barcode1 = existing_b1
    elif unit1 and not is_internal_barcode(barcode, code):
        # Có tem NSX ở ĐV lẻ — không tự bịa barcode1; để trống đến khi quét tem thùng
        barcode1 = None
    else:
        barcode1 = gen_b1

    c.execute(
        "UPDATE products SET product_code=?, barcode=?, barcode1=? WHERE id=?",
        (code, barcode, barcode1, product_id),
    )
    return code, barcode, barcode1


DEFAULT_WAREHOUSES = (
    ('KHO_001', 'Kho trung tâm', 1),
    ('KHO_002', 'Kho 2', 0),
    ('KHO_003', 'Kho 3', 0),
)

# Dòng ghi nhận tồn kho bán hàng / WAC (156, 152…)
INVENTORY_TRACKED_LINE_TYPES = frozenset({'goods', 'materials'})

# TSCĐ / CCDC: sổ riêng, không qua tồn POS
ASSET_REGISTER_LINE_TYPES = frozenset({'fixed_asset', 'tools'})


def tracks_retail_inventory(line_type):
    return (line_type or 'goods').strip().lower() in INVENTORY_TRACKED_LINE_TYPES


def _migrate_legacy_warehouse_codes(c):
    """Đổi MAIN→KHO_001, KHO-CN2→KHO_002 rồi xóa mã cũ nếu đã có kho chuẩn."""
    mapping = (
        ('MAIN', 'KHO_001'),
        ('KHO-CN2', 'KHO_002'),
        ('KHO_CN2', 'KHO_002'),
    )
    tables_cols = (
        ('warehouses', None),  # handled separately
        ('import', 'warehouse_code'),
        ('import_details', 'warehouse_code'),
        ('stock_moves', 'warehouse_code'),
        ('sale', 'warehouse_code'),
    )
    for old, new in mapping:
        has_old = c.execute(
            'SELECT 1 FROM warehouses WHERE UPPER(code) = ? LIMIT 1', (old.upper(),)
        ).fetchone()
        has_new = c.execute(
            'SELECT 1 FROM warehouses WHERE code = ? LIMIT 1', (new,)
        ).fetchone()
        if not has_old or not has_new:
            continue
        for table, col in tables_cols:
            if col is None:
                continue
            try:
                cols = {r[1] for r in c.execute(f'PRAGMA table_info("{table}")').fetchall()}
            except sqlite3.Error:
                continue
            if col not in cols:
                continue
            try:
                c.execute(
                    f'UPDATE "{table}" SET {col} = ? WHERE UPPER(COALESCE({col}, \'\')) = ?',
                    (new, old.upper()),
                )
            except sqlite3.Error:
                pass
        # Giữ branch_code của kho cũ nếu kho mới chưa gắn
        try:
            wh_cols = {r[1] for r in c.execute('PRAGMA table_info(warehouses)').fetchall()}
            if 'branch_code' in wh_cols:
                old_br = c.execute(
                    'SELECT branch_code FROM warehouses WHERE UPPER(code) = ?',
                    (old.upper(),),
                ).fetchone()
                new_br = c.execute(
                    'SELECT branch_code FROM warehouses WHERE code = ?', (new,)
                ).fetchone()
                if old_br and (old_br[0] or '').strip():
                    old_bc = (old_br[0] or '').strip().upper()
                    new_bc = ((new_br[0] if new_br else '') or '').strip().upper()
                    if old_bc and old_bc != 'HQ' and (not new_bc or new_bc == 'HQ'):
                        c.execute(
                            'UPDATE warehouses SET branch_code = ? WHERE code = ?',
                            (old_bc, new),
                        )
        except sqlite3.Error:
            pass
        c.execute('DELETE FROM warehouses WHERE UPPER(code) = ?', (old.upper(),))


_WAREHOUSE_SCHEMA_VERSION = '2026-08-03g'
_warehouse_schema_ready: dict[str, str] = {}


def _warehouse_db_key(conn) -> str:
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
        if row:
            path = row[2] if not hasattr(row, 'keys') else row['file']
            if path:
                return str(path)
    except Exception:
        pass
    return f'conn:{id(conn)}'


def ensure_warehouse_schema(conn):
    """Idempotent — một lần / process / DB. Commit ngay để không giữ write-lock khi render trang."""
    db_key = _warehouse_db_key(conn)
    if _warehouse_schema_ready.get(db_key) == _WAREHOUSE_SCHEMA_VERSION:
        return

    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            branch_name TEXT,
            address TEXT,
            is_default INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Bổ sung cột branch_code nếu thiếu (SME multi-CN)
    wh_cols = {r[1] for r in c.execute('PRAGMA table_info(warehouses)').fetchall()}
    if 'branch_code' not in wh_cols:
        try:
            c.execute('ALTER TABLE warehouses ADD COLUMN branch_code TEXT')
            wh_cols.add('branch_code')
        except sqlite3.OperationalError:
            pass

    # Luôn đảm bảo có đủ KHO_001 / KHO_002 / KHO_003
    existing = {
        (r[0] or '').strip().upper()
        for r in c.execute('SELECT code FROM warehouses').fetchall()
    }
    for code, name, is_def in DEFAULT_WAREHOUSES:
        if code in existing:
            continue
        if 'branch_code' in wh_cols:
            c.execute(
                """
                INSERT INTO warehouses (code, name, is_default, is_active, branch_code)
                VALUES (?, ?, ?, 1, 'HQ')
                """,
                (code, name, is_def),
            )
        else:
            c.execute(
                """
                INSERT INTO warehouses (code, name, is_default, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (code, name, is_def),
            )

    # Gộp mã kho cũ (MAIN / KHO-CN2) → chuẩn KHO_001 / KHO_002
    _migrate_legacy_warehouse_codes(c)

    # Đảm bảo đúng 1 kho mặc định = KHO_001 nếu có
    has_kho001 = c.execute(
        "SELECT 1 FROM warehouses WHERE code = 'KHO_001' AND is_active = 1 LIMIT 1"
    ).fetchone()
    if has_kho001:
        c.execute("UPDATE warehouses SET is_default = 0 WHERE code != 'KHO_001'")
        c.execute("UPDATE warehouses SET is_default = 1 WHERE code = 'KHO_001'")
    else:
        has_default = c.execute(
            'SELECT 1 FROM warehouses WHERE is_active = 1 AND is_default = 1 LIMIT 1'
        ).fetchone()
        if not has_default:
            c.execute(
                """
                UPDATE warehouses SET is_default = 1
                WHERE id = (
                    SELECT id FROM warehouses WHERE is_active = 1 ORDER BY code LIMIT 1
                )
                """
            )

    for table, col in (('import', 'warehouse_code'), ('import_details', 'warehouse_code')):
        c.execute(f'PRAGMA table_info({table})')
        cols = {r[1] for r in c.fetchall()}
        if 'warehouse_code' not in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN warehouse_code TEXT DEFAULT 'KHO_001'")
            except sqlite3.OperationalError:
                pass

    c.execute('PRAGMA table_info(stock_moves)')
    sm_cols = {r[1] for r in c.fetchall()}
    if 'warehouse_code' not in sm_cols:
        try:
            c.execute("ALTER TABLE stock_moves ADD COLUMN warehouse_code TEXT DEFAULT 'KHO_001'")
        except sqlite3.OperationalError:
            pass

    try:
        conn.commit()
    except Exception:
        pass
    _warehouse_schema_ready[db_key] = _WAREHOUSE_SCHEMA_VERSION


def create_warehouse(
    conn,
    *,
    code: str,
    name: str,
    address: str = '',
    branch_code: str = 'HQ',
    is_default: bool = False,
    commit: bool = True,
) -> dict:
    """Thêm kho mới (SME). Mã tự chuẩn hoá chữ hoa."""
    ensure_warehouse_schema(conn)
    code = (code or '').strip().upper().replace(' ', '_')
    name = (name or '').strip()
    if not code:
        raise ValueError('Thiếu mã kho')
    if not name:
        raise ValueError('Thiếu tên kho')
    if not code.startswith('KHO_') and not code.startswith('KHO'):
        # Cho phép mã tự do nhưng khuyến nghị KHO_xxx
        pass
    exists = conn.execute(
        'SELECT 1 FROM warehouses WHERE UPPER(code) = ?', (code,)
    ).fetchone()
    if exists:
        raise ValueError(f'Mã kho {code} đã tồn tại')

    cols = {r[1] for r in conn.execute('PRAGMA table_info(warehouses)').fetchall()}
    if is_default:
        conn.execute('UPDATE warehouses SET is_default = 0')

    fields = ['code', 'name', 'is_default', 'is_active']
    vals = [code, name, 1 if is_default else 0, 1]
    if 'address' in cols:
        fields.append('address')
        vals.append((address or '').strip() or None)
    if 'branch_code' in cols:
        fields.append('branch_code')
        vals.append((branch_code or 'HQ').strip().upper() or 'HQ')
    if 'branch_name' in cols:
        fields.append('branch_name')
        vals.append(None)

    conn.execute(
        f"INSERT INTO warehouses ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
        vals,
    )
    if commit:
        conn.commit()
    row = conn.execute(
        'SELECT * FROM warehouses WHERE code = ?', (code,)
    ).fetchone()
    if row is None:
        return {'code': code, 'name': name}
    if hasattr(row, 'keys'):
        return dict(row)
    return {'code': code, 'name': name}


def update_warehouse(
    conn,
    code: str,
    *,
    name: str | None = None,
    address: str | None = None,
    branch_code: str | None = None,
    is_default: bool | None = None,
    is_active: bool | None = None,
    commit: bool = True,
) -> dict:
    """Cập nhật thông tin kho SME (name/address/branch_code/default/active)."""
    ensure_warehouse_schema(conn)
    code = (code or '').strip().upper().replace(' ', '_')
    if not code:
        raise ValueError('Thiếu mã kho')

    cols = {r[1] for r in conn.execute('PRAGMA table_info(warehouses)').fetchall()}
    row = conn.execute('SELECT code FROM warehouses WHERE UPPER(code) = ?', (code,)).fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy kho {code}')

    fields = []
    vals = []
    if name is not None:
        name = (name or '').strip()
        if not name:
            raise ValueError('Thiếu tên kho')
        fields.append('name')
        vals.append(name)
    if address is not None and 'address' in cols:
        fields.append('address')
        vals.append((address or '').strip() or None)
    if branch_code is not None and 'branch_code' in cols:
        fields.append('branch_code')
        vals.append((branch_code or 'HQ').strip().upper() or 'HQ')
    if is_active is not None and 'is_active' in cols:
        fields.append('is_active')
        vals.append(1 if is_active else 0)

    if is_default:
        # Đảm bảo duy nhất 1 kho mặc định (phần còn lại set về 0)
        if 'is_default' in cols:
            conn.execute('UPDATE warehouses SET is_default = 0 WHERE code != ?', (code,))
            fields.append('is_default')
            vals.append(1)
    elif is_default is False:
        if 'is_default' in cols:
            fields.append('is_default')
            vals.append(0)

    if fields:
        ph = ', '.join([f"{k}=?" for k in fields])
        conn.execute(f"UPDATE warehouses SET {ph} WHERE UPPER(code)=?", vals + [code])

    if commit:
        conn.commit()

    r = conn.execute('SELECT * FROM warehouses WHERE UPPER(code) = ?', (code,)).fetchone()
    return dict(r) if r and hasattr(r, 'keys') else {'code': code}


def next_warehouse_code(conn) -> str:
    """Gợi ý mã kho tiếp theo: KHO_004, KHO_005, …"""
    ensure_warehouse_schema(conn)
    rows = conn.execute(
        "SELECT code FROM warehouses WHERE code GLOB 'KHO_[0-9]*'"
    ).fetchall()
    max_n = 3
    for r in rows:
        code = (r[0] if not hasattr(r, 'keys') else r['code']) or ''
        tail = code[4:] if code.upper().startswith('KHO_') else ''
        if tail.isdigit():
            max_n = max(max_n, int(tail))
    return f'KHO_{max_n + 1:03d}'


def list_active_warehouses(conn, *, branch_code: str | None = None):
    ensure_warehouse_schema(conn)
    from Services.sme.branches import ensure_sme_branches_schema
    try:
        ensure_sme_branches_schema(conn, commit=False)
    except Exception:
        pass
    c = conn.cursor()
    cols = {r[1] for r in c.execute('PRAGMA table_info(warehouses)').fetchall()}
    has_br = 'branch_code' in cols
    sql = """
        SELECT id, code, name, branch_name, address, is_default
    """
    if has_br:
        sql = """
        SELECT id, code, name, branch_name, address, is_default, branch_code
        """
    sql += " FROM warehouses WHERE is_active = 1"
    params = []
    code = (branch_code or '').strip().upper()
    if has_br and code and code != 'ALL':
        if code == 'HQ':
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
            params.append('HQ')
        else:
            sql += " AND branch_code = ?"
            params.append(code)
    sql += " ORDER BY is_default DESC, code ASC"
    c.execute(sql, params)
    out_cols = ['id', 'code', 'name', 'branch_name', 'address', 'is_default']
    if has_br:
        out_cols.append('branch_code')
    return [dict(zip(out_cols, row)) for row in c.fetchall()]


def insert_import_detail_row(c, import_id, fields):
    """INSERT động vào import_details theo cột có sẵn."""
    c.execute('PRAGMA table_info(import_details)')
    allowed = {row[1] for row in c.fetchall()}
    cols, vals = [], []
    for key, val in fields.items():
        if key in allowed:
            cols.append(key)
            vals.append(val)
    if not cols:
        return
    ph = ', '.join(['?'] * len(vals))
    c.execute(f"INSERT INTO import_details ({', '.join(cols)}) VALUES ({ph})", vals)


def is_service_detail_row(row):
    """Dòng dịch vụ: chỉ lưu trên import_details (product_id NULL hoặc line_type=service)."""
    lt = (row.get('line_type') or '').strip().lower()
    if lt == 'service':
        return True
    pid = row.get('product_id')
    return pid is None or pid == '' or pid == 0


def detect_service_import(imp, detail_rows):
    """Phiếu mua dịch vụ thuần — lấy chi tiết trực tiếp từ import_details."""
    if (imp.get('doc_type') or '').strip().lower() == 'service':
        return True
    if not detail_rows:
        return False
    return all(is_service_detail_row(r if isinstance(r, dict) else dict(r)) for r in detail_rows)


def fetch_import_details_raw(c, import_id):
    """Luồng 2: SELECT trực tiếp từ import_details, không JOIN products."""
    c.execute('PRAGMA table_info(import_details)')
    detail_cols = {col[1] for col in c.fetchall()}

    select_parts = [
        'id', 'import_id', 'product_id', 'qty', 'buyprice',
        'COALESCE(discount, 0) AS discount',
        'COALESCE(tax, 0) AS tax',
        'COALESCE(subtotal, 0) AS subtotal',
        'COALESCE(payment_amt, 0) AS payment_amt',
        'COALESCE(unit_type, 0) AS unit_type',
        'COALESCE(tax_pct, 0) AS tax_pct',
        'COALESCE(discount_pct, 0) AS discount_pct',
    ]
    if 'product_name' in detail_cols:
        select_parts.append("COALESCE(product_name, '') AS product_name")
    if 'unit' in detail_cols:
        select_parts.append("COALESCE(unit, '') AS unit")
    if 'line_type' in detail_cols:
        select_parts.append("COALESCE(line_type, 'goods') AS line_type")
    if 'warehouse_code' in detail_cols:
        select_parts.append('warehouse_code')
    if 'import_tax_pct' in detail_cols:
        select_parts.append('COALESCE(import_tax_pct, 0) AS import_tax_pct')
    if 'import_tax_amount' in detail_cols:
        select_parts.append('COALESCE(import_tax_amount, 0) AS import_tax_amount')
    if 'excise_tax_pct' in detail_cols:
        select_parts.append('COALESCE(excise_tax_pct, 0) AS excise_tax_pct')
    if 'excise_tax_amount' in detail_cols:
        select_parts.append('COALESCE(excise_tax_amount, 0) AS excise_tax_amount')
    if 'expense_account' in detail_cols:
        select_parts.append("COALESCE(expense_account, '') AS expense_account")

    sql = f"""
        SELECT {', '.join(select_parts)}
        FROM import_details
        WHERE import_id = ?
        ORDER BY id
    """
    c.execute(sql, (import_id,))
    col_names = [d[0] for d in (c.description or [])]
    rows = []
    for row in c.fetchall():
        if isinstance(row, dict):
            rows.append(dict(row))
        elif hasattr(row, 'keys'):
            rows.append(dict(row))
        else:
            rows.append(dict(zip(col_names, row)))
    return rows


def _calc_line_financials(row):
    """Tính % chiết khấu/thuế và tổng thanh toán dòng từ import_details."""
    qty = float(row.get('qty') or 0)
    buyprice = float(row.get('buyprice') or 0)
    discount_amount = float(row.get('discount') or 0)
    tax_amount = float(row.get('tax') or 0)

    line_total = qty * buyprice
    after_discount = line_total - discount_amount
    payment_amt = float(row.get('payment_amt') or 0)
    if not payment_amt:
        payment_amt = after_discount + tax_amount

    discount_pct = float(row.get('discount_pct') or 0)
    if not discount_pct and line_total:
        discount_pct = discount_amount / line_total * 100
    tax_pct = float(row.get('tax_pct') or 0)
    if not tax_pct and after_discount:
        tax_pct = tax_amount / after_discount * 100

    return {
        'qty': qty,
        'buyprice': buyprice,
        'line_total': line_total,
        'subtotal': after_discount,
        'payment_amount': payment_amt,
        'discount_pct': round(discount_pct, 2),
        'tax_pct': round(tax_pct, 2),
        'discount': discount_amount,
        'tax': tax_amount,
    }


def map_service_detail_for_edit(row):
    """Map dòng dịch vụ (luồng 2) cho API edit / in phiếu."""
    fin = _calc_line_financials(row)
    unit = str(row.get('unit') or 'Lần').strip() or 'Lần'
    name = str(row.get('product_name') or '').strip()
    return {
        'id': row.get('id'),
        'import_id': row.get('import_id'),
        'product_id': None,
        'product_name': name,
        'name': name,
        'unit': unit,
        'invoice_unit': unit,
        'base_unit': unit,
        'wholesale_unit': '—',
        'line_type': 'service',
        'unit_type': 0,
        'base_sale_price': 0,
        'sale_price': 0,
        'unit_ratio': 1,
        **fin,
    }


def enrich_stock_detail_for_edit(c, row):
    """Luồng 1: bổ sung thông tin từ products khi có product_id."""
    item = dict(row)
    fin = _calc_line_financials(item)
    item.update(fin)

    pid = item.get('product_id')
    p_row = None
    if pid:
        c.execute('PRAGMA table_info(products)')
        product_cols = {col[1] for col in c.fetchall()}
        p_select = ['name']
        for col in ('unit', 'unit1', 'base_price', 'price', 'unit_ratio', 'barcode', 'barcode1', 'product_code'):
            if col in product_cols:
                p_select.append(col)
        c.execute(
            f"SELECT {', '.join(p_select)} FROM products WHERE id = ?",
            (pid,),
        )
        p_row = c.fetchone()

    detail_unit = str(item.get('unit') or '').strip()
    if p_row:
        p = dict(p_row)
        b_unit = str(p.get('unit') or detail_unit or 'Cái').strip() or 'Cái'
        w_unit = str(p.get('unit1') or '').strip()
        if not detail_unit:
            detail_unit = b_unit
        if not str(item.get('product_name') or '').strip():
            item['product_name'] = p.get('name') or ''
        if p.get('product_code') and not item.get('product_code'):
            item['product_code'] = p.get('product_code')
    else:
        b_unit = detail_unit or 'Cái'
        w_unit = ''
        if not detail_unit:
            detail_unit = b_unit

    unit_type = int(item.get('unit_type') or 0)
    if unit_type == 1 and w_unit:
        invoice_unit = w_unit
    else:
        invoice_unit = detail_unit or b_unit

    p_data = dict(p_row) if p_row else {}
    item.update({
        'invoice_unit': invoice_unit,
        'unit': invoice_unit,
        'base_unit': b_unit,
        'wholesale_unit': w_unit or '—',
        'base_sale_price': float(p_data.get('base_price') or 0),
        'sale_price': float(p_data.get('price') or 0),
        'unit_ratio': float(p_data.get('unit_ratio') or 1),
        'line_type': (item.get('line_type') or 'goods').strip().lower(),
        'product_code': item.get('product_code') or p_data.get('product_code') or '',
    })
    if p_data.get('barcode') is not None:
        item['barcode'] = p_data['barcode']
    if p_data.get('barcode1') is not None:
        item['barcode1'] = p_data['barcode1']
    return item


def build_service_line_insert_fields(import_id, item, extra_cost, total_base_for_allocation):
    """Chuẩn hóa payload dòng dịch vụ khi cập nhật phiếu nhập."""
    from decimal import Decimal

    qty = Decimal(str(item.get('qty', 0) or 0))
    if qty <= 0:
        return None, Decimal('0')

    name = (
        item.get('name')
        or item.get('invoice_name')
        or item.get('product_name')
        or ''
    ).strip()
    buyprice = Decimal(str(item.get('buyprice', 0) or 0))
    discount_pct = Decimal(str(
        item.get('discount_pct')
        if item.get('discount_pct') is not None
        else item.get('discountPct', 0)
    ))
    tax_pct = Decimal(str(
        item.get('tax_pct')
        if item.get('tax_pct') is not None
        else item.get('taxPct', 0)
    ))
    unit_in = str(item.get('unit') or 'Lần').strip() or 'Lần'

    line_subtotal = qty * buyprice
    line_disc = (
        Decimal(str(item.get('discount_amount')))
        if item.get('discount_amount') is not None
        else line_subtotal * discount_pct / Decimal('100')
    )
    line_after_disc = line_subtotal - line_disc
    line_tax = (
        Decimal(str(item.get('tax_amount')))
        if item.get('tax_amount') is not None
        else line_after_disc * tax_pct / Decimal('100')
    )
    allocated_extra = (
        extra_cost * (line_after_disc / total_base_for_allocation)
        if extra_cost > 0
        else Decimal('0')
    )
    line_total = line_after_disc + line_tax + allocated_extra
    per_unit_cost = line_total / qty if qty > 0 else Decimal('0')

    fields = {
        'import_id': import_id,
        'product_id': None,
        'qty': float(qty),
        'buyprice': float(buyprice),
        'subtotal': float(line_subtotal),
        'discount': float(line_disc),
        'tax': float(line_tax),
        'cost_price': float(per_unit_cost),
        'tax_pct': float(tax_pct),
        'discount_pct': float(discount_pct),
        'payment_amt': float(line_total),
        'product_name': name,
        'unit': unit_in,
        'line_type': 'service',
        'unit_type': 0,
    }
    wh = (item.get('warehouse_code') or '').strip()
    if wh:
        fields['warehouse_code'] = wh
    return fields, line_total


def is_service_line_payload(item):
    """Nhận diện dòng dịch vụ từ payload frontend."""
    line_type = (item.get('line_type') or '').strip().lower()
    if line_type == 'service':
        return True
    pid = item.get('product_id')
    try:
        pid_int = int(pid) if pid not in (None, '') else 0
    except (TypeError, ValueError):
        pid_int = 0
    return pid_int <= 0


def calc_import_detail_line_amounts(
    quantity,
    unit_price,
    discount_amount,
    tax_amount,
    subtotal,
    discount_pct=0,
    tax_pct=0,
):
    """Tính chiết khấu, thuế, tổng thanh toán cho một dòng import_details."""
    qty = float(quantity or 0)
    price = float(unit_price or 0)
    disc = float(discount_amount or 0)
    tax = float(tax_amount or 0)
    sub = float(subtotal or 0)

    if disc or tax or sub:
        discount_val = round(disc)
        tax_val = round(tax)
        if sub > 0:
            line_total = round(sub - discount_val + tax_val)
        else:
            line_total = round(qty * price - discount_val + tax_val)
    else:
        line_sub = round(qty * price)
        discount_val = round(line_sub * float(discount_pct or 0) / 100)
        after_discount = line_sub - discount_val
        tax_val = round(after_discount * float(tax_pct or 0) / 100)
        line_total = after_discount + tax_val

    return {
        'discount_amount': discount_val,
        'tax_amount': tax_val,
        'line_total': line_total,
    }


def sum_import_details_payment_period(cursor, start_date, end_date):
    """Tổng thanh toán + số dòng từ import_details (gồm dịch vụ không có product_id)."""
    cursor.execute('PRAGMA table_info(import_details)')
    detail_cols = {col[1] for col in cursor.fetchall()}

    disc_pct_sel = (
        'COALESCE(ii.discount_pct, 0) AS discount_pct'
        if 'discount_pct' in detail_cols
        else '0 AS discount_pct'
    )
    tax_pct_sel = (
        'COALESCE(ii.tax_pct, 0) AS tax_pct'
        if 'tax_pct' in detail_cols
        else '0 AS tax_pct'
    )

    cursor.execute(
        f"""
        SELECT
            ii.qty AS quantity,
            ii.buyprice AS unit_price,
            COALESCE(ii.discount, 0) AS discount_amount,
            COALESCE(ii.tax, 0) AS tax_amount,
            COALESCE(ii.subtotal, 0) AS subtotal,
            {disc_pct_sel},
            {tax_pct_sel}
        FROM import_details ii
        INNER JOIN import i ON i.id = ii.import_id
        WHERE ii.qty > 0
          AND date(i.date) >= date(?)
          AND date(i.date) <= date(?)
        """,
        (start_date, end_date),
    )

    rows = cursor.fetchall()
    col_names = [d[0] for d in (cursor.description or [])]
    total = 0.0
    count = 0
    for row in rows:
        if isinstance(row, dict):
            r = row
        elif hasattr(row, 'keys'):
            r = dict(row)
        else:
            r = dict(zip(col_names, row))
        amounts = calc_import_detail_line_amounts(
            r['quantity'],
            r['unit_price'],
            r['discount_amount'],
            r['tax_amount'],
            r['subtotal'],
            r.get('discount_pct'),
            r.get('tax_pct'),
        )
        total += amounts['line_total']
        count += 1
    return total, count


def _json_safe(value):
    """Chuyển giá trị SQLite/Python sang kiểu an toàn cho JSON response."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            return 0.0
        return value
    if isinstance(value, str):
        return value
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            return float(value)
    except ImportError:
        pass
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def prepare_import_edit_json(imp):
    """Chuẩn hóa payload GET /api/import/<id>/edit cho frontend."""
    if not imp:
        return imp
    out = {}
    for key, val in imp.items():
        if key == 'items':
            continue
        out[key] = _json_safe(val)

    out['items'] = []
    for item in imp.get('items') or []:
        row = {k: _json_safe(v) for k, v in dict(item).items()}
        pid = row.get('product_id')
        if pid in (0, '0', ''):
            row['product_id'] = None
        if not row.get('product_name') and row.get('name'):
            row['product_name'] = row['name']
        out['items'].append(row)

    # Bảo đảm luôn là list (tránh FE .map lỗi)
    adv = out.get('linked_advances')
    if not isinstance(adv, list):
        out['linked_advances'] = []
    return out


def _normalize_db_date(value):
    if value is None:
        return None
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    text = str(value).strip()
    if not text:
        return None
    if 'T' in text:
        text = text.split('T', 1)[0]
    elif ' ' in text:
        text = text.split(' ', 1)[0]
    return text[:10] if len(text) >= 10 else text


def load_import_for_edit(conn, import_id):
    """Chi tiết phiếu nhập + dòng — dùng chung API sửa (HKD/SME)."""
    c = conn.cursor()
    c.execute("SELECT * FROM import WHERE id = ?", (import_id,))
    row = c.fetchone()
    if not row:
        return None

    imp = dict(row)
    imp_keys = row.keys()

    if imp.get('supplier_id'):
        c.execute(
            "SELECT name, tax_code, address FROM suppliers WHERE id = ?",
            (imp['supplier_id'],),
        )
        sup_row = c.fetchone()
        if sup_row:
            imp['supplier_name'] = sup_row['name']
            imp['tax_code'] = sup_row['tax_code']
            imp['address'] = sup_row['address']

    if 'invoice_no' in imp_keys and not imp.get('bill_no'):
        imp['bill_no'] = imp['invoice_no']
    if 'bill_date' not in imp_keys:
        imp['bill_date'] = imp.get('date')
    if 'payment_method' not in imp_keys:
        imp['payment_method'] = 'cash'

    imp['date'] = _normalize_db_date(imp.get('date'))
    imp['bill_date'] = _normalize_db_date(imp.get('bill_date'))

    calculated_total = 0
    items = []
    raw_rows = fetch_import_details_raw(c, import_id)
    is_service_import = detect_service_import(imp, raw_rows)

    for detail_row in raw_rows:
        if is_service_detail_row(detail_row):
            item = map_service_detail_for_edit(detail_row)
        else:
            item = enrich_stock_detail_for_edit(c, detail_row)
        calculated_total += float(item.get('payment_amount') or 0)
        items.append(item)

    # Liên kết thanh toán NK (tạm ứng / L/C) — nếu schema đã có
    try:
        from Services.sme.import_payment import list_import_advances, ensure_import_payment_schema
        ensure_import_payment_schema(conn, commit=False)
        imp['linked_advances'] = list_import_advances(conn, int(import_id))
    except Exception:
        imp['linked_advances'] = []

    imp['is_service_import'] = is_service_import
    extra_cost = float(imp.get('extra_cost') or 0)
    imp['total_payment'] = calculated_total
    imp['total_value'] = calculated_total + extra_cost
    imp['items'] = items
    return imp
