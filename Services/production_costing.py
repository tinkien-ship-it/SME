"""Tính giá thành thành phẩm — định mức BOM + phiếu sản xuất.

Luồng hoàn thành phiếu:
1. Xuất NVL (stock_moves type=export, ref_type=PRODUCTION) theo WAC hiện tại
2. Nhập TP (type=import, ref_type=PRODUCTION) với giá thành = Σ(NVL) / SL_TP
3. Cập nhật inventory.avg_cost qua apply_wac_*

Hủy phiếu: đảo chiều (nhập lại NVL, xuất lại TP) — giữ chứng từ gốc để audit.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from Services.inventory_stock_helpers import (
    apply_wac_inbound,
    apply_wac_outbound,
    get_wac,
    ledger_quantity,
    sync_inventory_quantity_from_moves,
)

REF_TYPE = 'PRODUCTION'
VOUCHER_PREFIX = 'SX'
STATUS_COMPLETED = 'completed'
STATUS_CANCELLED = 'cancelled'

# NVL chấp nhận: materials; goods cũng cho phép (HKD hay dùng hàng mua làm NVL)
_MATERIAL_TYPES = ('materials', 'goods', 'raw_materials')
_FINISHED_TYPES = ('finished_goods',)


def ensure_production_schema(conn: sqlite3.Connection) -> None:
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS voucher_seq (
            type TEXT PRIMARY KEY,
            seq INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS product_bom (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finished_product_id INTEGER NOT NULL UNIQUE,
            note TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (finished_product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS product_bom_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bom_id INTEGER NOT NULL,
            material_product_id INTEGER NOT NULL,
            qty_per_unit REAL NOT NULL DEFAULT 0,
            note TEXT,
            UNIQUE (bom_id, material_product_id),
            FOREIGN KEY (bom_id) REFERENCES product_bom(id) ON DELETE CASCADE,
            FOREIGN KEY (material_product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS production_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT NOT NULL UNIQUE,
            production_date TEXT NOT NULL,
            finished_product_id INTEGER NOT NULL,
            qty_planned REAL DEFAULT 0,
            qty_completed REAL NOT NULL DEFAULT 0,
            total_material_cost REAL DEFAULT 0,
            unit_cost REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'completed',
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            cancelled_at TEXT,
            cancel_note TEXT,
            FOREIGN KEY (finished_product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS production_order_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            material_product_id INTEGER NOT NULL,
            qty_standard REAL NOT NULL DEFAULT 0,
            qty_actual REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (order_id) REFERENCES production_orders(id) ON DELETE CASCADE,
            FOREIGN KEY (material_product_id) REFERENCES products(id)
        );

        CREATE INDEX IF NOT EXISTS idx_product_bom_fg ON product_bom(finished_product_id);
        CREATE INDEX IF NOT EXISTS idx_prod_orders_date ON production_orders(production_date);
        CREATE INDEX IF NOT EXISTS idx_prod_orders_status ON production_orders(status);
        CREATE INDEX IF NOT EXISTS idx_prod_order_mats ON production_order_materials(order_id);
        """
    )
    # Giai đoạn 2: chi phí nhân công / chi phí khác cộng vào giá thành
    cols = {r[1] for r in c.execute("PRAGMA table_info(production_orders)").fetchall()}
    for col, decl in (
        ('labor_cost', 'REAL DEFAULT 0'),
        ('other_cost', 'REAL DEFAULT 0'),
        ('total_cost', 'REAL DEFAULT 0'),
    ):
        if col not in cols:
            try:
                c.execute(f"ALTER TABLE production_orders ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
    conn.commit()


def _row_dict(row):
    if row is None:
        return None
    return dict(row)


def _product_type(cursor, product_id: int) -> str:
    cursor.execute(
        "SELECT COALESCE(product_type, 'goods') FROM products WHERE id = ?",
        (product_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy sản phẩm #{product_id}')
    return str(row[0] or 'goods').strip().lower()


def _assert_finished(cursor, product_id: int) -> None:
    pt = _product_type(cursor, product_id)
    if pt not in _FINISHED_TYPES:
        raise ValueError('Chỉ thành phẩm (finished_goods) mới có định mức / phiếu sản xuất')


def _assert_material(cursor, product_id: int, *, require_vt_code: bool = False) -> None:
    pt = _product_type(cursor, product_id)
    if pt not in _MATERIAL_TYPES:
        raise ValueError(
            f'Sản phẩm #{product_id} không phải vật tư/hàng hóa (loại: {pt})'
        )
    if require_vt_code:
        cursor.execute(
            "SELECT COALESCE(product_code, '') FROM products WHERE id = ?",
            (product_id,),
        )
        code = str(cursor.fetchone()[0] or '').strip().upper()
        if not code.startswith('VT'):
            raise ValueError(
                f'Mã vật tư phải bắt đầu bằng VT (hiện tại: {code or "trống"})'
            )


def next_production_voucher(cursor) -> str:
    cursor.execute(
        "INSERT INTO voucher_seq (type, seq) VALUES (?, 1) "
        "ON CONFLICT(type) DO UPDATE SET seq = seq + 1",
        (VOUCHER_PREFIX,),
    )
    cursor.execute("SELECT seq FROM voucher_seq WHERE type = ?", (VOUCHER_PREFIX,))
    seq = int(cursor.fetchone()[0] or 1)
    return f"{VOUCHER_PREFIX}{seq:06d}"


# ---------------------------------------------------------------------------
# Products for pickers
# ---------------------------------------------------------------------------

def list_finished_products(conn: sqlite3.Connection, q: str = '') -> list[dict]:
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = """
        SELECT p.id, p.name, p.product_code, p.barcode, p.unit,
               COALESCE(p.product_type, 'goods') AS product_type,
               COALESCE(i.quantity, 0) AS stock,
               COALESCE(i.avg_cost, 0) AS avg_cost,
               CASE WHEN b.id IS NOT NULL THEN 1 ELSE 0 END AS has_bom
        FROM products p
        LEFT JOIN inventory i ON i.product_id = p.id
        LEFT JOIN product_bom b ON b.finished_product_id = p.id
        WHERE LOWER(COALESCE(p.product_type, '')) = 'finished_goods'
    """
    params: list = []
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (p.name LIKE ? OR p.product_code LIKE ? OR p.barcode LIKE ?)"
        params.extend([like, like, like])
    sql += " ORDER BY p.name COLLATE NOCASE LIMIT 200"
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def list_material_products(
    conn: sqlite3.Connection,
    q: str = '',
    *,
    code_prefix: str = '',
) -> list[dict]:
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = """
        SELECT p.id, p.name, p.product_code, p.barcode, p.unit,
               COALESCE(p.product_type, 'goods') AS product_type,
               COALESCE(i.quantity, 0) AS stock,
               COALESCE(i.avg_cost, 0) AS avg_cost
        FROM products p
        LEFT JOIN inventory i ON i.product_id = p.id
        WHERE LOWER(COALESCE(p.product_type, 'goods')) IN ('materials', 'goods', 'raw_materials')
    """
    params: list = []
    prefix = (code_prefix or '').strip().upper()
    if prefix:
        sql += " AND UPPER(COALESCE(p.product_code, '')) LIKE ?"
        params.append(f"{prefix}%")
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (p.name LIKE ? OR p.product_code LIKE ? OR p.barcode LIKE ?)"
        params.extend([like, like, like])
    sql += " ORDER BY p.name COLLATE NOCASE LIMIT 300"
    return [dict(r) for r in c.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

def get_bom(conn: sqlite3.Connection, finished_product_id: int) -> dict | None:
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    bom = c.execute(
        """
        SELECT b.*, p.name AS finished_name, p.product_code, p.unit AS finished_unit,
               COALESCE(i.avg_cost, 0) AS finished_avg_cost,
               COALESCE(i.quantity, 0) AS finished_stock
        FROM product_bom b
        JOIN products p ON p.id = b.finished_product_id
        LEFT JOIN inventory i ON i.product_id = p.id
        WHERE b.finished_product_id = ?
        """,
        (finished_product_id,),
    ).fetchone()
    if not bom:
        return None
    items = c.execute(
        """
        SELECT bi.*, m.name AS material_name, m.product_code AS material_code,
               m.unit AS material_unit,
               COALESCE(inv.quantity, 0) AS stock,
               COALESCE(inv.avg_cost, 0) AS avg_cost
        FROM product_bom_items bi
        JOIN products m ON m.id = bi.material_product_id
        LEFT JOIN inventory inv ON inv.product_id = m.id
        WHERE bi.bom_id = ?
        ORDER BY bi.id
        """,
        (bom['id'],),
    ).fetchall()
    data = dict(bom)
    data['items'] = [dict(x) for x in items]
    return data


def list_boms(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        """
        SELECT b.id, b.finished_product_id, b.note, b.updated_at,
               p.name AS finished_name, p.product_code, p.unit AS finished_unit,
               (SELECT COUNT(*) FROM product_bom_items bi WHERE bi.bom_id = b.id) AS item_count
        FROM product_bom b
        JOIN products p ON p.id = b.finished_product_id
        ORDER BY p.name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(r) for r in rows]


def save_bom(
    conn: sqlite3.Connection,
    finished_product_id: int,
    items: list[dict],
    note: str = '',
) -> dict:
    """Tạo/cập nhật định mức. items: [{material_product_id, qty_per_unit, note?}]"""
    c = conn.cursor()
    ensure_production_schema(conn)
    _assert_finished(c, finished_product_id)

    cleaned = []
    seen = set()
    for raw in items or []:
        mid = int(raw.get('material_product_id') or 0)
        qty = float(raw.get('qty_per_unit') or 0)
        if mid <= 0 or qty <= 0:
            continue
        if mid == int(finished_product_id):
            raise ValueError('Không thể dùng chính thành phẩm làm vật tư')
        if mid in seen:
            raise ValueError(f'Vật tư #{mid} bị trùng trong định mức')
        _assert_material(c, mid, require_vt_code=True)
        seen.add(mid)
        cleaned.append({
            'material_product_id': mid,
            'qty_per_unit': qty,
            'note': (raw.get('note') or '').strip(),
        })
    if not cleaned:
        raise ValueError('Định mức phải có ít nhất 1 vật tư với số lượng > 0')

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    existing = c.execute(
        "SELECT id FROM product_bom WHERE finished_product_id = ?",
        (finished_product_id,),
    ).fetchone()
    if existing:
        bom_id = existing[0]
        c.execute(
            "UPDATE product_bom SET note = ?, updated_at = ? WHERE id = ?",
            ((note or '').strip(), now, bom_id),
        )
        c.execute("DELETE FROM product_bom_items WHERE bom_id = ?", (bom_id,))
    else:
        c.execute(
            "INSERT INTO product_bom (finished_product_id, note, updated_at) VALUES (?, ?, ?)",
            (finished_product_id, (note or '').strip(), now),
        )
        bom_id = c.lastrowid

    for it in cleaned:
        c.execute(
            """
            INSERT INTO product_bom_items (bom_id, material_product_id, qty_per_unit, note)
            VALUES (?, ?, ?, ?)
            """,
            (bom_id, it['material_product_id'], it['qty_per_unit'], it['note']),
        )
    conn.commit()
    return get_bom(conn, finished_product_id)


def delete_bom(conn: sqlite3.Connection, finished_product_id: int) -> None:
    c = conn.cursor()
    c.execute("DELETE FROM product_bom WHERE finished_product_id = ?", (finished_product_id,))
    conn.commit()


def preview_materials(
    conn: sqlite3.Connection,
    finished_product_id: int,
    qty_completed: float,
    overrides: list[dict] | None = None,
) -> list[dict]:
    """Tính NVL cần xuất theo định mức (có thể override qty_actual)."""
    bom = get_bom(conn, finished_product_id)
    if not bom:
        raise ValueError('Thành phẩm chưa có định mức BOM')
    qty_completed = float(qty_completed or 0)
    if qty_completed <= 0:
        raise ValueError('Số lượng hoàn thành phải > 0')

    override_map = {}
    for o in overrides or []:
        mid = int(o.get('material_product_id') or 0)
        if mid:
            override_map[mid] = float(o.get('qty_actual') or 0)

    c = conn.cursor()
    lines = []
    for it in bom['items']:
        mid = int(it['material_product_id'])
        std = float(it['qty_per_unit']) * qty_completed
        actual = override_map[mid] if mid in override_map else std
        if actual < 0:
            raise ValueError(f"SL vật tư thực tế không hợp lệ: {it['material_name']}")
        stock = ledger_quantity(c, mid)
        wac = get_wac(c, mid)
        lines.append({
            'material_product_id': mid,
            'material_name': it['material_name'],
            'material_code': it.get('material_code'),
            'material_unit': it.get('material_unit'),
            'qty_per_unit': float(it['qty_per_unit']),
            'qty_standard': round(std, 6),
            'qty_actual': round(actual, 6),
            'stock': stock,
            'avg_cost': wac,
            'line_cost': round(actual * wac, 2),
            'enough_stock': stock + 1e-9 >= actual,
        })
    return lines


# ---------------------------------------------------------------------------
# Production orders
# ---------------------------------------------------------------------------

def _insert_stock_move(
    cursor,
    *,
    product_id: int,
    when: str,
    move_type: str,
    type1: str,
    ref_id: int,
    voucher_no: str,
    quantity: float,
    cost_price: float,
    note: str,
):
    cursor.execute(
        """
        INSERT INTO stock_moves (
            product_id, date, type, type1, ref_type, ref_id, ref_document,
            quantity, cost_price, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id, when, move_type, type1, REF_TYPE, ref_id, voucher_no,
            quantity, cost_price, note,
        ),
    )
    return cursor.lastrowid


def _ensure_finished_product_codes(cursor, finished_product_id: int) -> tuple[str, str]:
    """
    Đảm bảo thành phẩm có mã giống products.html:
    product_code = TP001 (tăng dần), barcode = TP00101, barcode1 = TP00102 (nếu có ĐVT 2).
    """
    cursor.execute(
        "SELECT product_code, barcode, unit1 FROM products WHERE id = ?",
        (finished_product_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy thành phẩm #{finished_product_id}')
    if isinstance(row, sqlite3.Row):
        code = (row['product_code'] or '').strip()
        barcode = (row['barcode'] or '').strip()
        unit1 = row['unit1']
    else:
        code = (row[0] or '').strip()
        barcode = (row[1] or '').strip()
        unit1 = row[2] if len(row) > 2 else None
    if code.upper().startswith('TP') and barcode:
        return code, barcode
    from Services.import_line_helpers import assign_product_codes
    new_code, new_barcode, _b1 = assign_product_codes(
        cursor, finished_product_id, 'finished_goods', unit1,
    )
    return new_code, new_barcode


def create_production_order(
    conn: sqlite3.Connection,
    *,
    finished_product_id: int,
    qty_completed: float,
    production_date: str | None = None,
    note: str = '',
    material_overrides: list[dict] | None = None,
    labor_cost: float = 0,
    other_cost: float = 0,
    created_by: str = '',
    allow_negative_stock: bool = False,
) -> dict:
    ensure_production_schema(conn)
    c = conn.cursor()
    try:
        _assert_finished(c, finished_product_id)
        _ensure_finished_product_codes(c, finished_product_id)

        qty = float(qty_completed or 0)
        if qty <= 0:
            raise ValueError('Số lượng hoàn thành phải > 0')

        labor = max(0.0, float(labor_cost or 0))
        other = max(0.0, float(other_cost or 0))

        date_str = (production_date or datetime.now().strftime('%Y-%m-%d')).strip()[:10]
        when = f"{date_str} {datetime.now().strftime('%H:%M:%S')}"

        lines = preview_materials(conn, finished_product_id, qty, material_overrides)
        if not any(l['qty_actual'] > 0 for l in lines):
            raise ValueError('Không có vật tư nào được xuất (SL thực tế = 0)')

        if not allow_negative_stock:
            short = [l for l in lines if l['qty_actual'] > 0 and not l['enough_stock']]
            if short:
                names = ', '.join(
                    f"{s['material_name']} (cần {s['qty_actual']}, còn {s['stock']})"
                    for s in short[:5]
                )
                raise ValueError(f'Tồn vật tư không đủ: {names}')

        voucher_no = next_production_voucher(c)
        material_cost = 0.0
        material_rows = []

        for line in lines:
            mid = line['material_product_id']
            q_act = float(line['qty_actual'])
            if q_act <= 0:
                continue
            _new_wac, cost_used = apply_wac_outbound(c, mid, q_act)
            line_cost = round(q_act * cost_used, 2)
            material_cost += line_cost
            material_rows.append({
                'material_product_id': mid,
                'qty_standard': line['qty_standard'],
                'qty_actual': q_act,
                'unit_cost': cost_used,
                'total_cost': line_cost,
            })

        material_cost = round(material_cost, 2)
        total_cost = round(material_cost + labor + other, 2)
        unit_cost = round(total_cost / qty, 4) if qty else 0.0

        c.execute(
            """
            INSERT INTO production_orders (
                voucher_no, production_date, finished_product_id,
                qty_planned, qty_completed,
                total_material_cost, labor_cost, other_cost, total_cost, unit_cost,
                status, note, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                voucher_no, date_str, finished_product_id,
                qty, qty,
                material_cost, labor, other, total_cost, unit_cost,
                STATUS_COMPLETED, (note or '').strip(), (created_by or '').strip(),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            ),
        )
        order_id = c.lastrowid

        for m in material_rows:
            c.execute(
                """
                INSERT INTO production_order_materials (
                    order_id, material_product_id, qty_standard, qty_actual, unit_cost, total_cost
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id, m['material_product_id'], m['qty_standard'],
                    m['qty_actual'], m['unit_cost'], m['total_cost'],
                ),
            )
            move_id = _insert_stock_move(
                c,
                product_id=m['material_product_id'],
                when=when,
                move_type='export',
                type1='Xuất vật tư sản xuất',
                ref_id=order_id,
                voucher_no=voucher_no,
                quantity=-m['qty_actual'],
                cost_price=m['unit_cost'],
                note=f"SX {voucher_no}: xuất vật tư",
            )
            try:
                c.execute(
                    """
                    INSERT INTO inventory_transactions
                        (product_id, type, type1, quantity, cost_price, reference_id, reference_type, note, created_at)
                    VALUES (?, 'export', 'Xuất vật tư sản xuất', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        m['material_product_id'], -m['qty_actual'], m['unit_cost'],
                        move_id, REF_TYPE, f"SX {voucher_no}", when,
                    ),
                )
            except sqlite3.Error:
                pass
            sync_inventory_quantity_from_moves(c, m['material_product_id'])

        apply_wac_inbound(c, finished_product_id, qty, total_cost)
        fg_move = _insert_stock_move(
            c,
            product_id=finished_product_id,
            when=when,
            move_type='import',
            type1='Nhập thành phẩm SX',
            ref_id=order_id,
            voucher_no=voucher_no,
            quantity=qty,
            cost_price=unit_cost,
            note=f"SX {voucher_no}: nhập TP giá thành {unit_cost:,.0f}",
        )
        try:
            c.execute(
                """
                INSERT INTO inventory_transactions
                    (product_id, type, type1, quantity, cost_price, reference_id, reference_type, note, created_at)
                VALUES (?, 'import', 'Nhập thành phẩm SX', ?, ?, ?, ?, ?, ?)
                """,
                (
                    finished_product_id, qty, unit_cost, fg_move, REF_TYPE,
                    f"SX {voucher_no}", when,
                ),
            )
        except sqlite3.Error:
            pass
        sync_inventory_quantity_from_moves(c, finished_product_id)

        conn.commit()
        return get_production_order(conn, order_id)
    except Exception:
        conn.rollback()
        raise


def get_production_order(conn: sqlite3.Connection, order_id: int) -> dict | None:
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute(
        """
        SELECT o.*,
               p.name AS finished_name, p.product_code, p.unit AS finished_unit
        FROM production_orders o
        JOIN products p ON p.id = o.finished_product_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()
    if not row:
        return None
    mats = c.execute(
        """
        SELECT m.*, p.name AS material_name, p.product_code AS material_code,
               p.unit AS material_unit
        FROM production_order_materials m
        JOIN products p ON p.id = m.material_product_id
        WHERE m.order_id = ?
        ORDER BY m.id
        """,
        (order_id,),
    ).fetchall()
    data = dict(row)
    data['labor_cost'] = float(data.get('labor_cost') or 0)
    data['other_cost'] = float(data.get('other_cost') or 0)
    mat = float(data.get('total_material_cost') or 0)
    if data.get('total_cost') is None:
        data['total_cost'] = round(mat + data['labor_cost'] + data['other_cost'], 2)
    else:
        data['total_cost'] = float(data['total_cost'] or 0)
    data['materials'] = [dict(x) for x in mats]
    return data


def list_production_orders(
    conn: sqlite3.Connection,
    *,
    date_from: str = '',
    date_to: str = '',
    status: str = '',
    q: str = '',
    limit: int = 200,
) -> list[dict]:
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = """
        SELECT o.id, o.voucher_no, o.production_date, o.finished_product_id,
               o.qty_completed, o.total_material_cost,
               COALESCE(o.labor_cost, 0) AS labor_cost,
               COALESCE(o.other_cost, 0) AS other_cost,
               COALESCE(o.total_cost, o.total_material_cost) AS total_cost,
               o.unit_cost, o.status,
               o.note, o.created_at, o.cancelled_at,
               p.name AS finished_name, p.product_code, p.unit AS finished_unit
        FROM production_orders o
        JOIN products p ON p.id = o.finished_product_id
        WHERE 1=1
    """
    cols = {r[1] for r in c.execute('PRAGMA table_info(production_orders)').fetchall()}
    if 'journal_entry_id' in cols:
        sql = sql.replace(
            'o.note, o.created_at, o.cancelled_at,',
            'o.note, o.created_at, o.cancelled_at, o.journal_entry_id,',
        )
    params: list = []
    if date_from:
        sql += " AND o.production_date >= ?"
        params.append(date_from[:10])
    if date_to:
        sql += " AND o.production_date <= ?"
        params.append(date_to[:10])
    if status:
        sql += " AND o.status = ?"
        params.append(status)
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (o.voucher_no LIKE ? OR p.name LIKE ? OR p.product_code LIKE ?)"
        params.extend([like, like, like])
    sql += " ORDER BY o.production_date DESC, o.id DESC LIMIT ?"
    params.append(int(limit or 200))
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def cancel_production_order(
    conn: sqlite3.Connection,
    order_id: int,
    *,
    cancel_note: str = '',
    allow_negative_stock: bool = False,
) -> dict:
    ensure_production_schema(conn)
    order = get_production_order(conn, order_id)
    if not order:
        raise ValueError('Không tìm thấy phiếu sản xuất')
    if order['status'] == STATUS_CANCELLED:
        raise ValueError('Phiếu đã bị hủy')

    c = conn.cursor()
    try:
        fg_id = int(order['finished_product_id'])
        qty = float(order['qty_completed'] or 0)
        unit_cost = float(order['unit_cost'] or 0)
        voucher_no = order['voucher_no']
        when = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        fg_stock = ledger_quantity(c, fg_id)
        if not allow_negative_stock and fg_stock + 1e-9 < qty:
            raise ValueError(
                f"Không hủy được: tồn thành phẩm còn {fg_stock}, cần xuất lại {qty}. "
                "Hãy nhập lại / điều chỉnh tồn TP trước."
            )

        apply_wac_outbound(c, fg_id, qty, unit_cost)
        _insert_stock_move(
            c,
            product_id=fg_id,
            when=when,
            move_type='export',
            type1='Hủy SX — xuất lại TP',
            ref_id=order_id,
            voucher_no=voucher_no,
            quantity=-qty,
            cost_price=unit_cost,
            note=f"Hủy {voucher_no}: xuất lại TP",
        )
        sync_inventory_quantity_from_moves(c, fg_id)

        for m in order['materials']:
            mid = int(m['material_product_id'])
            q_act = float(m['qty_actual'] or 0)
            cost = float(m['unit_cost'] or 0)
            if q_act <= 0:
                continue
            value = q_act * cost
            apply_wac_inbound(c, mid, q_act, value)
            _insert_stock_move(
                c,
                product_id=mid,
                when=when,
                move_type='import',
            type1='Hủy SX — nhập lại vật tư',
            ref_id=order_id,
            voucher_no=voucher_no,
            quantity=q_act,
            cost_price=cost,
            note=f"Hủy {voucher_no}: nhập lại vật tư",
        )
            sync_inventory_quantity_from_moves(c, mid)

        # Đảo bút toán SME (full: collect/wip/fg hoặc simple)
        try:
            from Services.sme.production_journal import reverse_production_journals
            reverse_production_journals(
                conn, order_id, reason=f'Hủy SX {voucher_no}',
            )
        except Exception:
            pass

        c.execute(
            """
            UPDATE production_orders
            SET status = ?, cancelled_at = ?, cancel_note = ?
            WHERE id = ?
            """,
            (STATUS_CANCELLED, when, (cancel_note or '').strip(), order_id),
        )
        conn.commit()
        return get_production_order(conn, order_id)
    except Exception:
        conn.rollback()
        raise
