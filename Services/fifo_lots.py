"""Thao tác lô hàng — FEFO/FIFO vận hành, tạo lô, xuất, hoàn trả."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from Services.inventory_lot_schema import ensure_inventory_lot_schema
from Services.inventory_stock_helpers import _set_avg_cost, ledger_quantity

_LOT_ORDER_FIFO = 'received_at ASC, id ASC'
_LOT_ORDER_FEFO = (
    "CASE WHEN expiry_date IS NULL OR expiry_date = '' THEN 1 ELSE 0 END, "
    'expiry_date ASC, received_at ASC, id ASC'
)
_LOT_SELECT = (
    'id, qty_remaining, unit_cost, lot_no, received_at, source_type, expiry_date'
)


def _use_fefo_issue(conn: sqlite3.Connection) -> bool:
    from Services.inventory_cost_method import is_lot_tracking_enabled

    return is_lot_tracking_enabled(conn)


def _lot_order_sql(conn: sqlite3.Connection) -> str:
    return _LOT_ORDER_FEFO if _use_fefo_issue(conn) else _LOT_ORDER_FIFO


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    raw = str(value).strip()[:10]
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def expiry_meta(expiry_date: str | None, *, warn_days: int = 30) -> dict[str, Any]:
    """Trạng thái hạn dùng và mức ưu tiên bán (1 = cao nhất)."""
    exp = _parse_date(expiry_date)
    if exp is None:
        return {
            'days_to_expiry': None,
            'expiry_status': 'unknown',
            'sell_priority': 2,
            'expiry_label': 'Chưa có HSD',
        }
    days = (exp - date.today()).days
    if days < 0:
        status, priority, label = 'expired', 0, f'Quá hạn {abs(days)} ngày'
    elif days <= 7:
        status, priority, label = 'critical', 1, f'Còn {days} ngày'
    elif days <= warn_days:
        status, priority, label = 'warning', 1, f'Còn {days} ngày'
    else:
        status, priority, label = 'ok', 2, f'Còn {days} ngày'
    return {
        'days_to_expiry': days,
        'expiry_status': status,
        'sell_priority': priority,
        'expiry_label': label,
    }


LOT_SOURCE_LABELS: dict[str, str] = {
    'IMPORT': 'Nhập kho',
    'OPENING_BALANCE': 'Tồn đầu năm',
    'OPENING': 'Tồn đầu năm',
    'PRODUCTION': 'Sản xuất',
    'PRODUCTION_REVERSE': 'Hoàn sản xuất',
    'MATERIAL_ALLOC_VOID': 'Hoàn cấp NVL',
    'MATERIAL_ALLOC': 'Cấp NVL sản xuất',
    'TRANSIT': 'Nhập luồng',
    'RETURN': 'Trả hàng',
    'RETURN_SALE': 'Khách trả hàng',
    'RETURN_IMPORT': 'Trả NCC',
    'SALE': 'Bán hàng',
    'SALE_RECIPE': 'Bán (công thức)',
    'EXPORT': 'Xuất kho',
    'EXPORT_MATERIAL': 'Xuất NVL',
    'EXPORT_FOR_USE': 'Xuất sử dụng',
    'ADJUST': 'Điều chỉnh',
    'ADJUST_IN': 'Điều chỉnh tăng',
    'ADJUST_OUT': 'Điều chỉnh giảm',
    'ADJUSTMENT': 'Kiểm kê / điều chỉnh',
    'ADJUSTMENT_IN': 'Điều chỉnh tăng',
    'ADJUSTMENT_OUT': 'Điều chỉnh giảm',
    'TRANSFER': 'Chuyển kho',
    'STOCK_TRANSFER': 'Chuyển kho',
    'LANDED_COST': 'Chi phí mua hàng',
    'DELETE_IMPORT': 'Hủy phiếu nhập',
    'DELETE_SALE': 'Hủy bán hàng',
    'CONSIGN_RETURN': 'Nhập hàng ký gửi',
    'INITIAL': 'Tồn ban đầu',
    'MANUAL': 'Nhập tay',
}


def _normalize_lot_source_key(source_type: str | None) -> str:
    import re

    raw = str(source_type or '').strip()
    if not raw:
        return ''
    return re.sub(r'[\s\-]+', '_', raw).upper().strip('_')


def lot_source_type_label(source_type: str | None) -> str:
    """Nhãn nguồn lô tiếng Việt."""
    raw = str(source_type or '').strip()
    if not raw:
        return '—'
    norm = _normalize_lot_source_key(raw)
    if norm in LOT_SOURCE_LABELS:
        return LOT_SOURCE_LABELS[norm]
    # Đã là tiếng Việt (có dấu) — giữ nguyên
    if any('\u0100' <= ch <= '\u1ef9' or ch in 'đĐ' for ch in raw):
        return raw
    # Ghép từ khóa phổ biến: OPENING_BALANCE, RETURN_SALE, …
    _WORD_VI = {
        'OPENING': 'Tồn đầu',
        'BALANCE': 'năm',
        'IMPORT': 'Nhập kho',
        'EXPORT': 'Xuất kho',
        'RETURN': 'Trả',
        'SALE': 'bán hàng',
        'PRODUCTION': 'Sản xuất',
        'REVERSE': 'hoàn',
        'MATERIAL': 'NVL',
        'ALLOC': 'cấp phát',
        'VOID': 'hủy',
        'ADJUST': 'Điều chỉnh',
        'ADJUSTMENT': 'Điều chỉnh',
        'TRANSFER': 'Chuyển kho',
        'TRANSIT': 'Nhập luồng',
        'LANDED': 'Chi phí',
        'COST': 'mua hàng',
        'INITIAL': 'Tồn ban đầu',
        'MANUAL': 'Nhập tay',
        'DELETE': 'Hủy',
    }
    parts = [p for p in norm.split('_') if p]
    if parts and all(p in _WORD_VI for p in parts):
        return ' '.join(_WORD_VI[p] for p in parts).strip().capitalize()
    if len(parts) == 1 and parts[0] in _WORD_VI:
        return _WORD_VI[parts[0]]
    return raw.replace('_', ' ').replace('-', ' ')


def format_lot_date_vi(value: str | None) -> str | None:
    """Định dạng ngày kiểu Việt Nam (dd/mm/yyyy)."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = _parse_date(text[:10])
    if parsed:
        return parsed.strftime('%d/%m/%Y')
    if len(text) >= 10 and text[2] == '/':
        return text[:10]
    return text[:10]


def _enrich_lot_display(d: dict[str, Any]) -> dict[str, Any]:
    d['source_type_label'] = lot_source_type_label(d.get('source_type'))
    d['received_at_display'] = format_lot_date_vi(d.get('received_at')) or '—'
    d['expiry_date_display'] = format_lot_date_vi(d.get('expiry_date')) or '—'
    return d


def _row_val(row, key, idx=0):
    if row is None:
        return None
    if hasattr(row, 'keys'):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            pass
    try:
        return row[idx]
    except (KeyError, IndexError, TypeError):
        return None


def sync_avg_cost_from_lots(cursor, product_id: int) -> float:
    """Cập nhật inventory.avg_cost = giá trị lô còn / SL lô (báo cáo)."""
    cursor.execute(
        """
        SELECT COALESCE(SUM(qty_remaining), 0), COALESCE(SUM(qty_remaining * unit_cost), 0)
        FROM inventory_lots
        WHERE product_id = ? AND qty_remaining > 0
        """,
        (product_id,),
    )
    row = cursor.fetchone()
    qty = float(_row_val(row, 0, 0) or 0)
    val = float(_row_val(row, 1, 1) or 0)
    avg = (val / qty) if qty > 0 else 0.0
    _set_avg_cost(cursor, product_id, avg)
    return avg


def create_lot(
    cursor,
    *,
    product_id: int,
    qty: float,
    unit_cost: float,
    source_type: str,
    received_at: str | None = None,
    source_id: int | None = None,
    source_line_id: int | None = None,
    warehouse_code: str | None = None,
    lot_no: str | None = None,
    expiry_date: str | None = None,
    note: str | None = None,
    update_avg_cost: bool = True,
) -> int:
    ensure_inventory_lot_schema(cursor.connection)
    qty = float(qty or 0)
    unit_cost = float(unit_cost or 0)
    if qty <= 0:
        raise ValueError('Số lượng lô phải > 0')
    received = received_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute(
        """
        INSERT INTO inventory_lots (
            product_id, warehouse_code, source_type, source_id, source_line_id,
            lot_no, received_at, expiry_date, qty_in, qty_remaining, unit_cost,
            status, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            int(product_id),
            warehouse_code,
            source_type,
            source_id,
            source_line_id,
            lot_no,
            received,
            expiry_date,
            qty,
            qty,
            unit_cost,
            note,
        ),
    )
    lot_id = int(cursor.lastrowid)
    if update_avg_cost:
        sync_avg_cost_from_lots(cursor, int(product_id))
    return lot_id


def _open_lots(cursor, product_id: int, warehouse_code: str | None = None):
    conn = cursor.connection
    order = _lot_order_sql(conn)
    if warehouse_code:
        cursor.execute(
            f"""
            SELECT {_LOT_SELECT}
            FROM inventory_lots
            WHERE product_id = ? AND qty_remaining > 0
              AND (warehouse_code IS NULL OR warehouse_code = ? OR warehouse_code = '')
            ORDER BY {order}
            """,
            (int(product_id), warehouse_code),
        )
    else:
        cursor.execute(
            f"""
            SELECT {_LOT_SELECT}
            FROM inventory_lots
            WHERE product_id = ? AND qty_remaining > 0
            ORDER BY {order}
            """,
            (int(product_id),),
        )
    return cursor.fetchall()


def consume_fifo(
    cursor,
    product_id: int,
    qty_needed: float,
    *,
    ref_type: str,
    ref_id: int | None = None,
    stock_move_id: int | None = None,
    warehouse_code: str | None = None,
    actor_user_id: int | None = None,
    update_avg_cost: bool = True,
) -> tuple[float, list[dict[str, Any]]]:
    """
    Cắt lô FIFO. Trả về (unit_cost_weighted, consumptions).
    update_avg_cost=False khi WAC + theo dõi lô vận hành (giữ avg_cost WAC).
    """
    ensure_inventory_lot_schema(cursor.connection)
    qty_needed = float(qty_needed or 0)
    if qty_needed <= 0:
        return 0.0, []

    lots = _open_lots(cursor, product_id, warehouse_code)
    available = sum(float(_row_val(r, 'qty_remaining', 1) or 0) for r in lots)
    ledger_qty = ledger_quantity(cursor, product_id)
    if ledger_qty + 0.0001 < qty_needed:
        raise ValueError(
            f'Tồn kho không đủ (SP #{product_id}: cần {qty_needed}, còn {ledger_qty})'
        )
    if available + 0.0001 < qty_needed:
        raise ValueError(
            f'Lô FIFO không đủ (SP #{product_id}: cần {qty_needed}, lô còn {available}). '
            f'Chạy seed lô tồn đầu năm hoặc kiểm tra nhập kho.'
        )

    remaining = qty_needed
    total_cost = 0.0
    consumptions: list[dict[str, Any]] = []

    for lot in lots:
        if remaining <= 1e-9:
            break
        lot_id = int(_row_val(lot, 'id', 0))
        lot_qty = float(_row_val(lot, 'qty_remaining', 1) or 0)
        unit_cost = float(_row_val(lot, 'unit_cost', 2) or 0)
        if lot_qty <= 0:
            continue
        take = min(remaining, lot_qty)
        new_rem = lot_qty - take
        status = 'closed' if new_rem <= 1e-9 else 'open'
        cursor.execute(
            """
            UPDATE inventory_lots
            SET qty_remaining = ?, status = ?
            WHERE id = ?
            """,
            (new_rem, status, lot_id),
        )
        cursor.execute(
            """
            INSERT INTO inventory_lot_consumptions (
                lot_id, product_id, direction, qty, unit_cost,
                ref_type, ref_id, stock_move_id, actor_user_id
            ) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?)
            """,
            (
                lot_id, int(product_id), take, unit_cost,
                ref_type, ref_id, stock_move_id, actor_user_id,
            ),
        )
        cons_id = int(cursor.lastrowid)
        consumptions.append({
            'consumption_id': cons_id,
            'lot_id': lot_id,
            'qty': take,
            'unit_cost': unit_cost,
            'lot_no': _row_val(lot, 'lot_no', 3),
            'received_at': _row_val(lot, 'received_at', 4),
            'expiry_date': _row_val(lot, 'expiry_date', 6),
        })
        total_cost += take * unit_cost
        remaining -= take

    if remaining > 0.0001:
        raise ValueError(f'Không đủ lô FIFO cho SP #{product_id} (thiếu {remaining})')

    weighted_cost = total_cost / qty_needed if qty_needed > 0 else 0.0
    if update_avg_cost:
        sync_avg_cost_from_lots(cursor, int(product_id))
    return weighted_cost, consumptions


def restore_to_lots_from_sale(
    cursor,
    product_id: int,
    qty_return: float,
    sale_id: int,
    *,
    return_sales_id: int | None = None,
    unit_cost_fallback: float | None = None,
    actor_user_id: int | None = None,
    update_avg_cost: bool = True,
) -> tuple[float, list[dict[str, Any]]]:
    """
    Hoàn hàng vào lô cũ (LIFO trên consumptions out của sale).
    Đơn WAC cũ → lô OPENING_BALANCE.
    """
    ensure_inventory_lot_schema(cursor.connection)
    qty_return = float(qty_return or 0)
    if qty_return <= 0:
        return 0.0, []

    cursor.execute(
        """
        SELECT c.id, c.lot_id, c.qty, c.unit_cost, l.source_type
        FROM inventory_lot_consumptions c
        JOIN inventory_lots l ON l.id = c.lot_id
        WHERE c.product_id = ? AND c.direction = 'out'
          AND c.ref_type IN ('sale', 'SALE', 'export')
          AND c.ref_id = ?
        ORDER BY c.id DESC
        """,
        (int(product_id), int(sale_id)),
    )
    outs = cursor.fetchall()

    remaining = qty_return
    total_cost = 0.0
    restored: list[dict[str, Any]] = []

    if outs:
        for row in outs:
            if remaining <= 1e-9:
                break
            cons_id = int(_row_val(row, 'id', 0))
            lot_id = int(_row_val(row, 'lot_id', 1))
            out_qty = float(_row_val(row, 'qty', 2) or 0)
            unit_cost = float(_row_val(row, 'unit_cost', 3) or 0)

            cursor.execute(
                """
                SELECT COALESCE(SUM(qty), 0) FROM inventory_lot_consumptions
                WHERE reversed_consumption_id = ? AND direction = 'in'
                """,
                (cons_id,),
            )
            already = float(_row_val(cursor.fetchone(), 0, 0) or 0)
            can_restore = max(0.0, out_qty - already)
            if can_restore <= 0:
                continue
            take = min(remaining, can_restore)

            cursor.execute(
                """
                UPDATE inventory_lots
                SET qty_remaining = qty_remaining + ?, status = 'open'
                WHERE id = ?
                """,
                (take, lot_id),
            )
            cursor.execute(
                """
                INSERT INTO inventory_lot_consumptions (
                    lot_id, product_id, direction, qty, unit_cost,
                    ref_type, ref_id, return_sales_id, reversed_consumption_id, actor_user_id
                ) VALUES (?, ?, 'in', ?, ?, 'return_sale', ?, ?, ?, ?)
                """,
                (
                    lot_id, int(product_id), take, unit_cost,
                    sale_id, return_sales_id, cons_id, actor_user_id,
                ),
            )
            restored.append({
                'lot_id': lot_id,
                'qty': take,
                'unit_cost': unit_cost,
                'source': 'sale_consumption',
            })
            total_cost += take * unit_cost
            remaining -= take

    if remaining > 1e-9:
        cost = float(unit_cost_fallback or 0)
        lot_id = _find_or_create_opening_lot(
            cursor, int(product_id), cost, qty_add=remaining,
        )
        cursor.execute(
            """
            INSERT INTO inventory_lot_consumptions (
                lot_id, product_id, direction, qty, unit_cost,
                ref_type, ref_id, return_sales_id, actor_user_id
            ) VALUES (?, ?, 'in', ?, ?, 'return_sale_legacy', ?, ?, ?)
            """,
            (
                lot_id, int(product_id), remaining, cost,
                sale_id, return_sales_id, actor_user_id,
            ),
        )
        restored.append({
            'lot_id': lot_id,
            'qty': remaining,
            'unit_cost': cost,
            'source': 'opening_balance',
        })
        total_cost += remaining * cost

    if update_avg_cost:
        sync_avg_cost_from_lots(cursor, int(product_id))
    weighted = total_cost / qty_return if qty_return > 0 else 0.0
    return weighted, restored


def _find_or_create_opening_lot(
    cursor, product_id: int, unit_cost: float, *, qty_add: float = 0.0,
) -> int:
    """Tìm lô tồn đầu năm; nếu chưa có thì tạo. Cộng qty_add vào qty_remaining."""
    year = datetime.now().year
    qty_add = float(qty_add or 0)
    cursor.execute(
        """
        SELECT id, qty_remaining FROM inventory_lots
        WHERE product_id = ? AND source_type = 'OPENING_BALANCE'
        ORDER BY received_at DESC, id DESC
        LIMIT 1
        """,
        (product_id,),
    )
    row = cursor.fetchone()
    if row:
        lot_id = int(_row_val(row, 'id', 0))
        if qty_add > 0:
            cursor.execute(
                """
                UPDATE inventory_lots
                SET qty_remaining = qty_remaining + ?, status = 'open'
                WHERE id = ?
                """,
                (qty_add, lot_id),
            )
        return lot_id
    if qty_add <= 0:
        qty_add = 0.0001
    return create_lot(
        cursor,
        product_id=product_id,
        qty=qty_add,
        unit_cost=unit_cost,
        source_type='OPENING_BALANCE',
        source_id=year,
        received_at=f'{year:04d}-01-01',
        lot_no=f'TD-{year}-{product_id}',
        note='Lô tồn đầu năm / hoàn trả đơn WAC cũ',
    )


def adjust_lot_unit_costs(
    cursor,
    product_id: int,
    value_delta: float,
    *,
    prefer_source_id: int | None = None,
    prefer_source_type: str = 'IMPORT',
    update_avg_cost: bool = True,
) -> tuple[float, float, float]:
    """
    Phân bổ ΔV vào unit_cost các lô còn tồn (ưu tiên lô cùng PN).
    Trả về (avg_before, avg_after, qty).
    """
    ensure_inventory_lot_schema(cursor.connection)
    value_delta = float(value_delta or 0)
    pid = int(product_id)
    Q = ledger_quantity(cursor, pid)
    before = 0.0
    cursor.execute(
        """
        SELECT COALESCE(SUM(qty_remaining * unit_cost), 0), COALESCE(SUM(qty_remaining), 0)
        FROM inventory_lots WHERE product_id = ? AND qty_remaining > 0
        """,
        (pid,),
    )
    row = cursor.fetchone()
    val = float(_row_val(row, 0, 0) or 0)
    lot_qty = float(_row_val(row, 1, 1) or 0)
    before = (val / lot_qty) if lot_qty > 0 else 0.0
    if abs(value_delta) < 1e-9:
        return before, before, Q
    if lot_qty <= 1e-9:
        raise ValueError(
            f'Không thể vốn hóa CP vào SP #{pid}: không còn lô tồn'
        )

    lots = []
    if prefer_source_id is not None:
        cursor.execute(
            """
            SELECT id, qty_remaining, unit_cost FROM inventory_lots
            WHERE product_id = ? AND qty_remaining > 0
              AND source_type = ? AND source_id = ?
            ORDER BY received_at ASC, id ASC
            """,
            (pid, prefer_source_type, int(prefer_source_id)),
        )
        lots = cursor.fetchall()
    if not lots:
        cursor.execute(
            """
            SELECT id, qty_remaining, unit_cost FROM inventory_lots
            WHERE product_id = ? AND qty_remaining > 0
            ORDER BY received_at ASC, id ASC
            """,
            (pid,),
        )
        lots = cursor.fetchall()

    total_q = sum(float(_row_val(l, 'qty_remaining', 1) or 0) for l in lots)
    if total_q <= 1e-9:
        raise ValueError(f'Không thể vốn hóa CP vào SP #{pid}: lô còn = 0')

    for lot in lots:
        lot_id = int(_row_val(lot, 'id', 0))
        q = float(_row_val(lot, 'qty_remaining', 1) or 0)
        if q <= 0:
            continue
        share = value_delta * (q / total_q)
        new_unit = float(_row_val(lot, 'unit_cost', 2) or 0) + (share / q)
        cursor.execute(
            'UPDATE inventory_lots SET unit_cost = ? WHERE id = ?',
            (new_unit, lot_id),
        )
    after = before
    if update_avg_cost:
        after = sync_avg_cost_from_lots(cursor, pid)
    else:
        # Báo cáo: trung bình lô sau điều chỉnh (không ghi đè WAC)
        cursor.execute(
            """
            SELECT COALESCE(SUM(qty_remaining * unit_cost), 0), COALESCE(SUM(qty_remaining), 0)
            FROM inventory_lots WHERE product_id = ? AND qty_remaining > 0
            """,
            (pid,),
        )
        r2 = cursor.fetchone()
        v2 = float(_row_val(r2, 0, 0) or 0)
        q2 = float(_row_val(r2, 1, 1) or 0)
        after = (v2 / q2) if q2 > 0 else before
    return before, after, Q


def consume_prefer_import_lot(
    cursor,
    product_id: int,
    qty_needed: float,
    *,
    import_id: int,
    ref_type: str,
    ref_id: int | None = None,
    stock_move_id: int | None = None,
    actor_user_id: int | None = None,
    update_avg_cost: bool = True,
) -> tuple[float, list[dict[str, Any]]]:
    """Trả NCC: ưu tiên cắt lô của PN gốc, thiếu thì FIFO các lô còn lại."""
    ensure_inventory_lot_schema(cursor.connection)
    qty_needed = float(qty_needed or 0)
    if qty_needed <= 0:
        return 0.0, []

    remaining = qty_needed
    total_cost = 0.0
    consumptions: list[dict[str, Any]] = []

    cursor.execute(
        """
        SELECT id, qty_remaining, unit_cost, lot_no, received_at
        FROM inventory_lots
        WHERE product_id = ? AND qty_remaining > 0
          AND source_type = 'IMPORT' AND source_id = ?
        ORDER BY received_at ASC, id ASC
        """,
        (int(product_id), int(import_id)),
    )
    preferred = list(cursor.fetchall())
    cursor.execute(
        """
        SELECT id, qty_remaining, unit_cost, lot_no, received_at
        FROM inventory_lots
        WHERE product_id = ? AND qty_remaining > 0
          AND NOT (source_type = 'IMPORT' AND source_id = ?)
        ORDER BY received_at ASC, id ASC
        """,
        (int(product_id), int(import_id)),
    )
    others = list(cursor.fetchall())
    lots = preferred + others

    available = sum(float(_row_val(r, 'qty_remaining', 1) or 0) for r in lots)
    ledger_qty = ledger_quantity(cursor, product_id)
    if ledger_qty + 0.0001 < qty_needed:
        raise ValueError(
            f'Tồn kho không đủ (SP #{product_id}: cần {qty_needed}, còn {ledger_qty})'
        )
    if available + 0.0001 < qty_needed:
        raise ValueError(
            f'Lô FIFO không đủ để trả NCC (SP #{product_id}: cần {qty_needed}, lô còn {available})'
        )

    for lot in lots:
        if remaining <= 1e-9:
            break
        lot_id = int(_row_val(lot, 'id', 0))
        lot_qty = float(_row_val(lot, 'qty_remaining', 1) or 0)
        unit_cost = float(_row_val(lot, 'unit_cost', 2) or 0)
        if lot_qty <= 0:
            continue
        take = min(remaining, lot_qty)
        new_rem = lot_qty - take
        status = 'closed' if new_rem <= 1e-9 else 'open'
        cursor.execute(
            'UPDATE inventory_lots SET qty_remaining = ?, status = ? WHERE id = ?',
            (new_rem, status, lot_id),
        )
        cursor.execute(
            """
            INSERT INTO inventory_lot_consumptions (
                lot_id, product_id, direction, qty, unit_cost,
                ref_type, ref_id, stock_move_id, actor_user_id
            ) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?)
            """,
            (
                lot_id, int(product_id), take, unit_cost,
                ref_type, ref_id, stock_move_id, actor_user_id,
            ),
        )
        consumptions.append({
            'consumption_id': int(cursor.lastrowid),
            'lot_id': lot_id,
            'qty': take,
            'unit_cost': unit_cost,
        })
        total_cost += take * unit_cost
        remaining -= take

    if remaining > 0.0001:
        raise ValueError(f'Không đủ lô để trả NCC SP #{product_id}')

    weighted = total_cost / qty_needed if qty_needed else 0.0
    if update_avg_cost:
        sync_avg_cost_from_lots(cursor, int(product_id))
    return weighted, consumptions


def reverse_return_lots(
    cursor,
    return_sales_id: int,
    sale_id: int,
    product_id: int,
) -> None:
    """Hủy phiếu trả: đảo hoàn lô."""
    cursor.execute(
        """
        SELECT id, lot_id, qty FROM inventory_lot_consumptions
        WHERE return_sales_id = ? AND direction = 'in' AND product_id = ?
        """,
        (int(return_sales_id), int(product_id)),
    )
    for row in cursor.fetchall():
        cons_id = int(_row_val(row, 'id', 0))
        lot_id = int(_row_val(row, 'lot_id', 1))
        qty = float(_row_val(row, 'qty', 2) or 0)
        cursor.execute(
            """
            UPDATE inventory_lots
            SET qty_remaining = CASE WHEN qty_remaining - ? <= 0.0001 THEN 0 ELSE qty_remaining - ? END,
                status = CASE WHEN qty_remaining - ? <= 0.0001 THEN 'closed' ELSE 'open' END
            WHERE id = ?
            """,
            (qty, qty, qty, lot_id),
        )
        cursor.execute('DELETE FROM inventory_lot_consumptions WHERE id = ?', (cons_id,))
    sync_avg_cost_from_lots(cursor, int(product_id))


def _products_columns(conn: sqlite3.Connection) -> set[str]:
    from db.schema_helpers import table_cols
    return table_cols(conn, 'products')


def _lot_matches_product_query(row: dict[str, Any], q: str | None) -> bool:
    """Lọc theo tên / mã / barcode SP hoặc mã lô (Python)."""
    text = (q or '').strip().lower()
    if not text:
        return True
    for f in (
        row.get('product_name'),
        row.get('product_code'),
        row.get('barcode'),
        row.get('lot_no'),
        row.get('product_id'),
    ):
        if f is None:
            continue
        if text in str(f).lower():
            return True
    return False


def list_lots(
    conn: sqlite3.Connection,
    *,
    product_id: int | None = None,
    q: str | None = None,
    only_open: bool = False,
    fiscal_year: int | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    ensure_inventory_lot_schema(conn)
    clauses = ['1=1']
    params: list[Any] = []
    if product_id:
        clauses.append('l.product_id = ?')
        params.append(int(product_id))
    if only_open:
        clauses.append('l.qty_remaining > 0')
    if fiscal_year:
        clauses.append('substr(l.received_at, 1, 4) = ?')
        params.append(str(int(fiscal_year)))
    where = ' AND '.join(clauses)
    conn_order = _lot_order_sql(conn)
    pcols = _products_columns(conn)
    code_sel = 'p.product_code' if 'product_code' in pcols else "'' AS product_code"
    barcode_sel = 'p.barcode' if 'barcode' in pcols else "NULL AS barcode"
    # Có từ khóa: lấy rộng rồi lọc Python (tránh SQL LIKE lỗi / cột thiếu)
    fetch_limit = int(limit)
    fetch_offset = int(offset)
    if (q or '').strip():
        fetch_limit = min(max(fetch_limit, 2000), 5000)
        fetch_offset = 0
    rows = conn.execute(
        f"""
        SELECT l.*, p.name AS product_name, {code_sel}, {barcode_sel}
        FROM inventory_lots l
        LEFT JOIN products p ON p.id = l.product_id
        WHERE {where}
        ORDER BY {conn_order}
        LIMIT ? OFFSET ?
        """,
        params + [fetch_limit, fetch_offset],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r) if hasattr(r, 'keys') else {}
        if not d:
            continue
        age_days = None
        try:
            recv = str(d.get('received_at') or '')[:10]
            if recv:
                age_days = (datetime.now().date() - datetime.strptime(recv, '%Y-%m-%d').date()).days
        except ValueError:
            pass
        d['age_days'] = age_days
        d['value_remaining'] = float(d.get('qty_remaining') or 0) * float(d.get('unit_cost') or 0)
        d.update(expiry_meta(d.get('expiry_date')))
        _enrich_lot_display(d)
        out.append(d)
    if (q or '').strip():
        out = [d for d in out if _lot_matches_product_query(d, q)]
        out = out[int(offset): int(offset) + int(limit)]
    return out


def update_lot_expiry(cursor, lot_id: int, expiry_date: str | None) -> None:
    """Cập nhật hạn sử dụng cho lô (định dạng YYYY-MM-DD hoặc null)."""
    ensure_inventory_lot_schema(cursor.connection)
    exp = None
    if expiry_date:
        parsed = _parse_date(expiry_date)
        if not parsed:
            raise ValueError('Ngày hết hạn không hợp lệ (YYYY-MM-DD)')
        exp = parsed.strftime('%Y-%m-%d')
    cursor.execute(
        'UPDATE inventory_lots SET expiry_date = ? WHERE id = ?',
        (exp, int(lot_id)),
    )


def lot_physical_reconcile(
    conn: sqlite3.Connection,
    *,
    tolerance: float = 0.0001,
) -> list[dict[str, Any]]:
    """So sánh tổng SL lô còn vs tồn sổ kho — phát hiện lệch vật lý."""
    from Services.inventory_cost_method import is_lot_tracking_enabled

    if not is_lot_tracking_enabled(conn):
        return []
    ensure_inventory_lot_schema(conn)
    cursor = conn.cursor()
    pids: set[int] = set()
    for row in conn.execute('SELECT DISTINCT product_id FROM inventory_lots'):
        pids.add(int(row[0]))
    for row in conn.execute(
        "SELECT DISTINCT product_id FROM stock_moves WHERE product_id IS NOT NULL"
    ):
        pids.add(int(row[0]))
    mismatches: list[dict[str, Any]] = []
    for pid in sorted(pids):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(qty_remaining), 0)
            FROM inventory_lots
            WHERE product_id = ? AND qty_remaining > 0
            """,
            (pid,),
        ).fetchone()
        lot_qty = float(_row_val(row, 0, 0) or 0)
        ledger_qty = float(ledger_quantity(cursor, pid) or 0)
        if abs(lot_qty - ledger_qty) <= tolerance:
            continue
        prod = conn.execute(
            'SELECT name, product_code FROM products WHERE id = ?',
            (pid,),
        ).fetchone()
        mismatches.append({
            'product_id': pid,
            'product_name': _row_val(prod, 'name', 0) if prod else '',
            'product_code': _row_val(prod, 'product_code', 1) if prod else '',
            'lot_qty': lot_qty,
            'ledger_qty': ledger_qty,
            'diff': round(lot_qty - ledger_qty, 4),
        })
    return mismatches


def expiry_alerts(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    only_open: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Lô sắp hết hạn / đã quá hạn — phục vụ kế hoạch tiêu thụ."""
    ensure_inventory_lot_schema(conn)
    clauses = ["expiry_date IS NOT NULL", "expiry_date != ''"]
    if only_open:
        clauses.append('qty_remaining > 0')
    where = ' AND '.join(clauses)
    rows = conn.execute(
        f"""
        SELECT l.*, p.name AS product_name, p.product_code
        FROM inventory_lots l
        LEFT JOIN products p ON p.id = l.product_id
        WHERE {where}
        ORDER BY l.expiry_date ASC, l.received_at ASC, l.id ASC
        LIMIT ?
        """,
        (int(limit * 3),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        meta = expiry_meta(d.get('expiry_date'), warn_days=int(days))
        if meta['expiry_status'] in ('expired', 'critical', 'warning'):
            d.update(meta)
            d['value_remaining'] = float(d.get('qty_remaining') or 0) * float(d.get('unit_cost') or 0)
            _enrich_lot_display(d)
            out.append(d)
        if len(out) >= limit:
            break
    return out


def consumption_plan(
    conn: sqlite3.Connection,
    *,
    product_id: int | None = None,
    q: str | None = None,
    only_open: bool = True,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Thứ tự ưu tiên xuất/bán: FEFO (HSD sớm trước), sau đó nhập cũ."""
    ensure_inventory_lot_schema(conn)
    clauses = ['1=1']
    params: list[Any] = []
    if product_id:
        clauses.append('l.product_id = ?')
        params.append(int(product_id))
    if only_open:
        clauses.append('l.qty_remaining > 0')
    order = _lot_order_sql(conn)
    pcols = _products_columns(conn)
    code_sel = 'p.product_code' if 'product_code' in pcols else "'' AS product_code"
    barcode_sel = 'p.barcode' if 'barcode' in pcols else "NULL AS barcode"
    fetch_limit = min(max(int(limit), 2000), 5000) if (q or '').strip() else int(limit)
    rows = conn.execute(
        f"""
        SELECT l.*, p.name AS product_name, {code_sel}, {barcode_sel}
        FROM inventory_lots l
        LEFT JOIN products p ON p.id = l.product_id
        WHERE {' AND '.join(clauses)}
        ORDER BY l.product_id ASC, {order}
        LIMIT ?
        """,
        params + [fetch_limit],
    ).fetchall()
    out: list[dict[str, Any]] = []
    rank_by_product: dict[int, int] = {}
    for r in rows:
        d = dict(r)
        if (q or '').strip() and not _lot_matches_product_query(d, q):
            continue
        pid = int(d.get('product_id') or 0)
        rank_by_product[pid] = rank_by_product.get(pid, 0) + 1
        d['sell_rank'] = rank_by_product[pid]
        d.update(expiry_meta(d.get('expiry_date')))
        d['value_remaining'] = float(d.get('qty_remaining') or 0) * float(d.get('unit_cost') or 0)
        _enrich_lot_display(d)
        out.append(d)
        if len(out) >= int(limit):
            break
    return out


def fifo_violations(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Cảnh báo: xuất lô không theo thứ tự FEFO/FIFO vận hành."""
    ensure_inventory_lot_schema(conn)
    fy = fiscal_year or datetime.now().year
    fefo = _use_fefo_issue(conn)
    if fefo:
        priority_cmp = """
            (
              COALESCE(NULLIF(ol.expiry_date, ''), '9999-12-31') < COALESCE(NULLIF(l.expiry_date, ''), '9999-12-31')
              OR (
                COALESCE(NULLIF(ol.expiry_date, ''), '9999-12-31') = COALESCE(NULLIF(l.expiry_date, ''), '9999-12-31')
                AND ol.received_at < l.received_at
              )
            )
        """
        violation_type = 'fefo'
    else:
        priority_cmp = 'ol.received_at < l.received_at'
        violation_type = 'fifo'
    rows = conn.execute(
        f"""
        SELECT c.id, c.product_id, p.name AS product_name, c.ref_type, c.ref_id,
               c.qty, c.unit_cost, c.created_at, l.lot_no, l.received_at, l.expiry_date,
               (
                 SELECT COUNT(*) FROM inventory_lots ol
                 WHERE ol.product_id = c.product_id
                   AND ol.qty_remaining > 0
                   AND ol.id != l.id
                   AND {priority_cmp}
               ) AS higher_priority_open_lots
        FROM inventory_lot_consumptions c
        JOIN inventory_lots l ON l.id = c.lot_id
        LEFT JOIN products p ON p.id = c.product_id
        WHERE c.direction = 'out'
          AND substr(c.created_at, 1, 4) = ?
        ORDER BY c.created_at DESC
        LIMIT ?
        """,
        (str(int(fy)), int(limit)),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if int(d.get('higher_priority_open_lots') or 0) > 0:
            d['violation'] = True
            d['violation_type'] = violation_type
            d.update(expiry_meta(d.get('expiry_date')))
            d['created_at_display'] = format_lot_date_vi(d.get('created_at')) or '—'
            d['received_at_display'] = format_lot_date_vi(d.get('received_at')) or '—'
            out.append(d)
    return out
