"""Helper checkout POS: tồn kho theo product_type và snapshot nhóm HKD."""

from Services.hkd_sector import requires_stock_check, resolve_item_hkd_sector
from Services.inventory_cost import apply_cost_outbound, cost_snapshot_for_sale
from Services.inventory_stock_helpers import sync_inventory_quantity_from_moves


from db.schema_helpers import column_exists


def table_has_column(cursor, table, column):
    conn = getattr(cursor, 'connection', None) or cursor
    return column_exists(conn, table, column)


def fetch_product_for_checkout(cursor, product_id, warehouse_codes=None):
    """Lấy thông tin SP + tồn kho cho checkout POS (ưu tiên sổ cái).

    warehouse_codes: nếu chỉ định, chỉ tính stock trong các kho này (qua stock_moves).
    Bảng inventory thường không có warehouse_code (1 dòng / SP) — không lọc theo kho trên inventory.
    """
    has_sm_wh = table_has_column(cursor, 'stock_moves', 'warehouse_code')
    has_inv_wh = table_has_column(cursor, 'inventory', 'warehouse_code')
    filter_wh = bool(warehouse_codes) and (has_sm_wh or has_inv_wh)

    if filter_wh:
        ph = ','.join('?' * len(warehouse_codes))
        params: list = []
        if has_sm_wh:
            stock_expr = (
                f"(SELECT SUM(sm.quantity) FROM stock_moves sm "
                f"WHERE sm.product_id = p.id AND sm.warehouse_code IN ({ph}))"
            )
            params.extend(warehouse_codes)
        else:
            stock_expr = (
                "(SELECT SUM(sm.quantity) FROM stock_moves sm WHERE sm.product_id = p.id)"
            )

        if has_inv_wh:
            inv_fallback = (
                f"(SELECT SUM(i2.quantity) FROM inventory i2 "
                f"WHERE i2.product_id = p.id AND i2.warehouse_code IN ({ph}))"
            )
            avg_expr = (
                f"COALESCE("
                f"(SELECT AVG(i3.avg_cost) FROM inventory i3 "
                f"WHERE i3.product_id = p.id AND i3.warehouse_code IN ({ph}) AND i3.quantity > 0), "
                f"COALESCE(i.avg_cost, 0))"
            )
            params.extend(warehouse_codes)
            params.extend(warehouse_codes)
        else:
            inv_fallback = "i.quantity"
            avg_expr = "COALESCE(i.avg_cost, 0)"

        params.append(product_id)
        cursor.execute(f"""
            SELECT
                p.id, p.name, p.unit, p.unit1, p.unit_ratio,
                COALESCE(p.product_type, 'goods') AS product_type,
                p.hkd_sector_code,
                COALESCE({stock_expr}, COALESCE({inv_fallback}, 0)) AS stock,
                {avg_expr} AS avg_cost
            FROM products p
            LEFT JOIN inventory i ON p.id = i.product_id
            WHERE p.id = ?
        """, params)
    else:
        cursor.execute("""
            SELECT
                p.id, p.name, p.unit, p.unit1, p.unit_ratio,
                COALESCE(p.product_type, 'goods') AS product_type,
                p.hkd_sector_code,
                COALESCE(
                    (SELECT SUM(sm.quantity) FROM stock_moves sm WHERE sm.product_id = p.id),
                    i.quantity,
                    0
                ) AS stock,
                COALESCE(i.avg_cost, 0) AS avg_cost
            FROM products p
            LEFT JOIN inventory i ON p.id = i.product_id
            WHERE p.id = ?
        """, (product_id,))
    return cursor.fetchone()


def snapshot_item_hkd_sector(product_type, hkd_sector_code, business_line='pos'):
    return resolve_item_hkd_sector(
        product_sector=hkd_sector_code,
        product_type=product_type,
        business_line=business_line,
    )


def insert_pos_sale_item(cursor, sale_id, product_id, detail, hkd_sector_code=None):
    """Ghi sale_items; thêm hkd_sector_code nếu cột tồn tại."""
    cols = [
        'sale_id', 'product_id', 'quantity', 'price', 'cost_price',
        'UseSaleUnit', 'unit_ratio', 'discount_pct', 'tax_pct',
    ]
    vals = [
        sale_id, product_id, detail['qty_input'], detail['price'], detail['avg_cost'],
        1 if detail['use_unit1'] else 0, detail['ratio'],
        detail['discount_pct'], detail['tax_pct'],
    ]
    if hkd_sector_code and table_has_column(cursor, 'sale_items', 'hkd_sector_code'):
        cols.append('hkd_sector_code')
        vals.append(hkd_sector_code)
    placeholders = ', '.join(['?'] * len(vals))
    cursor.execute(
        f"INSERT INTO sale_items ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )


def _ensure_inventory_avg_cost(cursor, product_id, avg_cost):
    cursor.execute("SELECT avg_cost FROM inventory WHERE product_id = ?", (product_id,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?, 0, ?)",
            (product_id, avg_cost),
        )
    elif row[0] is None or float(row[0] or 0) == 0:
        cursor.execute(
            "UPDATE inventory SET avg_cost = ? WHERE product_id = ?",
            (avg_cost, product_id),
        )


def deduct_inventory_for_sale(
    cursor,
    product_id,
    deduct_qty,
    avg_cost,
    sale_id,
    sale_date,
    ref_doc,
    warehouse_code=None,
):
    """Trừ kho: WAC hoặc FIFO, ghi stock_moves, sync quantity."""
    from Services.stock_move_write import insert_stock_move, resolve_posting_warehouse_code

    deduct_qty = float(deduct_qty)
    conn = cursor.connection
    cost_used = cost_snapshot_for_sale(cursor, product_id, conn=conn)
    if cost_used <= 0 and float(avg_cost or 0) > 0:
        cost_used = float(avg_cost)
    _ensure_inventory_avg_cost(cursor, product_id, cost_used)
    _new_avg, cost_used, fifo_details = apply_cost_outbound(
        cursor, product_id, deduct_qty, cost_used,
        ref_type='sale', ref_id=sale_id, conn=conn,
    )
    wh = resolve_posting_warehouse_code(conn, warehouse_code)
    move_id = insert_stock_move(cursor, {
        'product_id': product_id,
        'date': sale_date,
        'type': 'SALE',
        'ref_id': sale_id,
        'quantity': -deduct_qty,
        'cost_price': cost_used,
        'ref_document': ref_doc,
        'ref_type': 'export',
        'type1': 'Bán',
        'note': 'Bán hàng cho khách',
        'warehouse_code': wh,
    })
    for det in fifo_details or []:
        cid = det.get('consumption_id')
        if cid and move_id:
            cursor.execute(
                'UPDATE inventory_lot_consumptions SET stock_move_id = ? WHERE id = ?',
                (move_id, cid),
            )
    cursor.execute("""
        INSERT INTO inventory_transactions
        (product_id, type1, type, quantity, cost_price, reference_id, reference_type, created_at)
        VALUES (?, 'Bán', 'export', ?, ?, ?, 'sale', ?)
    """, (product_id, -deduct_qty, cost_used, sale_id, sale_date))
    sync_inventory_quantity_from_moves(cursor, product_id)
    return cost_used


def restore_inventory_for_sale_item(cursor, product_id, restore_qty, product_type):
    """
    Không cộng inventory trực tiếp — caller phải gọi revert_sale_stock(sale_id).
    Giữ hàm để tương thích import cũ.
    """
    return


def insert_sale_item_with_sector(cursor, columns, values, hkd_sector_code=None):
    """INSERT sale_items linh hoạt (F&B, rental) kèm hkd_sector_code nếu có."""
    cols = list(columns)
    vals = list(values)
    if hkd_sector_code and table_has_column(cursor, 'sale_items', 'hkd_sector_code'):
        cols.append('hkd_sector_code')
        vals.append(hkd_sector_code)
    placeholders = ', '.join(['?'] * len(vals))
    cursor.execute(
        f"INSERT INTO sale_items ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    if table_has_column(cursor, 'sale_items', 'id'):
        try:
            cursor.execute(
                'UPDATE sale_items SET id = rowid WHERE id IS NULL AND rowid = last_insert_rowid()'
            )
        except Exception:
            pass
