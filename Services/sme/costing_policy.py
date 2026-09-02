"""Chính sách giá thành kỳ: chọn 1/3 phương án, khóa khi có lệnh SX, auto/manual close."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from db_utils import sqlite_commit
from Services.sme.product_cost_standards import (
    METHOD_ACTUAL,
    METHOD_LABELS,
    METHOD_NORMAL,
    METHOD_STANDARD,
    METHODS,
)

STATUS_OPEN = 'open'
STATUS_CLOSED = 'closed'


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _f(v) -> float:
    return float(v or 0)


def ensure_costing_policy_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    need_create_settings = True
    need_create_closes = True
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('sme_costing_settings','sme_costing_period_closes')"
            ).fetchall()
        }
        need_create_settings = 'sme_costing_settings' not in tables
        need_create_closes = 'sme_costing_period_closes' not in tables
    except sqlite3.Error:
        pass

    if need_create_settings or need_create_closes:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sme_costing_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                allocation_method TEXT NOT NULL DEFAULT 'normal_capacity',
                normal_capacity_month REAL NOT NULL DEFAULT 500,
                working_days_month REAL NOT NULL DEFAULT 25,
                require_finalize_before_fg INTEGER NOT NULL DEFAULT 0,
                department_name TEXT DEFAULT 'Bộ phận sản xuất',
                auto_close INTEGER NOT NULL DEFAULT 0,
                policy_locked INTEGER NOT NULL DEFAULT 0,
                policy_locked_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sme_costing_period_closes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fiscal_year INTEGER NOT NULL,
                period INTEGER NOT NULL,
                allocation_method TEXT NOT NULL,
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                wip_opening REAL NOT NULL DEFAULT 0,
                period_production_cost REAL NOT NULL DEFAULT 0,
                wip_closing REAL NOT NULL DEFAULT 0,
                cost_reductions REAL NOT NULL DEFAULT 0,
                fg_qty_received REAL NOT NULL DEFAULT 0,
                actual_total_cost REAL NOT NULL DEFAULT 0,
                actual_unit_cost REAL NOT NULL DEFAULT 0,
                labor_amount REAL NOT NULL DEFAULT 0,
                oh_fixed_amount REAL NOT NULL DEFAULT 0,
                oh_variable_amount REAL NOT NULL DEFAULT 0,
                labor_idle REAL NOT NULL DEFAULT 0,
                oh_fixed_idle REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'closed',
                auto_posted INTEGER NOT NULL DEFAULT 0,
                allocation_id INTEGER,
                adjust_journal_entry_id INTEGER,
                note TEXT,
                created_by TEXT,
                created_at TEXT,
                UNIQUE(fiscal_year, period, status)
            );
            """
        )
    # Migrate columns on existing settings
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_costing_settings)').fetchall()}
    altered = False
    for col, decl in (
        ('auto_close', 'INTEGER NOT NULL DEFAULT 0'),
        ('policy_locked', 'INTEGER NOT NULL DEFAULT 0'),
        ('policy_locked_at', 'TEXT'),
        ('require_finalize_before_fg', 'INTEGER NOT NULL DEFAULT 0'),
    ):
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE sme_costing_settings ADD COLUMN {col} {decl}')
                altered = True
            except sqlite3.OperationalError:
                pass

    row = conn.execute('SELECT id FROM sme_costing_settings WHERE id = 1').fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO sme_costing_settings (
                id, allocation_method, normal_capacity_month, working_days_month,
                require_finalize_before_fg, department_name, auto_close, policy_locked, updated_at
            ) VALUES (1, 'normal_capacity', 500, 25, 0, 'Bộ phận sản xuất', 0, 0, ?)
            """,
            (_now(),),
        )
        altered = True

    # Order columns for provisional costing
    try:
        ocols = {r[1] for r in conn.execute('PRAGMA table_info(production_orders)').fetchall()}
        for col, decl in (
            ('costing_method', 'TEXT'),
            ('provisional_labor', 'REAL DEFAULT 0'),
            ('provisional_oh_fixed', 'REAL DEFAULT 0'),
            ('provisional_oh_variable', 'REAL DEFAULT 0'),
            ('provisional_unit_cost', 'REAL DEFAULT 0'),
            ('provisional_total_cost', 'REAL DEFAULT 0'),
            ('wip_opening_cost', 'REAL DEFAULT 0'),
            ('cost_basis', "TEXT DEFAULT 'provisional'"),
        ):
            if col not in ocols:
                conn.execute(f'ALTER TABLE production_orders ADD COLUMN {col} {decl}')
                altered = True
    except sqlite3.Error:
        pass

    if commit or altered:
        sqlite_commit(conn, label='costing_policy')


def get_costing_policy(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_costing_policy_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM sme_costing_settings WHERE id = 1').fetchone()
    data = dict(row) if row else {
        'allocation_method': METHOD_NORMAL,
        'normal_capacity_month': 500,
        'working_days_month': 25,
        'require_finalize_before_fg': 0,
        'department_name': 'Bộ phận sản xuất',
        'auto_close': 0,
        'policy_locked': 0,
    }
    method = (data.get('allocation_method') or METHOD_NORMAL).strip()
    if method not in METHODS:
        method = METHOD_NORMAL
    data['allocation_method'] = method
    data['allocation_method_label'] = METHOD_LABELS.get(method, method)
    data['auto_close'] = int(data.get('auto_close') or 0)
    data['policy_locked'] = int(data.get('policy_locked') or 0)
    data['require_finalize_before_fg'] = int(data.get('require_finalize_before_fg') or 0)
    data['has_production_orders'] = _has_production_orders(conn)
    # Auto-lock if orders exist and method was saved
    if data['has_production_orders'] and not data['policy_locked']:
        data['policy_locked'] = 1
    return data


def _has_production_orders(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            """
            SELECT 1 FROM production_orders
            WHERE COALESCE(status, '') != 'cancelled'
            LIMIT 1
            """
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def assert_method_unlocked(conn: sqlite3.Connection, method: str | None = None) -> None:
    """Định mức từng PA vẫn sửa được; chỉ khóa ĐỔI phương án áp dụng khi đã có lệnh SX."""
    return


def assert_can_change_method(conn: sqlite3.Connection) -> None:
    policy = get_costing_policy(conn)
    if policy.get('has_production_orders') or policy.get('policy_locked'):
        raise ValueError(
            'Đã có lệnh sản xuất — không được đổi phương án giá thành trong kỳ. '
            'Chỉ đổi được sau khi hủy toàn bộ lệnh hoặc sang kỳ mới (mở khóa chính sách).'
        )


def save_costing_policy(
    conn: sqlite3.Connection,
    *,
    allocation_method: str = METHOD_NORMAL,
    normal_capacity_month: float = 500,
    working_days_month: float = 25,
    require_finalize_before_fg: bool = False,
    department_name: str = 'Bộ phận sản xuất',
    auto_close: bool = False,
    force_method: bool = False,
    commit: bool = True,
) -> dict:
    ensure_costing_policy_schema(conn)
    method = (allocation_method or METHOD_NORMAL).strip()
    if method not in METHODS:
        raise ValueError('Phương án không hợp lệ (chọn PA1 / PA2 / PA3)')
    cap = _f(normal_capacity_month)
    days = _f(working_days_month)
    if cap <= 0 or days <= 0:
        raise ValueError('Công suất bình thường và số ngày làm việc phải > 0')

    current = get_costing_policy(conn)
    if (not force_method) and method != current.get('allocation_method'):
        assert_can_change_method(conn)

    locked = 1 if (current.get('has_production_orders') or current.get('policy_locked')) else 0
    locked_at = current.get('policy_locked_at') or (_now() if locked else None)

    conn.execute(
        """
        INSERT INTO sme_costing_settings (
            id, allocation_method, normal_capacity_month, working_days_month,
            require_finalize_before_fg, department_name, auto_close,
            policy_locked, policy_locked_at, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            allocation_method = excluded.allocation_method,
            normal_capacity_month = excluded.normal_capacity_month,
            working_days_month = excluded.working_days_month,
            require_finalize_before_fg = excluded.require_finalize_before_fg,
            department_name = excluded.department_name,
            auto_close = excluded.auto_close,
            policy_locked = excluded.policy_locked,
            policy_locked_at = COALESCE(excluded.policy_locked_at, sme_costing_settings.policy_locked_at),
            updated_at = excluded.updated_at
        """,
        (
            method, cap, days,
            1 if require_finalize_before_fg else 0,
            (department_name or 'Bộ phận sản xuất').strip(),
            1 if auto_close else 0,
            locked, locked_at, _now(),
        ),
    )
    if commit:
        sqlite_commit(conn, label='costing_policy')
    return get_costing_policy(conn)


def lock_policy_on_first_order(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    ensure_costing_policy_schema(conn)
    conn.execute(
        """
        UPDATE sme_costing_settings
        SET policy_locked = 1, policy_locked_at = COALESCE(policy_locked_at, ?)
        WHERE id = 1
        """,
        (_now(),),
    )
    if commit:
        sqlite_commit(conn, label='costing_policy')


def unlock_policy_if_no_orders(conn: sqlite3.Connection, *, commit: bool = True) -> dict:
    """Mở khóa khi không còn lệnh SX hiệu lực (phục vụ demo / đầu kỳ mới)."""
    ensure_costing_policy_schema(conn)
    if _has_production_orders(conn):
        raise ValueError('Vẫn còn lệnh sản xuất — không mở khóa được')
    conn.execute(
        """
        UPDATE sme_costing_settings
        SET policy_locked = 0, policy_locked_at = NULL, updated_at = ?
        WHERE id = 1
        """,
        (_now(),),
    )
    if commit:
        sqlite_commit(conn, label='costing_policy')
    return get_costing_policy(conn)


def set_auto_close(conn: sqlite3.Connection, enabled: bool, *, commit: bool = True) -> dict:
    ensure_costing_policy_schema(conn)
    conn.execute(
        'UPDATE sme_costing_settings SET auto_close = ?, updated_at = ? WHERE id = 1',
        (1 if enabled else 0, _now()),
    )
    if commit:
        sqlite_commit(conn, label='costing_policy')
    return get_costing_policy(conn)


def is_auto_close_enabled(conn: sqlite3.Connection) -> bool:
    return bool(int(get_costing_policy(conn).get('auto_close') or 0))


def get_period_close(conn: sqlite3.Connection, fiscal_year: int, period: int) -> dict | None:
    ensure_costing_policy_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT * FROM sme_costing_period_closes
        WHERE fiscal_year = ? AND period = ? AND status = 'closed'
        ORDER BY id DESC LIMIT 1
        """,
        (int(fiscal_year), int(period)),
    ).fetchone()
    return dict(row) if row else None


def list_period_closes(conn: sqlite3.Connection, *, fiscal_year: int | None = None) -> list[dict]:
    ensure_costing_policy_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM sme_costing_period_closes WHERE status IN ('closed', 'reversed', 'history')"
    params: list = []
    if fiscal_year:
        sql += ' AND fiscal_year = ?'
        params.append(int(fiscal_year))
    sql += ' ORDER BY fiscal_year DESC, period DESC, id DESC'
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
