"""Facade giá vốn — WAC hoặc FIFO; tùy chọn theo dõi lô vận hành khi WAC."""
from __future__ import annotations

from typing import Any

from Services.inventory_cost_method import (
    is_fifo_mode,
    is_lot_ops_only,
    is_lot_tracking_enabled,
)
from Services.inventory_stock_helpers import (
    apply_wac_inbound,
    apply_wac_outbound,
    get_wac,
    sync_inventory_quantity_from_moves,
)


def apply_cost_inbound(
    cursor,
    product_id: int,
    qty_base: float,
    value_total: float,
    *,
    unit_cost: float | None = None,
    source_type: str = 'IMPORT',
    source_id: int | None = None,
    source_line_id: int | None = None,
    warehouse_code: str | None = None,
    received_at: str | None = None,
    lot_no: str | None = None,
    expiry_date: str | None = None,
    note: str | None = None,
    conn=None,
) -> float:
    """
    Nhập kho:
    - FIFO: tạo lô + đồng bộ avg từ lô
    - WAC + theo dõi lô: cập nhật WAC rồi tạo lô (không đè avg)
    - WAC thuần: chỉ WAC
    """
    qty_base = float(qty_base or 0)
    value_total = float(value_total or 0)
    if qty_base <= 0:
        return get_wac(cursor, product_id)

    db = conn or cursor.connection
    unit = float(unit_cost if unit_cost is not None else (value_total / qty_base if qty_base else 0))
    track = is_lot_tracking_enabled(db)
    fifo = is_fifo_mode(db)
    ops_only = is_lot_ops_only(db)

    if fifo:
        from Services.fifo_lots import create_lot

        create_lot(
            cursor,
            product_id=int(product_id),
            qty=qty_base,
            unit_cost=unit,
            source_type=source_type,
            source_id=source_id,
            source_line_id=source_line_id,
            warehouse_code=warehouse_code,
            received_at=received_at,
            lot_no=lot_no,
            expiry_date=expiry_date,
            note=note,
            update_avg_cost=True,
        )
        sync_inventory_quantity_from_moves(cursor, product_id)
        return unit

    # WAC (có hoặc không theo dõi lô)
    new_wac = apply_wac_inbound(cursor, product_id, qty_base, value_total)
    if track and ops_only:
        from Services.fifo_lots import create_lot

        create_lot(
            cursor,
            product_id=int(product_id),
            qty=qty_base,
            unit_cost=unit,
            source_type=source_type,
            source_id=source_id,
            source_line_id=source_line_id,
            warehouse_code=warehouse_code,
            received_at=received_at,
            lot_no=lot_no,
            expiry_date=expiry_date,
            note=note,
            update_avg_cost=False,
        )
    return new_wac


def apply_cost_outbound(
    cursor,
    product_id: int,
    qty_base: float,
    unit_cost: float | None = None,
    *,
    ref_type: str = 'sale',
    ref_id: int | None = None,
    stock_move_id: int | None = None,
    warehouse_code: str | None = None,
    actor_user_id: int | None = None,
    conn=None,
) -> tuple[float, float, list[dict[str, Any]]]:
    """
    Xuất kho. Trả về (new_wac_or_avg, cost_used, lot_consumptions).
    WAC + theo dõi lô: giá vốn vẫn WAC; consumptions chỉ để kiểm soát FIFO vận hành.
    """
    qty_base = float(qty_base or 0)
    if qty_base <= 0:
        c = get_wac(cursor, product_id)
        return c, c, []

    db = conn or cursor.connection

    if is_fifo_mode(db):
        from Services.fifo_lots import consume_fifo

        cost_used, details = consume_fifo(
            cursor,
            int(product_id),
            qty_base,
            ref_type=ref_type,
            ref_id=ref_id,
            stock_move_id=stock_move_id,
            warehouse_code=warehouse_code,
            actor_user_id=actor_user_id,
            update_avg_cost=True,
        )
        return get_wac(cursor, product_id), cost_used, details

    if unit_cost is not None:
        new_c, used = apply_wac_outbound(cursor, product_id, qty_base, float(unit_cost))
    else:
        new_c, used = apply_wac_outbound(cursor, product_id, qty_base, None)

    details: list[dict[str, Any]] = []
    if is_lot_ops_only(db):
        from Services.fifo_lots import consume_fifo

        try:
            _lot_cost, details = consume_fifo(
                cursor,
                int(product_id),
                qty_base,
                ref_type=ref_type,
                ref_id=ref_id,
                stock_move_id=stock_move_id,
                warehouse_code=warehouse_code,
                actor_user_id=actor_user_id,
                update_avg_cost=False,
            )
        except ValueError:
            # Chưa seed lô / lệch sổ — không chặn bán WAC
            details = []

    return new_c, used, details


def apply_cost_return_inbound(
    cursor,
    product_id: int,
    qty_base: float,
    sale_id: int,
    *,
    return_sales_id: int | None = None,
    unit_cost_fallback: float | None = None,
    actor_user_id: int | None = None,
    conn=None,
) -> float:
    """Khách trả hàng — hoàn lô (nếu theo dõi) + WAC hoặc FIFO theo method."""
    qty_base = float(qty_base or 0)
    if qty_base <= 0:
        return get_wac(cursor, product_id)

    db = conn or cursor.connection
    fallback = float(unit_cost_fallback or 0)

    if is_fifo_mode(db):
        from Services.fifo_lots import restore_to_lots_from_sale

        cost_used, _restored = restore_to_lots_from_sale(
            cursor,
            int(product_id),
            qty_base,
            int(sale_id),
            return_sales_id=return_sales_id,
            unit_cost_fallback=fallback,
            actor_user_id=actor_user_id,
            update_avg_cost=True,
        )
        sync_inventory_quantity_from_moves(cursor, product_id)
        return cost_used

    apply_wac_inbound(cursor, product_id, qty_base, qty_base * fallback)
    if is_lot_ops_only(db):
        from Services.fifo_lots import restore_to_lots_from_sale

        try:
            restore_to_lots_from_sale(
                cursor,
                int(product_id),
                qty_base,
                int(sale_id),
                return_sales_id=return_sales_id,
                unit_cost_fallback=fallback,
                actor_user_id=actor_user_id,
                update_avg_cost=False,
            )
        except Exception:
            pass
    sync_inventory_quantity_from_moves(cursor, product_id)
    return fallback


def cost_snapshot_for_sale(cursor, product_id: int, conn=None) -> float:
    """Giá vốn dự kiến khi bán — luôn theo method kế toán (WAC hoặc FIFO)."""
    db = conn or cursor.connection
    if is_fifo_mode(db):
        from Services.fifo_lots import _open_lots

        lots = _open_lots(cursor, int(product_id))
        if lots:
            row = lots[0]
            if hasattr(row, 'keys'):
                return float(row['unit_cost'] or 0)
            return float(row[2] or 0)
        return get_wac(cursor, product_id)
    return get_wac(cursor, product_id)


def apply_cost_value_adjustment(
    cursor,
    product_id: int,
    value_delta: float,
    *,
    prefer_source_id: int | None = None,
    prefer_source_type: str = 'IMPORT',
    conn=None,
) -> tuple[float, float, float]:
    """PBCP: FIFO → lô; WAC → avg; WAC+ops → WAC + cập nhật unit_cost lô (không đè avg)."""
    db = conn or cursor.connection
    if is_fifo_mode(db):
        from Services.fifo_lots import adjust_lot_unit_costs

        return adjust_lot_unit_costs(
            cursor,
            int(product_id),
            float(value_delta or 0),
            prefer_source_id=prefer_source_id,
            prefer_source_type=prefer_source_type,
            update_avg_cost=True,
        )

    from Services.inventory_stock_helpers import apply_wac_value_adjustment

    before, after, qty = apply_wac_value_adjustment(cursor, product_id, value_delta)
    if is_lot_ops_only(db):
        from Services.fifo_lots import adjust_lot_unit_costs

        try:
            adjust_lot_unit_costs(
                cursor,
                int(product_id),
                float(value_delta or 0),
                prefer_source_id=prefer_source_id,
                prefer_source_type=prefer_source_type,
                update_avg_cost=False,
            )
        except ValueError:
            pass
    return before, after, qty


def apply_cost_outbound_return_import(
    cursor,
    product_id: int,
    qty_base: float,
    unit_cost: float | None = None,
    *,
    import_id: int,
    ref_type: str = 'return_import',
    ref_id: int | None = None,
    stock_move_id: int | None = None,
    actor_user_id: int | None = None,
    conn=None,
) -> tuple[float, float]:
    """Trả NCC: FIFO / WAC+ops ưu tiên lô PN; giá vốn WAC dùng đơn giá PN."""
    qty_base = float(qty_base or 0)
    if qty_base <= 0:
        c = get_wac(cursor, product_id)
        return c, c

    db = conn or cursor.connection
    if is_fifo_mode(db):
        from Services.fifo_lots import consume_prefer_import_lot

        cost_used, _ = consume_prefer_import_lot(
            cursor,
            int(product_id),
            qty_base,
            import_id=int(import_id),
            ref_type=ref_type,
            ref_id=ref_id,
            stock_move_id=stock_move_id,
            actor_user_id=actor_user_id,
            update_avg_cost=True,
        )
        return get_wac(cursor, product_id), cost_used

    new_c, used = apply_wac_outbound(
        cursor, product_id, qty_base,
        float(unit_cost) if unit_cost is not None else None,
    )
    if is_lot_ops_only(db):
        from Services.fifo_lots import consume_prefer_import_lot

        try:
            consume_prefer_import_lot(
                cursor,
                int(product_id),
                qty_base,
                import_id=int(import_id),
                ref_type=ref_type,
                ref_id=ref_id,
                stock_move_id=stock_move_id,
                actor_user_id=actor_user_id,
                update_avg_cost=False,
            )
        except ValueError:
            pass
    return new_c, used


def reverse_import_cost(cursor, import_id: int, conn=None) -> set[int]:
    """Hủy/sửa PN: đảo WAC hoặc cắt/đóng lô thuộc PN."""
    from Services.inventory_stock_helpers import reverse_import_moves_wac

    db = conn or cursor.connection
    if not is_lot_tracking_enabled(db):
        return reverse_import_moves_wac(cursor, import_id)

    from Services.fifo_lots import consume_prefer_import_lot, sync_avg_cost_from_lots
    from Services.inventory_lot_schema import ensure_inventory_lot_schema

    ensure_inventory_lot_schema(db)
    ops_only = is_lot_ops_only(db)
    update_avg = not ops_only

    if ops_only:
        # Đảo WAC trước, rồi đóng lô (không đè avg)
        pids_wac = reverse_import_moves_wac(cursor, import_id)

    cursor.execute(
        """
        SELECT product_id, quantity, cost_price
        FROM stock_moves
        WHERE ref_id = ? AND type = 'import' AND quantity > 0
        """,
        (import_id,),
    )
    pids: set[int] = set()
    rows = cursor.fetchall()
    if ops_only:
        # stock_moves vẫn còn cho đến khi caller xóa — đọc qty từ đây
        pass

    for row in rows:
        r = dict(row) if hasattr(row, 'keys') else row
        pid = int(r['product_id'] if hasattr(r, 'keys') else r[0])
        qty = float((r['quantity'] if hasattr(r, 'keys') else r[1]) or 0)
        if qty <= 0:
            continue
        try:
            consume_prefer_import_lot(
                cursor, pid, qty,
                import_id=int(import_id),
                ref_type='import_void',
                ref_id=int(import_id),
                update_avg_cost=update_avg,
            )
        except ValueError:
            cursor.execute(
                """
                SELECT id, qty_remaining FROM inventory_lots
                WHERE product_id = ? AND source_type = 'IMPORT' AND source_id = ?
                  AND qty_remaining > 0
                """,
                (pid, int(import_id)),
            )
            for lot in cursor.fetchall():
                lot_id = int(lot[0] if not hasattr(lot, 'keys') else lot['id'])
                rem = float(lot[1] if not hasattr(lot, 'keys') else lot['qty_remaining'])
                take = min(qty, rem)
                if take <= 0:
                    continue
                cursor.execute(
                    """
                    UPDATE inventory_lots
                    SET qty_remaining = qty_remaining - ?,
                        status = CASE WHEN qty_remaining - ? <= 0.0001 THEN 'closed' ELSE 'open' END
                    WHERE id = ?
                    """,
                    (take, take, lot_id),
                )
                qty -= take
            if update_avg:
                sync_avg_cost_from_lots(cursor, pid)
        pids.add(pid)

    if ops_only:
        return set(pids_wac) | pids

    if not is_fifo_mode(db):
        return reverse_import_moves_wac(cursor, import_id) | pids
    return pids
