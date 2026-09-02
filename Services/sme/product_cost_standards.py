"""Định mức giá thành theo từng phương án (NVL + NCTT + CPSXC).

Mỗi phương án có bộ định mức riêng — không dùng chung.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from db_utils import sqlite_commit

METHOD_NORMAL = 'normal_capacity'
METHOD_ACTUAL = 'actual'
METHOD_STANDARD = 'standard_provisional'
METHODS = (METHOD_NORMAL, METHOD_ACTUAL, METHOD_STANDARD)

METHOD_LABELS = {
    METHOD_NORMAL: 'PA1 — Công suất bình thường (TT99)',
    METHOD_ACTUAL: 'PA2 — Phân bổ toàn bộ chi phí thực tế',
    METHOD_STANDARD: 'PA3 — Giá định mức (tạm) + điều chỉnh cuối kỳ',
}


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _f(v) -> float:
    return float(v or 0)


def _money(v) -> float:
    return round(float(v or 0), 2)


def ensure_product_cost_standards_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    # Tránh executescript (tự COMMIT + khóa ghi) khi bảng đã có — gây "database is locked"
    # nếu UI gọi song song 3 tab định mức.
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sme_product_cost_standards'"
        ).fetchone()
        if row:
            return
    except sqlite3.Error:
        pass

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sme_product_cost_standards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            allocation_method TEXT NOT NULL,
            finished_product_id INTEGER NOT NULL,
            labor_std_per_unit REAL NOT NULL DEFAULT 0,
            oh_fixed_std_per_unit REAL NOT NULL DEFAULT 0,
            oh_variable_std_per_unit REAL NOT NULL DEFAULT 0,
            equivalent_factor REAL NOT NULL DEFAULT 1,
            note TEXT,
            updated_at TEXT,
            UNIQUE(allocation_method, finished_product_id)
        );

        CREATE TABLE IF NOT EXISTS sme_product_cost_standard_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            standard_id INTEGER NOT NULL,
            material_product_id INTEGER NOT NULL,
            qty_per_unit REAL NOT NULL DEFAULT 0,
            note TEXT,
            UNIQUE(standard_id, material_product_id),
            FOREIGN KEY (standard_id) REFERENCES sme_product_cost_standards(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sme_pcs_method
            ON sme_product_cost_standards(allocation_method);
        CREATE INDEX IF NOT EXISTS idx_sme_pcs_mats
            ON sme_product_cost_standard_materials(standard_id);
        """
    )
    if commit:
        sqlite_commit(conn, label='product_cost_standards')


def list_standards(conn: sqlite3.Connection, allocation_method: str) -> list[dict]:
    ensure_product_cost_standards_schema(conn)
    method = (allocation_method or '').strip()
    if method not in METHODS:
        raise ValueError('Phương án định mức không hợp lệ')
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.*, p.name AS finished_name, p.product_code, p.unit AS finished_unit,
               (SELECT COUNT(*) FROM sme_product_cost_standard_materials m
                WHERE m.standard_id = s.id) AS material_count
        FROM sme_product_cost_standards s
        JOIN products p ON p.id = s.finished_product_id
        WHERE s.allocation_method = ?
        ORDER BY p.name COLLATE NOCASE
        """,
        (method,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_standard(
    conn: sqlite3.Connection,
    allocation_method: str,
    finished_product_id: int,
) -> dict | None:
    ensure_product_cost_standards_schema(conn)
    method = (allocation_method or '').strip()
    if method not in METHODS:
        raise ValueError('Phương án định mức không hợp lệ')
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT s.*, p.name AS finished_name, p.product_code, p.unit AS finished_unit
        FROM sme_product_cost_standards s
        JOIN products p ON p.id = s.finished_product_id
        WHERE s.allocation_method = ? AND s.finished_product_id = ?
        """,
        (method, int(finished_product_id)),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    mats = conn.execute(
        """
        SELECT m.*, p.name AS material_name, p.product_code AS material_code,
               p.unit AS material_unit,
               COALESCE(p.product_type, 'goods') AS material_type
        FROM sme_product_cost_standard_materials m
        JOIN products p ON p.id = m.material_product_id
        WHERE m.standard_id = ?
        ORDER BY m.id
        """,
        (data['id'],),
    ).fetchall()
    data['materials'] = [dict(x) for x in mats]
    return data


def save_standard(
    conn: sqlite3.Connection,
    *,
    allocation_method: str,
    finished_product_id: int,
    labor_std_per_unit: float = 0,
    oh_fixed_std_per_unit: float = 0,
    oh_variable_std_per_unit: float = 0,
    equivalent_factor: float = 1,
    note: str = '',
    materials: list[dict] | None = None,
    commit: bool = True,
) -> dict:
    """Lưu định mức NVL + NCTT + CPSXC cho 1 SP theo đúng 1 phương án."""
    from Services.sme.costing_policy import assert_method_unlocked

    ensure_product_cost_standards_schema(conn)
    method = (allocation_method or '').strip()
    if method not in METHODS:
        raise ValueError('Phương án định mức không hợp lệ')
    assert_method_unlocked(conn, method)

    fg_id = int(finished_product_id)
    row = conn.execute(
        "SELECT id, COALESCE(product_type,'') FROM products WHERE id = ?", (fg_id,)
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy thành phẩm')
    ptype = (row[1] if not isinstance(row, sqlite3.Row) else row[1]) or ''
    if str(ptype).lower() not in ('finished_goods', 'finished', 'tp'):
        # Cho phép nếu product_type trống nhưng vẫn là SP đang dùng SX
        pass

    labor = max(0.0, _f(labor_std_per_unit))
    oh_f = max(0.0, _f(oh_fixed_std_per_unit))
    oh_v = max(0.0, _f(oh_variable_std_per_unit))
    eq = _f(equivalent_factor) or 1.0
    if eq <= 0:
        raise ValueError('Hệ số quy đổi phải > 0')

    items: list[dict] = []
    seen = set()
    for raw in materials or []:
        mid = int(raw.get('material_product_id') or 0)
        qty = _f(raw.get('qty_per_unit'))
        if mid <= 0 or qty <= 0:
            continue
        if mid in seen:
            raise ValueError(f'Vật tư #{mid} bị trùng trong định mức')
        if mid == fg_id:
            raise ValueError('Không dùng chính thành phẩm làm NVL định mức')
        seen.add(mid)
        items.append({
            'material_product_id': mid,
            'qty_per_unit': qty,
            'note': (raw.get('note') or '').strip(),
        })
    if not items and labor <= 0 and oh_f <= 0 and oh_v <= 0:
        raise ValueError('Định mức cần ít nhất NVL hoặc NCTT/CPSXC')

    existing = conn.execute(
        """
        SELECT id FROM sme_product_cost_standards
        WHERE allocation_method = ? AND finished_product_id = ?
        """,
        (method, fg_id),
    ).fetchone()
    now = _now()
    if existing:
        sid = int(existing[0])
        conn.execute(
            """
            UPDATE sme_product_cost_standards SET
                labor_std_per_unit = ?, oh_fixed_std_per_unit = ?,
                oh_variable_std_per_unit = ?, equivalent_factor = ?,
                note = ?, updated_at = ?
            WHERE id = ?
            """,
            (labor, oh_f, oh_v, eq, (note or '').strip(), now, sid),
        )
        conn.execute(
            'DELETE FROM sme_product_cost_standard_materials WHERE standard_id = ?', (sid,)
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO sme_product_cost_standards (
                allocation_method, finished_product_id,
                labor_std_per_unit, oh_fixed_std_per_unit, oh_variable_std_per_unit,
                equivalent_factor, note, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (method, fg_id, labor, oh_f, oh_v, eq, (note or '').strip(), now),
        )
        sid = int(cur.lastrowid)

    for it in items:
        conn.execute(
            """
            INSERT INTO sme_product_cost_standard_materials (
                standard_id, material_product_id, qty_per_unit, note
            ) VALUES (?, ?, ?, ?)
            """,
            (sid, it['material_product_id'], it['qty_per_unit'], it['note']),
        )

    if commit:
        sqlite_commit(conn, label='product_cost_standards')
    return get_standard(conn, method, fg_id) or {}


def delete_standard(
    conn: sqlite3.Connection,
    allocation_method: str,
    finished_product_id: int,
    *,
    commit: bool = True,
) -> None:
    from Services.sme.costing_policy import assert_method_unlocked

    ensure_product_cost_standards_schema(conn)
    method = (allocation_method or '').strip()
    assert_method_unlocked(conn, method)
    conn.execute(
        """
        DELETE FROM sme_product_cost_standards
        WHERE allocation_method = ? AND finished_product_id = ?
        """,
        (method, int(finished_product_id)),
    )
    if commit:
        sqlite_commit(conn, label='product_cost_standards')


def preview_order_from_standard(
    conn: sqlite3.Connection,
    *,
    allocation_method: str,
    finished_product_id: int,
    qty: float,
) -> dict[str, Any]:
    """Tính NVL (theo WAC) + NCTT/CPSXC định mức cho SL lệnh."""
    from Services.inventory_stock_helpers import get_wac, ledger_quantity
    from Services.production_costing import _inventory_account_for_type

    std = get_standard(conn, allocation_method, finished_product_id)
    if not std:
        raise ValueError(
            f'Chưa có định mức cho phương án {METHOD_LABELS.get(allocation_method, allocation_method)}. '
            f'Vào trang định mức tương ứng để thiết lập.'
        )
    q = _f(qty)
    if q <= 0:
        raise ValueError('Số lượng phải > 0')

    c = conn.cursor()
    lines = []
    mat_total = 0.0
    for m in std.get('materials') or []:
        mid = int(m['material_product_id'])
        qty_std = round(_f(m['qty_per_unit']) * q, 6)
        avg = float(get_wac(c, mid) or 0)
        stock = float(ledger_quantity(c, mid) or 0)
        line_cost = _money(qty_std * avg)
        mat_total = _money(mat_total + line_cost)
        lines.append({
            'material_product_id': mid,
            'material_name': m.get('material_name'),
            'material_code': m.get('material_code'),
            'material_unit': m.get('material_unit'),
            'material_type': m.get('material_type'),
            'inventory_account': _inventory_account_for_type(m.get('material_type')),
            'qty_per_unit': _f(m['qty_per_unit']),
            'qty_standard': qty_std,
            'qty_actual': qty_std,
            'stock': stock,
            'avg_cost': avg,
            'line_cost': line_cost,
            'enough_stock': stock + 1e-9 >= qty_std,
        })

    labor = _money(_f(std.get('labor_std_per_unit')) * q)
    oh_f = _money(_f(std.get('oh_fixed_std_per_unit')) * q)
    oh_v = _money(_f(std.get('oh_variable_std_per_unit')) * q)
    other = _money(oh_f + oh_v)
    total = _money(mat_total + labor + other)
    unit = round(total / q, 4) if q else 0.0
    eq = _f(std.get('equivalent_factor')) or 1.0

    return {
        'allocation_method': allocation_method,
        'finished_product_id': int(finished_product_id),
        'finished_name': std.get('finished_name'),
        'product_code': std.get('product_code'),
        'qty': q,
        'equivalent_factor': eq,
        'equivalent_qty': round(q * eq, 6),
        'materials': lines,
        'total_material_cost': mat_total,
        'labor_std': labor,
        'oh_fixed_std': oh_f,
        'oh_variable_std': oh_v,
        'other_std': other,
        'provisional_total': total,
        'provisional_unit_cost': unit,
        'labor_std_per_unit': _f(std.get('labor_std_per_unit')),
        'oh_fixed_std_per_unit': _f(std.get('oh_fixed_std_per_unit')),
        'oh_variable_std_per_unit': _f(std.get('oh_variable_std_per_unit')),
    }
