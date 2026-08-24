"""Cài đặt phương pháp giá vốn WAC / FIFO và khóa theo năm tài chính."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any

from Services.sme.period_lock import is_year_locked, year_end_date
from db_utils import sqlite_commit

METHOD_WAC = 'wac'
METHOD_FIFO = 'fifo'
VALID_METHODS = (METHOD_WAC, METHOD_FIFO)

KEY_METHOD = 'inventory_cost_method'
KEY_EFFECTIVE_YEAR = 'inventory_cost_method_effective_year'
KEY_LOCKED_AT = 'inventory_cost_method_locked_at'
KEY_HISTORY = 'inventory_cost_method_history'
KEY_LOT_OPS = 'inventory_lot_ops_tracking'  # '1' = theo dõi lô vận hành khi WAC


def _get_setting(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    if not row:
        return default
    val = row[0] if not hasattr(row, 'keys') else row['value']
    return str(val or default)


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
        (key, value),
    )


def get_inventory_cost_method(conn: sqlite3.Connection) -> str:
    raw = (_get_setting(conn, KEY_METHOD, METHOD_WAC) or METHOD_WAC).strip().lower()
    return raw if raw in VALID_METHODS else METHOD_WAC


def get_cost_method_effective_year(conn: sqlite3.Connection) -> int | None:
    raw = _get_setting(conn, KEY_EFFECTIVE_YEAR, '')
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_fifo_mode(conn: sqlite3.Connection) -> bool:
    return get_inventory_cost_method(conn) == METHOD_FIFO


def is_lot_ops_tracking(conn: sqlite3.Connection) -> bool:
    """Theo dõi lô vận hành (kiểm soát xuất cũ trước) — độc lập với giá vốn khi WAC."""
    return (_get_setting(conn, KEY_LOT_OPS, '0') or '0').strip() in ('1', 'true', 'True', 'yes')


def is_lot_tracking_enabled(conn: sqlite3.Connection) -> bool:
    """Có ghi nhận lô: FIFO (giá vốn) hoặc WAC + bật theo dõi vận hành."""
    return is_fifo_mode(conn) or is_lot_ops_tracking(conn)


def is_lot_ops_only(conn: sqlite3.Connection) -> bool:
    """Lô chỉ để theo dõi — giá vốn vẫn WAC."""
    return (not is_fifo_mode(conn)) and is_lot_ops_tracking(conn)


def set_lot_ops_tracking(
    conn: sqlite3.Connection,
    enabled: bool,
    *,
    changed_by: str | None = None,
) -> dict[str, Any]:
    """Bật/tắt theo dõi lô khi đang WAC. Có thể đổi bất kỳ lúc nào (không ảnh hưởng sổ giá vốn)."""
    if is_fifo_mode(conn):
        # FIFO luôn theo dõi lô — tắt cờ ops không có tác dụng
        _set_setting(conn, KEY_LOT_OPS, '0')
        sqlite_commit(conn, label='lot_ops_tracking')
        return cost_method_status(conn)

    was = is_lot_ops_tracking(conn)
    _set_setting(conn, KEY_LOT_OPS, '1' if enabled else '0')
    seeded = []
    if enabled and not was:
        ensure_inventory_lot_schema(conn)
        seeded = seed_opening_balance_lots(conn, date.today().year)
    sqlite_commit(conn, label='lot_ops_tracking')
    status = cost_method_status(conn)
    status['seeded_opening_lots'] = len(seeded)
    status['changed_by'] = changed_by or ''
    return status


def _parse_move_year(move_date: str | None) -> int | None:
    if not move_date:
        return None
    s = str(move_date).strip()
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(s[:19], fmt).year
        except ValueError:
            continue
    return None


def has_stock_activity_in_year(conn: sqlite3.Connection, fiscal_year: int) -> bool:
    """Có phát sinh nhập/xuất/bán trong năm tài chính."""
    fy = int(fiscal_year)
    prefix = f'{fy}-%'
    row = conn.execute(
        """
        SELECT 1 FROM stock_moves
        WHERE date LIKE ? OR substr(date, 1, 4) = ?
        LIMIT 1
        """,
        (prefix, str(fy)),
    ).fetchone()
    if row:
        return True
    row = conn.execute(
        """
        SELECT 1 FROM inventory_lots
        WHERE substr(received_at, 1, 4) = ?
        LIMIT 1
        """,
        (str(fy),),
    ).fetchone()
    return bool(row)


def cost_method_status(conn: sqlite3.Connection) -> dict[str, Any]:
    method = get_inventory_cost_method(conn)
    effective_year = get_cost_method_effective_year(conn)
    locked_at = _get_setting(conn, KEY_LOCKED_AT, '')
    today = date.today()
    current_year = today.year
    configured = effective_year is not None
    activity = (
        has_stock_activity_in_year(conn, effective_year)
        if effective_year is not None
        else False
    )
    try:
        can_change = can_change_cost_method(conn, target_year=current_year)
    except Exception:
        can_change = False
    old_year_locked = False
    if effective_year is not None:
        try:
            old_year_locked = is_year_locked(conn, effective_year)
        except Exception:
            old_year_locked = False
    return {
        'method': method,
        'method_label': 'FIFO (nhập trước xuất trước)' if method == METHOD_FIFO else 'Bình quân gia quyền (WAC)',
        'effective_year': effective_year,
        'locked_at': locked_at,
        'configured': configured,
        'has_activity_in_effective_year': activity,
        'can_change': can_change,
        'current_fiscal_year': current_year,
        'effective_year_locked': old_year_locked,
        'lot_ops_tracking': is_lot_ops_tracking(conn),
        'lot_tracking_enabled': is_lot_tracking_enabled(conn),
        'lot_ops_only': is_lot_ops_only(conn),
        'warning': (
            'Phương pháp tính giá xuất kho áp dụng cả năm tài chính. '
            'Không thay đổi khi đã có phát sinh kho. '
            'Chỉ được đổi sau khi khóa sổ năm cũ hoặc sang năm tài chính mới.'
        ),
        'lot_ops_hint': (
            'Theo dõi lô vận hành: ghi nhận lô nhập/xuất theo FEFO (hết hạn sớm trước) '
            'để phản ánh hàng vật lý và lập kế hoạch tiêu thụ — không đổi giá vốn WAC.'
        ),
    }


def can_change_cost_method(
    conn: sqlite3.Connection,
    *,
    target_year: int | None = None,
    new_method: str | None = None,
) -> bool:
    effective_year = get_cost_method_effective_year(conn)
    current = get_inventory_cost_method(conn)
    ty = int(target_year or date.today().year)

    if effective_year is None:
        return True

    if new_method and new_method == current and ty == effective_year:
        return True

    if ty <= int(effective_year):
        return False

    if not is_year_locked(conn, int(effective_year)):
        if date.today() <= year_end_date(int(effective_year)):
            return False

    if has_stock_activity_in_year(conn, ty):
        return False

    return True


def assert_cost_method_change_allowed(
    conn: sqlite3.Connection,
    new_method: str,
    target_year: int,
) -> None:
    new_method = (new_method or '').strip().lower()
    if new_method not in VALID_METHODS:
        raise ValueError('Phương pháp không hợp lệ. Chọn WAC hoặc FIFO.')

    current = get_inventory_cost_method(conn)
    effective_year = get_cost_method_effective_year(conn)
    ty = int(target_year)

    if effective_year is not None and new_method == current and ty == effective_year:
        return

    if not can_change_cost_method(conn, target_year=ty, new_method=new_method):
        if effective_year is not None and ty <= effective_year:
            raise ValueError(
                f'Không thể đổi phương pháp trong năm tài chính {effective_year}. '
                f'Chỉ được đổi sau khi khóa sổ năm {effective_year} và trước phát sinh năm mới.'
            )
        if effective_year is not None and has_stock_activity_in_year(conn, ty):
            raise ValueError(
                f'Năm {ty} đã có phát sinh kho — không thể đổi phương pháp giá vốn.'
            )
        raise ValueError(
            'Chưa đủ điều kiện đổi phương pháp. '
            'Cần khóa sổ năm tài chính trước hoặc sang năm mới chưa phát sinh.'
        )


def _append_history(conn: sqlite3.Connection, entry: dict[str, Any]) -> None:
    raw = _get_setting(conn, KEY_HISTORY, '[]')
    try:
        hist = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        hist = []
    if not isinstance(hist, list):
        hist = []
    hist.append(entry)
    _set_setting(conn, KEY_HISTORY, json.dumps(hist, ensure_ascii=False))


def seed_opening_balance_lots(
    conn: sqlite3.Connection,
    fiscal_year: int,
    *,
    as_of_date: str | None = None,
    update_avg_cost: bool | None = None,
) -> list[int]:
    """Tồn hiện tại → lô tồn đầu năm (FIFO hoặc bật theo dõi lô WAC)."""
    from Services.fifo_lots import create_lot, sync_avg_cost_from_lots

    ensure_inventory_lot_schema(conn)
    if update_avg_cost is None:
        update_avg_cost = is_fifo_mode(conn)
    received = as_of_date or f'{int(fiscal_year):04d}-01-01'
    lot_ids: list[int] = []
    rows = conn.execute(
        """
        SELECT i.product_id, COALESCE(i.quantity, 0) AS qty, COALESCE(i.avg_cost, 0) AS avg_cost
        FROM inventory i
        WHERE COALESCE(i.quantity, 0) > 0.0001
        """
    ).fetchall()
    for row in rows:
        pid = int(row[0] if not hasattr(row, 'keys') else row['product_id'])
        qty = float(row[1] if not hasattr(row, 'keys') else row['qty'])
        cost = float(row[2] if not hasattr(row, 'keys') else row['avg_cost'])
        if qty <= 0:
            continue
        existing = conn.execute(
            """
            SELECT id FROM inventory_lots
            WHERE product_id = ? AND source_type = 'OPENING_BALANCE'
              AND substr(received_at, 1, 4) = ?
            LIMIT 1
            """,
            (pid, str(int(fiscal_year))),
        ).fetchone()
        if existing:
            continue
        # Khi chỉ theo dõi vận hành: bỏ qua SP đã có lô còn tồn (tránh cộng đôi)
        if not update_avg_cost:
            any_open = conn.execute(
                """
                SELECT id FROM inventory_lots
                WHERE product_id = ? AND qty_remaining > 0.0001
                LIMIT 1
                """,
                (pid,),
            ).fetchone()
            if any_open:
                continue
        lot_id = create_lot(
            conn.cursor(),
            product_id=pid,
            qty=qty,
            unit_cost=cost,
            source_type='OPENING_BALANCE',
            source_id=int(fiscal_year),
            received_at=received,
            lot_no=f'TD-{fiscal_year}-{pid}',
            note=f'Lô tồn đầu kỳ {fiscal_year} — theo dõi lô / chuyển phương pháp',
            update_avg_cost=bool(update_avg_cost),
        )
        lot_ids.append(lot_id)
        if update_avg_cost:
            sync_avg_cost_from_lots(conn.cursor(), pid)
    return lot_ids


def merge_lots_to_wac(conn: sqlite3.Connection) -> None:
    """FIFO → WAC: gộp giá trị lô còn lại vào avg_cost, đóng lô."""
    from Services.inventory_stock_helpers import _set_avg_cost, ledger_quantity

    rows = conn.execute(
        """
        SELECT product_id,
               COALESCE(SUM(qty_remaining), 0) AS qty,
               COALESCE(SUM(qty_remaining * unit_cost), 0) AS val
        FROM inventory_lots
        WHERE qty_remaining > 0
        GROUP BY product_id
        """
    ).fetchall()
    cur = conn.cursor()
    for row in rows:
        pid = int(row[0] if not hasattr(row, 'keys') else row['product_id'])
        val = float(row[2] if not hasattr(row, 'keys') else row['val'])
        qty = ledger_quantity(cur, pid)
        avg = (val / qty) if qty > 0 else 0.0
        _set_avg_cost(cur, pid, avg)
    conn.execute(
        """
        UPDATE inventory_lots
        SET qty_remaining = 0, status = 'closed'
        WHERE qty_remaining > 0
        """
    )


def apply_cost_method_change(
    conn: sqlite3.Connection,
    new_method: str,
    target_year: int,
    *,
    changed_by: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError('Cần xác nhận đã hiểu quy định không đổi trong năm tài chính.')

    assert_cost_method_change_allowed(conn, new_method, target_year)
    old_method = get_inventory_cost_method(conn)
    old_year = get_cost_method_effective_year(conn)

    if old_method == new_method and old_year == target_year:
        return cost_method_status(conn)

    if old_method == METHOD_WAC and new_method == METHOD_FIFO:
        seed_opening_balance_lots(conn, target_year)
    elif old_method == METHOD_FIFO and new_method == METHOD_WAC:
        merge_lots_to_wac(conn)

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _set_setting(conn, KEY_METHOD, new_method)
    _set_setting(conn, KEY_EFFECTIVE_YEAR, str(int(target_year)))
    _set_setting(conn, KEY_LOCKED_AT, now)
    _append_history(
        conn,
        {
            'at': now,
            'from': old_method,
            'to': new_method,
            'from_year': old_year,
            'to_year': int(target_year),
            'by': changed_by or '',
        },
    )
    sqlite_commit(conn, label='inventory_cost_method_change')
    return cost_method_status(conn)


def ensure_inventory_lot_schema(conn: sqlite3.Connection) -> None:
    from Services.inventory_lot_schema import ensure_inventory_lot_schema as _ensure

    _ensure(conn, commit=False)
