"""Phân bổ 622/627 cuối kỳ (hoặc theo giai đoạn) vào lệnh SX — TT99.

Phương án 1 (mặc định, ``normal_capacity``):
  Suất ĐM = chi phí cố định ÷ công suất bình thường (quy đổi theo số ngày).
  Vào giá thành = suất × sản lượng quy đổi thực tế.
  Phần dưới công suất treo 622/627; cuối kỳ (hoặc khi chốt idle) → Nợ 632.

Phương án 2 (``actual``):
  Chia hết chi phí thực tế theo tỷ lệ sản lượng quy đổi — không tạo idle.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from db_utils import sqlite_commit

METHOD_NORMAL = 'normal_capacity'
METHOD_ACTUAL = 'actual'
METHODS = (METHOD_NORMAL, METHOD_ACTUAL)

STATUS_DRAFT = 'draft'
STATUS_POSTED = 'posted'
STATUS_REVERSED = 'reversed'
STATUS_IDLE_CLOSED = 'idle_closed'


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _money(v) -> float:
    return round(float(v or 0), 2)


def _f(v) -> float:
    return float(v or 0)


def ensure_period_cost_allocation_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    need_ddl = True
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sme_period_cost_allocations'"
        ).fetchone()
        need_ddl = row is None
    except sqlite3.Error:
        pass

    if need_ddl:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sme_costing_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                allocation_method TEXT NOT NULL DEFAULT 'normal_capacity',
                normal_capacity_month REAL NOT NULL DEFAULT 500,
                working_days_month REAL NOT NULL DEFAULT 25,
                require_finalize_before_fg INTEGER NOT NULL DEFAULT 1,
                department_name TEXT DEFAULT 'Bộ phận sản xuất',
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sme_period_cost_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fiscal_year INTEGER NOT NULL,
                period INTEGER NOT NULL,
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                days_count REAL NOT NULL DEFAULT 0,
                allocation_method TEXT NOT NULL,
                normal_capacity_month REAL NOT NULL,
                working_days_month REAL NOT NULL,
                capacity_in_scope REAL NOT NULL DEFAULT 0,
                labor_amount REAL NOT NULL DEFAULT 0,
                oh_fixed_amount REAL NOT NULL DEFAULT 0,
                oh_variable_amount REAL NOT NULL DEFAULT 0,
                labor_rate REAL NOT NULL DEFAULT 0,
                oh_fixed_rate REAL NOT NULL DEFAULT 0,
                labor_allocated REAL NOT NULL DEFAULT 0,
                oh_fixed_allocated REAL NOT NULL DEFAULT 0,
                oh_variable_allocated REAL NOT NULL DEFAULT 0,
                labor_idle REAL NOT NULL DEFAULT 0,
                oh_fixed_idle REAL NOT NULL DEFAULT 0,
                equivalent_qty_total REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'draft',
                collect_journal_entry_id INTEGER,
                allocate_journal_entry_id INTEGER,
                idle_journal_entry_id INTEGER,
                note TEXT,
                created_by TEXT,
                created_at TEXT,
                posted_at TEXT,
                idle_closed_at TEXT,
                UNIQUE(fiscal_year, period, date_from, date_to, status)
            );

            CREATE TABLE IF NOT EXISTS sme_period_cost_allocation_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                allocation_id INTEGER NOT NULL,
                production_order_id INTEGER NOT NULL,
                finished_product_id INTEGER NOT NULL,
                voucher_no TEXT,
                qty REAL NOT NULL DEFAULT 0,
                equivalent_factor REAL NOT NULL DEFAULT 1,
                equivalent_qty REAL NOT NULL DEFAULT 0,
                material_cost REAL NOT NULL DEFAULT 0,
                labor_allocated REAL NOT NULL DEFAULT 0,
                oh_fixed_allocated REAL NOT NULL DEFAULT 0,
                oh_variable_allocated REAL NOT NULL DEFAULT 0,
                total_cost REAL NOT NULL DEFAULT 0,
                unit_cost REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (allocation_id) REFERENCES sme_period_cost_allocations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sme_pca_period
                ON sme_period_cost_allocations(fiscal_year, period);
            CREATE INDEX IF NOT EXISTS idx_sme_pca_lines_alloc
                ON sme_period_cost_allocation_lines(allocation_id);
            """
        )

    altered = need_ddl
    # Cột mở rộng production_orders + products
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(production_orders)').fetchall()}
        for col, decl in (
            ('cost_finalized', 'INTEGER DEFAULT 0'),
            ('allocation_id', 'INTEGER'),
            ('oh_fixed_cost', 'REAL DEFAULT 0'),
            ('oh_variable_cost', 'REAL DEFAULT 0'),
        ):
            if col not in cols:
                conn.execute(f'ALTER TABLE production_orders ADD COLUMN {col} {decl}')
                altered = True
    except sqlite3.Error:
        pass
    try:
        pcols = {r[1] for r in conn.execute('PRAGMA table_info(products)').fetchall()}
        if 'costing_equivalent_factor' not in pcols:
            conn.execute(
                'ALTER TABLE products ADD COLUMN costing_equivalent_factor REAL DEFAULT 1'
            )
            altered = True
    except sqlite3.Error:
        pass

    try:
        row = conn.execute('SELECT id FROM sme_costing_settings WHERE id = 1').fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO sme_costing_settings (
                    id, allocation_method, normal_capacity_month, working_days_month,
                    require_finalize_before_fg, department_name, updated_at
                ) VALUES (1, 'normal_capacity', 500, 25, 0, 'Bộ phận sản xuất', ?)
                """,
                (_now(),),
            )
            altered = True
    except sqlite3.Error:
        pass

    if commit or altered:
        sqlite_commit(conn, label='period_cost_allocation')


def get_costing_settings(conn: sqlite3.Connection) -> dict:
    ensure_period_cost_allocation_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM sme_costing_settings WHERE id = 1').fetchone()
    return dict(row) if row else {
        'allocation_method': METHOD_NORMAL,
        'normal_capacity_month': 500,
        'working_days_month': 25,
        'require_finalize_before_fg': 1,
        'department_name': 'Bộ phận sản xuất',
    }


def save_costing_settings(
    conn: sqlite3.Connection,
    *,
    allocation_method: str = METHOD_NORMAL,
    normal_capacity_month: float = 500,
    working_days_month: float = 25,
    require_finalize_before_fg: bool = True,
    department_name: str = 'Bộ phận sản xuất',
    commit: bool = True,
) -> dict:
    ensure_period_cost_allocation_schema(conn)
    method = (allocation_method or METHOD_NORMAL).strip()
    if method not in METHODS:
        raise ValueError('Phương án phân bổ không hợp lệ')
    cap = _f(normal_capacity_month)
    days = _f(working_days_month)
    if cap <= 0:
        raise ValueError('Công suất bình thường tháng phải lớn hơn 0')
    if days <= 0:
        raise ValueError('Số ngày làm việc tháng phải lớn hơn 0')
    conn.execute(
        """
        INSERT INTO sme_costing_settings (
            id, allocation_method, normal_capacity_month, working_days_month,
            require_finalize_before_fg, department_name, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            allocation_method = excluded.allocation_method,
            normal_capacity_month = excluded.normal_capacity_month,
            working_days_month = excluded.working_days_month,
            require_finalize_before_fg = excluded.require_finalize_before_fg,
            department_name = excluded.department_name,
            updated_at = excluded.updated_at
        """,
        (
            method, cap, days,
            1 if require_finalize_before_fg else 0,
            (department_name or 'Bộ phận sản xuất').strip(),
            _now(),
        ),
    )
    if commit:
        sqlite_commit(conn, label='period_cost_allocation')
    return get_costing_settings(conn)


def get_product_equivalent_factor(conn: sqlite3.Connection, product_id: int) -> float:
    try:
        row = conn.execute(
            'SELECT COALESCE(costing_equivalent_factor, 1) FROM products WHERE id = ?',
            (product_id,),
        ).fetchone()
        f = _f(row[0] if row else 1)
        return f if f > 0 else 1.0
    except sqlite3.Error:
        return 1.0


def set_product_equivalent_factor(
    conn: sqlite3.Connection,
    product_id: int,
    factor: float,
    *,
    commit: bool = True,
) -> None:
    ensure_period_cost_allocation_schema(conn)
    f = _f(factor)
    if f <= 0:
        raise ValueError('Hệ số quy đổi phải lớn hơn 0')
    conn.execute(
        'UPDATE products SET costing_equivalent_factor = ? WHERE id = ?',
        (f, product_id),
    )
    if commit:
        sqlite_commit(conn, label='period_cost_allocation')


def list_finished_product_factors(conn: sqlite3.Connection) -> list[dict]:
    ensure_period_cost_allocation_schema(conn)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, name, product_code, unit,
                   COALESCE(costing_equivalent_factor, 1) AS costing_equivalent_factor
            FROM products
            WHERE LOWER(COALESCE(product_type, '')) = 'finished_goods'
            ORDER BY name COLLATE NOCASE
            LIMIT 500
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    return [dict(r) for r in rows]


def _days_inclusive(date_from: str, date_to: str) -> float:
    from datetime import date as date_cls
    d0 = date_cls.fromisoformat(date_from[:10])
    d1 = date_cls.fromisoformat(date_to[:10])
    return float((d1 - d0).days + 1)


def list_orders_for_allocation(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
) -> list[dict]:
    """Lệnh SX trong khoảng ngày, chưa hủy, ưu tiên chưa gắn phân bổ khác đang hiệu lực."""
    ensure_period_cost_allocation_schema(conn)
    from Services.production_costing import ensure_production_schema
    ensure_production_schema(conn)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute('PRAGMA table_info(production_orders)').fetchall()}
    finalized = 'COALESCE(cost_finalized, 0)' if 'cost_finalized' in cols else '0'
    rows = conn.execute(
        f"""
        SELECT o.id, o.voucher_no, o.production_date, o.finished_product_id,
               COALESCE(o.qty_planned, o.qty_completed, 0) AS qty,
               COALESCE(o.total_material_cost, 0) AS material_cost,
               COALESCE(o.labor_cost, 0) AS labor_cost,
               COALESCE(o.other_cost, 0) AS other_cost,
               COALESCE(o.total_cost, o.total_material_cost, 0) AS total_cost,
               COALESCE(o.unit_cost, 0) AS unit_cost,
               COALESCE(o.status, '') AS status,
               {finalized} AS cost_finalized,
               p.name AS finished_name, p.product_code,
               COALESCE(p.costing_equivalent_factor, 1) AS equivalent_factor
        FROM production_orders o
        JOIN products p ON p.id = o.finished_product_id
        WHERE COALESCE(o.status, '') != 'cancelled'
          AND o.production_date >= ? AND o.production_date <= ?
        ORDER BY o.production_date, o.id
        """,
        (date_from[:10], date_to[:10]),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        factor = _f(d.get('equivalent_factor') or 1) or 1.0
        qty = _f(d.get('qty'))
        d['equivalent_factor'] = factor
        d['equivalent_qty'] = round(qty * factor, 6)
        out.append(d)
    return out


def preview_allocation(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    date_from: str | None = None,
    date_to: str | None = None,
    labor_amount: float = 0,
    oh_fixed_amount: float = 0,
    oh_variable_amount: float = 0,
    allocation_method: str | None = None,
    normal_capacity_month: float | None = None,
    working_days_month: float | None = None,
    order_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Tính bảng phân bổ (không ghi DB)."""
    settings = get_costing_settings(conn)
    method = (allocation_method or settings.get('allocation_method') or METHOD_NORMAL).strip()
    if method not in METHODS:
        raise ValueError('Phương án phân bổ không hợp lệ')

    cap_month = _f(normal_capacity_month if normal_capacity_month is not None
                   else settings.get('normal_capacity_month'))
    days_month = _f(working_days_month if working_days_month is not None
                    else settings.get('working_days_month'))
    if cap_month <= 0 or days_month <= 0:
        raise ValueError('Công suất bình thường / số ngày làm việc phải lớn hơn 0')

    # Mặc định cả tháng
    if not date_from:
        date_from = f'{int(fiscal_year):04d}-{int(period):02d}-01'
    if not date_to:
        import calendar
        last = calendar.monthrange(int(fiscal_year), int(period))[1]
        date_to = f'{int(fiscal_year):04d}-{int(period):02d}-{last:02d}'
    date_from = date_from[:10]
    date_to = date_to[:10]
    if date_to < date_from:
        raise ValueError('Đến ngày phải sau hoặc bằng từ ngày')

    days_count = _days_inclusive(date_from, date_to)
    # Công suất trong phạm vi: theo tỷ lệ ngày / ngày làm việc tháng (không vượt cap tháng)
    capacity_in_scope = round(cap_month * (days_count / days_month), 6)
    if capacity_in_scope <= 0:
        raise ValueError('Công suất trong kỳ phân bổ phải lớn hơn 0')

    labor = _money(labor_amount)
    oh_f = _money(oh_fixed_amount)
    oh_v = _money(oh_variable_amount)
    if labor < 0 or oh_f < 0 or oh_v < 0:
        raise ValueError('Chi phí không được âm')
    if labor + oh_f + oh_v <= 0:
        raise ValueError('Nhập ít nhất một khoản chi phí 622 hoặc 627')

    orders = list_orders_for_allocation(conn, date_from=date_from, date_to=date_to)
    if order_ids:
        idset = {int(x) for x in order_ids}
        orders = [o for o in orders if int(o['id']) in idset]
    if not orders:
        raise ValueError('Không có lệnh sản xuất trong khoảng ngày để phân bổ')

    eq_total = round(sum(_f(o['equivalent_qty']) for o in orders), 6)
    if eq_total <= 0:
        raise ValueError('Tổng sản lượng quy đổi phải lớn hơn 0')

    labor_rate = round(labor / capacity_in_scope, 6) if method == METHOD_NORMAL else 0.0
    oh_fixed_rate = round(oh_f / capacity_in_scope, 6) if method == METHOD_NORMAL else 0.0

    lines = []
    labor_alloc_sum = 0.0
    oh_f_alloc_sum = 0.0
    oh_v_alloc_sum = 0.0

    for i, o in enumerate(orders):
        eq = _f(o['equivalent_qty'])
        mat = _money(o.get('material_cost'))
        qty = _f(o.get('qty'))

        if method == METHOD_NORMAL:
            lab = _money(labor_rate * eq)
            ofx = _money(oh_fixed_rate * eq)
        else:
            lab = _money(labor * (eq / eq_total))
            ofx = _money(oh_f * (eq / eq_total))
        ov = _money(oh_v * (eq / eq_total))

        # Dòng cuối làm tròn cho phương án actual
        if method == METHOD_ACTUAL and i == len(orders) - 1:
            lab = _money(labor - labor_alloc_sum)
            ofx = _money(oh_f - oh_f_alloc_sum)
            ov = _money(oh_v - oh_v_alloc_sum)

        labor_alloc_sum = _money(labor_alloc_sum + lab)
        oh_f_alloc_sum = _money(oh_f_alloc_sum + ofx)
        oh_v_alloc_sum = _money(oh_v_alloc_sum + ov)

        total = _money(mat + lab + ofx + ov)
        unit = round(total / qty, 4) if qty else 0.0
        lines.append({
            'production_order_id': int(o['id']),
            'finished_product_id': int(o['finished_product_id']),
            'voucher_no': o.get('voucher_no'),
            'finished_name': o.get('finished_name'),
            'product_code': o.get('product_code'),
            'production_date': o.get('production_date'),
            'qty': qty,
            'equivalent_factor': _f(o.get('equivalent_factor') or 1),
            'equivalent_qty': eq,
            'material_cost': mat,
            'labor_allocated': lab,
            'oh_fixed_allocated': ofx,
            'oh_variable_allocated': ov,
            'total_cost': total,
            'unit_cost': unit,
            'cost_finalized': int(o.get('cost_finalized') or 0),
        })

    if method == METHOD_NORMAL:
        # Không phân bổ vượt chi phí thực tế khi sản xuất vượt công suất
        if labor_alloc_sum > labor + 0.01:
            # Scale down
            scale = labor / labor_alloc_sum if labor_alloc_sum else 1
            for ln in lines:
                ln['labor_allocated'] = _money(ln['labor_allocated'] * scale)
            labor_alloc_sum = labor
        if oh_f_alloc_sum > oh_f + 0.01:
            scale = oh_f / oh_f_alloc_sum if oh_f_alloc_sum else 1
            for ln in lines:
                ln['oh_fixed_allocated'] = _money(ln['oh_fixed_allocated'] * scale)
            oh_f_alloc_sum = oh_f
        for ln in lines:
            ln['total_cost'] = _money(
                ln['material_cost'] + ln['labor_allocated']
                + ln['oh_fixed_allocated'] + ln['oh_variable_allocated']
            )
            ln['unit_cost'] = round(ln['total_cost'] / ln['qty'], 4) if ln['qty'] else 0.0

        labor_idle = _money(max(0.0, labor - labor_alloc_sum))
        oh_fixed_idle = _money(max(0.0, oh_f - oh_f_alloc_sum))
    else:
        labor_idle = 0.0
        oh_fixed_idle = 0.0
        labor_alloc_sum = labor
        oh_f_alloc_sum = oh_f
        oh_v_alloc_sum = oh_v

    capacity_used_pct = round(eq_total * 100.0 / capacity_in_scope, 2) if capacity_in_scope else 0

    return {
        'fiscal_year': int(fiscal_year),
        'period': int(period),
        'date_from': date_from,
        'date_to': date_to,
        'days_count': days_count,
        'allocation_method': method,
        'allocation_method_label': (
            'Công suất bình thường (TT99)' if method == METHOD_NORMAL
            else 'Phân bổ toàn bộ chi phí thực tế'
        ),
        'normal_capacity_month': cap_month,
        'working_days_month': days_month,
        'capacity_in_scope': capacity_in_scope,
        'equivalent_qty_total': eq_total,
        'capacity_used_pct': capacity_used_pct,
        'labor_amount': labor,
        'oh_fixed_amount': oh_f,
        'oh_variable_amount': oh_v,
        'labor_rate': labor_rate if method == METHOD_NORMAL else (
            round(labor / eq_total, 6) if eq_total else 0
        ),
        'oh_fixed_rate': oh_fixed_rate if method == METHOD_NORMAL else (
            round(oh_f / eq_total, 6) if eq_total else 0
        ),
        'labor_allocated': labor_alloc_sum,
        'oh_fixed_allocated': oh_f_alloc_sum,
        'oh_variable_allocated': oh_v_alloc_sum,
        'labor_idle': labor_idle,
        'oh_fixed_idle': oh_fixed_idle,
        'idle_total': _money(labor_idle + oh_fixed_idle),
        'lines': lines,
        'require_finalize_before_fg': int(settings.get('require_finalize_before_fg') or 0),
    }


def post_allocation(
    conn: sqlite3.Connection,
    preview: dict | None = None,
    *,
    fiscal_year: int | None = None,
    period: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    labor_amount: float = 0,
    oh_fixed_amount: float = 0,
    oh_variable_amount: float = 0,
    allocation_method: str | None = None,
    normal_capacity_month: float | None = None,
    working_days_month: float | None = None,
    order_ids: list[int] | None = None,
    note: str = '',
    close_idle_now: bool = False,
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Ghi phân bổ: cập nhật lệnh + journal; tùy chọn chốt idle → 632 ngay."""
    from Services.sme.period_cost_allocation_journal import post_allocation_journals

    ensure_period_cost_allocation_schema(conn)
    data = preview or preview_allocation(
        conn,
        fiscal_year=fiscal_year or datetime.now().year,
        period=period or datetime.now().month,
        date_from=date_from,
        date_to=date_to,
        labor_amount=labor_amount,
        oh_fixed_amount=oh_fixed_amount,
        oh_variable_amount=oh_variable_amount,
        allocation_method=allocation_method,
        normal_capacity_month=normal_capacity_month,
        working_days_month=working_days_month,
        order_ids=order_ids,
    )

    # Chặn nếu đã có phân bổ posted trùng khoảng
    conflict = conn.execute(
        """
        SELECT id, status FROM sme_period_cost_allocations
        WHERE fiscal_year = ? AND period = ?
          AND date_from = ? AND date_to = ?
          AND status IN ('posted', 'idle_closed')
        """,
        (data['fiscal_year'], data['period'], data['date_from'], data['date_to']),
    ).fetchone()
    if conflict:
        raise ValueError(
            f"Đã có phân bổ #{conflict[0]} cho khoảng này (trạng thái {conflict[1]}). "
            f"Hãy đảo trước khi ghi lại."
        )

    # Chặn lệnh đã nhập kho TP khi bật chốt trước nhập
    settings = get_costing_settings(conn)
    if int(settings.get('require_finalize_before_fg') or 0):
        for ln in data['lines']:
            oid = int(ln['production_order_id'])
            row = conn.execute(
                """
                SELECT COALESCE(qty_received, 0), COALESCE(defer_fg_receipt, 0), status
                FROM production_orders WHERE id = ?
                """,
                (oid,),
            ).fetchone()
            if not row:
                continue
            qty_recv, defer, status = float(row[0] or 0), int(row[1] or 0), row[2]
            # Nếu đã nhập kho (HKD hoàn thành ngay hoặc SME đã nhận) — cảnh báo cứng
            if qty_recv > 1e-9 or status == 'completed' and not defer:
                # Cho phép cập nhật cost nếu chưa có allocation — nhưng cảnh báo
                # Với SME defer: qty_received>0 nghĩa đã nhập một phần với giá cũ
                if qty_recv > 1e-9:
                    raise ValueError(
                        f"Lệnh {ln.get('voucher_no')} đã nhập kho thành phẩm — "
                        f"không phân bổ đè giá. Hủy nhập kho hoặc chọn lệnh khác."
                    )

    c = conn.cursor()
    c.execute(
        """
        INSERT INTO sme_period_cost_allocations (
            fiscal_year, period, date_from, date_to, days_count, allocation_method,
            normal_capacity_month, working_days_month, capacity_in_scope,
            labor_amount, oh_fixed_amount, oh_variable_amount,
            labor_rate, oh_fixed_rate,
            labor_allocated, oh_fixed_allocated, oh_variable_allocated,
            labor_idle, oh_fixed_idle, equivalent_qty_total,
            status, note, created_by, created_at, posted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data['fiscal_year'], data['period'], data['date_from'], data['date_to'],
            data['days_count'], data['allocation_method'],
            data['normal_capacity_month'], data['working_days_month'], data['capacity_in_scope'],
            data['labor_amount'], data['oh_fixed_amount'], data['oh_variable_amount'],
            data['labor_rate'], data['oh_fixed_rate'],
            data['labor_allocated'], data['oh_fixed_allocated'], data['oh_variable_allocated'],
            data['labor_idle'], data['oh_fixed_idle'], data['equivalent_qty_total'],
            STATUS_POSTED, (note or '').strip(), (created_by or '').strip(),
            _now(), _now(),
        ),
    )
    alloc_id = int(c.lastrowid)

    for ln in data['lines']:
        c.execute(
            """
            INSERT INTO sme_period_cost_allocation_lines (
                allocation_id, production_order_id, finished_product_id, voucher_no,
                qty, equivalent_factor, equivalent_qty, material_cost,
                labor_allocated, oh_fixed_allocated, oh_variable_allocated,
                total_cost, unit_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alloc_id, ln['production_order_id'], ln['finished_product_id'], ln.get('voucher_no'),
                ln['qty'], ln['equivalent_factor'], ln['equivalent_qty'], ln['material_cost'],
                ln['labor_allocated'], ln['oh_fixed_allocated'], ln['oh_variable_allocated'],
                ln['total_cost'], ln['unit_cost'],
            ),
        )
        other = _money(ln['oh_fixed_allocated'] + ln['oh_variable_allocated'])
        c.execute(
            """
            UPDATE production_orders SET
                labor_cost = ?,
                other_cost = ?,
                oh_fixed_cost = ?,
                oh_variable_cost = ?,
                total_cost = ?,
                unit_cost = ?,
                cost_finalized = 1,
                allocation_id = ?
            WHERE id = ?
            """,
            (
                ln['labor_allocated'], other,
                ln['oh_fixed_allocated'], ln['oh_variable_allocated'],
                ln['total_cost'], ln['unit_cost'],
                alloc_id, ln['production_order_id'],
            ),
        )

    journals = post_allocation_journals(
        conn, alloc_id, data,
        close_idle_now=close_idle_now,
        created_by=created_by,
        commit=False,
    )

    if close_idle_now and (data['labor_idle'] > 0 or data['oh_fixed_idle'] > 0):
        c.execute(
            """
            UPDATE sme_period_cost_allocations
            SET status = ?, idle_closed_at = ?, idle_journal_entry_id = ?
            WHERE id = ?
            """,
            (STATUS_IDLE_CLOSED, _now(), journals.get('idle_journal_entry_id'), alloc_id),
        )
    else:
        c.execute(
            """
            UPDATE sme_period_cost_allocations SET
                collect_journal_entry_id = ?,
                allocate_journal_entry_id = ?,
                idle_journal_entry_id = ?
            WHERE id = ?
            """,
            (
                journals.get('collect_journal_entry_id'),
                journals.get('allocate_journal_entry_id'),
                journals.get('idle_journal_entry_id'),
                alloc_id,
            ),
        )

    if commit:
        sqlite_commit(conn, label='period_cost_allocation')
    return get_allocation(conn, alloc_id)


def close_allocation_idle(
    conn: sqlite3.Connection,
    allocation_id: int,
    *,
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Cuối kỳ: kết chuyển phần dưới công suất còn treo → Nợ 632 / Có 622·627."""
    from Services.sme.period_cost_allocation_journal import post_idle_to_cogs

    ensure_period_cost_allocation_schema(conn)
    alloc = get_allocation(conn, allocation_id)
    if not alloc:
        raise ValueError('Không tìm thấy đợt phân bổ')
    if alloc['status'] == STATUS_IDLE_CLOSED:
        return alloc
    if alloc['status'] != STATUS_POSTED:
        raise ValueError('Chỉ chốt dưới công suất khi phân bổ đang ở trạng thái đã ghi sổ')
    idle_l = _money(alloc.get('labor_idle'))
    idle_f = _money(alloc.get('oh_fixed_idle'))
    if idle_l <= 0 and idle_f <= 0:
        conn.execute(
            "UPDATE sme_period_cost_allocations SET status = ?, idle_closed_at = ? WHERE id = ?",
            (STATUS_IDLE_CLOSED, _now(), allocation_id),
        )
        if commit:
            sqlite_commit(conn, label='period_cost_allocation')
        return get_allocation(conn, allocation_id)

    j = post_idle_to_cogs(
        conn, allocation_id, alloc,
        created_by=created_by, commit=False,
    )
    conn.execute(
        """
        UPDATE sme_period_cost_allocations
        SET status = ?, idle_closed_at = ?, idle_journal_entry_id = ?
        WHERE id = ?
        """,
        (STATUS_IDLE_CLOSED, _now(), j.get('journal_entry_id'), allocation_id),
    )
    if commit:
        sqlite_commit(conn, label='period_cost_allocation')
    return get_allocation(conn, allocation_id)


def reverse_allocation(
    conn: sqlite3.Connection,
    allocation_id: int,
    *,
    created_by: str = '',
    reason: str = '',
    commit: bool = True,
) -> dict:
    from Services.sme.journal_engine import reverse_journal_entry

    alloc = get_allocation(conn, allocation_id)
    if not alloc:
        raise ValueError('Không tìm thấy đợt phân bổ')
    if alloc['status'] == STATUS_REVERSED:
        return alloc

    for key in ('idle_journal_entry_id', 'allocate_journal_entry_id', 'collect_journal_entry_id'):
        jid = alloc.get(key)
        if jid:
            try:
                reverse_journal_entry(
                    conn, int(jid), created_by=created_by,
                    reason=reason or 'Đảo phân bổ giá thành cuối kỳ',
                )
            except ValueError:
                pass

    # Reset lệnh về giá tạm (NVL + NCTT/CPSXC định mức) nếu có
    for ln in alloc.get('lines') or []:
        oid = int(ln['production_order_id'])
        mat = _money(ln.get('material_cost'))
        qty = _f(ln.get('qty'))
        row = conn.execute(
            """
            SELECT COALESCE(provisional_labor, 0), COALESCE(provisional_oh_fixed, 0),
                   COALESCE(provisional_oh_variable, 0),
                   COALESCE(provisional_total_cost, 0),
                   COALESCE(provisional_unit_cost, 0),
                   COALESCE(total_material_cost, 0)
            FROM production_orders WHERE id = ?
            """,
            (oid,),
        ).fetchone()
        if row:
            pl, pof, pov, ptotal, punit, mat_db = (
                _f(row[0]), _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]), _f(row[5]),
            )
            mat_use = mat_db if mat_db > 0 else mat
            other = _money(pof + pov)
            if ptotal > 0:
                total = _money(ptotal)
                unit = punit if punit > 0 else (round(total / qty, 4) if qty else 0.0)
            else:
                total = _money(mat_use + pl + other)
                unit = round(total / qty, 4) if qty else 0.0
            conn.execute(
                """
                UPDATE production_orders SET
                    labor_cost = ?, other_cost = ?,
                    oh_fixed_cost = ?, oh_variable_cost = ?,
                    total_cost = ?, unit_cost = ?,
                    cost_finalized = 0, allocation_id = NULL,
                    cost_basis = 'provisional'
                WHERE id = ? AND allocation_id = ?
                """,
                (pl, other, pof, pov, total, unit, oid, allocation_id),
            )
        else:
            unit = round(mat / qty, 4) if qty else 0.0
            conn.execute(
                """
                UPDATE production_orders SET
                    labor_cost = 0, other_cost = 0,
                    oh_fixed_cost = 0, oh_variable_cost = 0,
                    total_cost = ?, unit_cost = ?,
                    cost_finalized = 0, allocation_id = NULL
                WHERE id = ? AND allocation_id = ?
                """,
                (mat, unit, oid, allocation_id),
            )

    conn.execute(
        """
        UPDATE sme_period_cost_allocations SET status = ?, note = TRIM(COALESCE(note,'') || ?)
        WHERE id = ?
        """,
        (
            STATUS_REVERSED,
            f" | Đảo: {(reason or '').strip()}" if reason else ' | Đã đảo',
            allocation_id,
        ),
    )
    if commit:
        sqlite_commit(conn, label='period_cost_allocation')
    return get_allocation(conn, allocation_id)


def get_allocation(conn: sqlite3.Connection, allocation_id: int) -> dict | None:
    ensure_period_cost_allocation_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        'SELECT * FROM sme_period_cost_allocations WHERE id = ?', (allocation_id,)
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    lines = conn.execute(
        """
        SELECT l.*, p.name AS finished_name, p.product_code
        FROM sme_period_cost_allocation_lines l
        LEFT JOIN products p ON p.id = l.finished_product_id
        WHERE l.allocation_id = ?
        ORDER BY l.id
        """,
        (allocation_id,),
    ).fetchall()
    data['lines'] = [dict(x) for x in lines]
    data['idle_total'] = _money(_f(data.get('labor_idle')) + _f(data.get('oh_fixed_idle')))
    return data


def list_allocations(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
    period: int | None = None,
) -> list[dict]:
    ensure_period_cost_allocation_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = 'SELECT * FROM sme_period_cost_allocations WHERE 1=1'
    params: list = []
    if fiscal_year:
        sql += ' AND fiscal_year = ?'
        params.append(int(fiscal_year))
    if period:
        sql += ' AND period = ?'
        params.append(int(period))
    sql += ' ORDER BY fiscal_year DESC, period DESC, id DESC LIMIT 100'
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def require_cost_finalized_for_fg(conn: sqlite3.Connection) -> bool:
    try:
        from Services.sme.costing_policy import get_costing_policy
        return bool(int(get_costing_policy(conn).get('require_finalize_before_fg') or 0))
    except Exception:
        settings = get_costing_settings(conn)
        return bool(int(settings.get('require_finalize_before_fg') or 0))
