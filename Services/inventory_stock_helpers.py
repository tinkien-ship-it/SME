"""
Đồng bộ tồn kho: inventory (snapshot) ↔ stock_moves (sổ cái).

Nguyên tắc WAC (bình quân gia quyền):
- stock_moves là nguồn sự thật cho SỐ LƯỢNG tồn.
- inventory.avg_cost = giá trị tồn lũy kế / số lượng tồn lũy kế.
- Nhập: C' = (Q·C + ΔV) / (Q + ΔQ)
- Xuất: C' = (Q·C − ΔQ·Cₜ) / (Q − ΔQ)
- Trả NCC (return_import): xuất theo import_details.cost_price của PN gốc (đơn vị lẻ)
- Hoàn bán (return_sale / revert đơn): nhập lại theo sale_items.cost_price (giá vốn lúc bán)
"""

from Services.hkd_sector import requires_stock_check


def ledger_quantity(cursor, product_id):
    cursor.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM stock_moves WHERE product_id = ?",
        (product_id,),
    )
    row = cursor.fetchone()
    return float(row[0] if row else 0)


def get_wac(cursor, product_id):
    """WAC hiện tại từ inventory (0 nếu chưa có dòng)."""
    cursor.execute("SELECT avg_cost FROM inventory WHERE product_id = ?", (product_id,))
    row = cursor.fetchone()
    if row is None:
        return 0.0
    val = row[0] if not hasattr(row, 'keys') else row['avg_cost']
    return float(val or 0)


def _set_avg_cost(cursor, product_id, avg_cost):
    avg_cost = float(avg_cost or 0)
    cursor.execute("SELECT product_id FROM inventory WHERE product_id = ?", (product_id,))
    if cursor.fetchone():
        cursor.execute(
            "UPDATE inventory SET avg_cost = ? WHERE product_id = ?",
            (avg_cost, product_id),
        )
    else:
        cursor.execute(
            "INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?, 0, ?)",
            (product_id, avg_cost),
        )
    return avg_cost


def sync_inventory_quantity_from_moves(cursor, product_id):
    """Cập nhật inventory.quantity = SUM(stock_moves.quantity) cho một SP."""
    qty = ledger_quantity(cursor, product_id)
    cursor.execute("SELECT avg_cost FROM inventory WHERE product_id = ?", (product_id,))
    row = cursor.fetchone()
    if row is not None:
        cursor.execute(
            "UPDATE inventory SET quantity = ? WHERE product_id = ?",
            (qty, product_id),
        )
    else:
        cursor.execute(
            "INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?, ?, 0)",
            (product_id, qty),
        )
    return qty


def sync_inventory_quantities(cursor, product_ids):
    """Sync nhiều product_id; bỏ qua None/trùng."""
    seen = set()
    for pid in product_ids or []:
        if pid is None:
            continue
        pid = int(pid)
        if pid in seen:
            continue
        seen.add(pid)
        sync_inventory_quantity_from_moves(cursor, pid)


def apply_wac_inbound(cursor, product_id, qty_base, value_total):
    """
    Nhập thêm tồn: tăng Q, cập nhật WAC (gọi TRƯỚC khi INSERT stock_moves +qty).
    value_total: tổng giá trị nhập (= line_total hoặc qty × đơn giá vốn).
    """
    qty_base = float(qty_base or 0)
    value_total = float(value_total or 0)
    if qty_base <= 0:
        return get_wac(cursor, product_id)

    Q = ledger_quantity(cursor, product_id)
    C = get_wac(cursor, product_id)
    Q_new = Q + qty_base
    new_c = (Q * C + value_total) / Q_new if Q_new > 0 else 0.0
    _set_avg_cost(cursor, product_id, new_c)
    return new_c


def apply_wac_outbound(cursor, product_id, qty_base, unit_cost=None):
    """
    Xuất giảm tồn: cập nhật WAC theo Cₜ (gọi TRƯỚC khi INSERT stock_moves −qty).
    Trả về (new_wac, cost_used).
    """
    qty_base = float(qty_base or 0)
    if qty_base <= 0:
        c = get_wac(cursor, product_id)
        return c, c

    Q = ledger_quantity(cursor, product_id)
    C = get_wac(cursor, product_id)
    cost_used = float(unit_cost if unit_cost is not None else C)

    if Q < qty_base - 0.0001:
        raise ValueError(
            f"Tồn kho không đủ (SP #{product_id}: cần {qty_base}, còn {Q})"
        )

    Q_new = Q - qty_base
    if Q_new <= 0.0001:
        new_c = 0.0
    else:
        new_c = (Q * C - qty_base * cost_used) / Q_new

    _set_avg_cost(cursor, product_id, new_c)
    return new_c, cost_used


def restore_wac_after_sale_reversal(cursor, product_id, qty_base, sale_cost):
    """
    Sau khi đã XÓA stock_moves SALE, cập nhật WAC theo giá vốn lúc bán (giống return_sale).
    """
    qty_base = float(qty_base or 0)
    sale_cost = float(sale_cost or 0)
    if qty_base <= 0:
        return get_wac(cursor, product_id)

    Q = ledger_quantity(cursor, product_id)
    C = get_wac(cursor, product_id)
    Q_prev = Q - qty_base
    if Q <= 0.0001:
        new_c = 0.0
    elif Q_prev <= 0.0001:
        new_c = sale_cost
    else:
        new_c = (Q_prev * C + qty_base * sale_cost) / Q

    _set_avg_cost(cursor, product_id, new_c)
    return new_c


def wac_snapshot_for_sale(cursor, product_id):
    """Giá vốn snapshot khi bán/xuất: WAC hiện tại từ inventory."""
    return get_wac(cursor, product_id)


def delete_sale_stock_moves(cursor, sale_id):
    """Xóa mọi dòng xuất bán của đơn (ref_type export/sale, type SALE)."""
    cursor.execute(
        """
        DELETE FROM stock_moves
        WHERE type = 'SALE'
          AND ref_id = ?
          AND COALESCE(ref_type, '') IN ('export', 'sale', '')
        """,
        (sale_id,),
    )


def delete_sale_inventory_transactions(cursor, sale_id):
    cursor.execute(
        """
        DELETE FROM inventory_transactions
        WHERE reference_type = 'sale' AND reference_id = ?
        """,
        (sale_id,),
    )


def revert_sale_stock(cursor, sale_id, product_ids=None):
    """
    Hoàn sổ cái đơn bán khi sửa đơn / thay thế HĐ:
    đọc sale_items → xóa SALE moves → WAC theo cost_price lúc bán → sync qty.
    """
    cursor.execute(
        """
        SELECT si.product_id, si.quantity, si.UseSaleUnit, si.cost_price,
               COALESCE(si.unit_ratio, p.unit_ratio, 1) AS unit_ratio,
               COALESCE(p.product_type, 'goods') AS product_type
        FROM sale_items si
        LEFT JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = ?
        ORDER BY si.rowid
        """,
        (sale_id,),
    )
    rows = cursor.fetchall()
    restore_lines = []
    seen_pids = set()
    for row in rows:
        r = dict(row) if hasattr(row, 'keys') else row
        if not requires_stock_check(r.get('product_type')):
            continue
        pid = int(r['product_id'])
        qty_base = sale_base_qty(r['quantity'], r.get('UseSaleUnit'), r.get('unit_ratio'))
        if qty_base <= 0:
            continue
        restore_lines.append((pid, qty_base, float(r.get('cost_price') or 0)))
        seen_pids.add(pid)

    delete_sale_stock_moves(cursor, sale_id)
    delete_sale_inventory_transactions(cursor, sale_id)

    for pid, qty_base, sale_cost in restore_lines:
        restore_wac_after_sale_reversal(cursor, pid, qty_base, sale_cost)

    if product_ids is None:
        product_ids = list(seen_pids)
    sync_inventory_quantities(cursor, product_ids)


def reverse_import_moves_wac(cursor, import_id):
    """Hoàn WAC của các dòng nhập cũ trước khi sửa/xóa phiếu nhập."""
    cursor.execute(
        """
        SELECT product_id, quantity, cost_price
        FROM stock_moves
        WHERE ref_id = ? AND type = 'import' AND quantity > 0
        """,
        (import_id,),
    )
    pids = set()
    for row in cursor.fetchall():
        r = dict(row) if hasattr(row, 'keys') else row
        pid = int(r['product_id'])
        qty = float(r['quantity'] or 0)
        cost = float(r['cost_price'] or 0)
        if qty > 0:
            apply_wac_outbound(cursor, pid, qty, cost)
            pids.add(pid)
    return pids


def reconcile_all_inventory(cursor, product_type_filter=True):
    """
    Đối soát toàn bộ: inventory.quantity ← SUM(stock_moves).
    Trả về list dict sản phẩm đã sửa {product_id, old_qty, new_qty, diff}.
    """
    sql = """
        SELECT p.id AS product_id,
               COALESCE(i.quantity, 0) AS inv_qty,
               COALESCE((
                   SELECT SUM(sm.quantity) FROM stock_moves sm WHERE sm.product_id = p.id
               ), 0) AS ledger_qty
        FROM products p
        LEFT JOIN inventory i ON i.product_id = p.id
    """
    if product_type_filter:
        sql += " WHERE COALESCE(p.product_type, 'goods') != 'service'"
    cursor.execute(sql)
    fixes = []
    for row in cursor.fetchall():
        pid = row[0]
        old_qty = float(row[1] or 0)
        ledger_qty = float(row[2] or 0)
        if abs(old_qty - ledger_qty) < 0.0001:
            continue
        sync_inventory_quantity_from_moves(cursor, pid)
        fixes.append({
            'product_id': pid,
            'old_qty': old_qty,
            'new_qty': ledger_qty,
            'diff': ledger_qty - old_qty,
        })
    if fixes:
        cursor.execute(
            """
            SELECT p.id, p.product_code, p.name
            FROM products p
            WHERE p.id IN ({})
            """.format(','.join('?' * len(fixes))),
            [f['product_id'] for f in fixes],
        )
        meta = {r[0]: {'product_code': r[1] or '', 'name': r[2] or ''} for r in cursor.fetchall()}
        for f in fixes:
            m = meta.get(f['product_id'], {})
            f['product_code'] = m.get('product_code', '')
            f['product_name'] = m.get('name', '')
    return fixes


def sale_base_qty(quantity, use_sale_unit, unit_ratio):
    qty = float(quantity or 0)
    ratio = float(unit_ratio or 1) or 1.0
    if use_sale_unit in (1, True, '1'):
        return qty * ratio
    return qty


def import_base_qty(qty_input, unit_type, unit_ratio):
    """Quy đổi SL nhập/trả NCC về đơn vị lẻ."""
    qty = float(qty_input or 0)
    ratio = float(unit_ratio or 1) or 1.0
    if int(unit_type or 0) == 1:
        return qty * ratio
    return qty


def import_cost_to_base(cost_price, unit_type, unit_ratio):
    """cost_price trong import_details — luôn lưu theo đơn vị lẻ (base)."""
    return float(cost_price or 0)


def rebuild_wac_from_moves(cursor, product_id):
    """Tính lại avg_cost từ lịch sử stock_moves (theo thứ tự thời gian)."""
    cursor.execute(
        """
        SELECT quantity, cost_price FROM stock_moves
        WHERE product_id = ?
        ORDER BY date ASC, id ASC
        """,
        (product_id,),
    )
    Q = 0.0
    C = 0.0
    for row in cursor.fetchall():
        qty = float(row[0] if not hasattr(row, 'keys') else row['quantity'])
        move_cost = float(row[1] if not hasattr(row, 'keys') else row['cost_price'] or 0)
        if qty > 0:
            Q_new = Q + qty
            unit_in = move_cost if move_cost > 0 else C
            C = (Q * C + qty * unit_in) / Q_new if Q_new > 0 else unit_in
            Q = Q_new
        else:
            out = abs(qty)
            out_cost = move_cost if move_cost > 0 else C
            Q_before = Q
            if Q_before < out - 0.0001:
                continue
            Q = Q_before - out
            if Q <= 0.0001:
                Q = 0.0
                C = 0.0
            else:
                C = (Q_before * C - out * out_cost) / Q
    _set_avg_cost(cursor, product_id, C)
    sync_inventory_quantity_from_moves(cursor, product_id)
    return Q, C


def rebuild_all_wac_from_moves(cursor, product_type_filter=True):
    """Rebuild WAC cho mọi SP có stock_moves. Trả về list thay đổi."""
    sql = """
        SELECT DISTINCT p.id FROM products p
        INNER JOIN stock_moves sm ON sm.product_id = p.id
    """
    if product_type_filter:
        sql += " WHERE COALESCE(p.product_type, 'goods') != 'service'"
    cursor.execute(sql)
    fixes = []
    for row in cursor.fetchall():
        pid = row[0]
        cursor.execute("SELECT avg_cost FROM inventory WHERE product_id = ?", (pid,))
        old_row = cursor.fetchone()
        old_c = float(old_row[0] or 0) if old_row else 0.0
        Q, C = rebuild_wac_from_moves(cursor, pid)
        if abs(old_c - C) > 0.0001:
            fixes.append({'product_id': pid, 'old_wac': old_c, 'new_wac': C, 'qty': Q})
    return fixes


def collect_sale_item_base_qty(cursor, sale_id):
    """product_id → tổng số lượng đơn vị lẻ cần hoàn/xóa."""
    cursor.execute(
        """
        SELECT si.product_id, si.quantity, si.UseSaleUnit,
               COALESCE(p.unit_ratio, 1) AS unit_ratio,
               COALESCE(p.product_type, 'goods') AS product_type
        FROM sale_items si
        LEFT JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = ?
        """,
        (sale_id,),
    )
    out = {}
    for row in cursor.fetchall():
        r = dict(row) if hasattr(row, 'keys') else row
        pid = r['product_id']
        if not requires_stock_check(r.get('product_type')):
            continue
        base = sale_base_qty(r['quantity'], r.get('UseSaleUnit'), r.get('unit_ratio'))
        out[pid] = out.get(pid, 0.0) + base
    return out
