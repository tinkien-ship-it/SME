"""Thuế suất HKD theo lịch hiệu lực — không hardcode trong báo cáo.

Bảng lưu trên main DB (database.db) để Master cấu hình dùng chung.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from db_utils import get_main_db_connection

# scope: loại thuế | revenue_tier | nn_code (nullable)
DEFAULT_TAX_RATES = (
    # DT3/DT4 — GTGT trên doanh thu + TNCN trên lãi
    {'scope': 'hkd_gtgt_on_revenue', 'revenue_tier': 'DT3', 'nn_code': None, 'rate_pct': 1.0},
    {'scope': 'hkd_gtgt_on_revenue', 'revenue_tier': 'DT4', 'nn_code': None, 'rate_pct': 1.0},
    {'scope': 'hkd_tncn_on_profit', 'revenue_tier': 'DT3', 'nn_code': None, 'rate_pct': 17.0},
    {'scope': 'hkd_tncn_on_profit', 'revenue_tier': 'DT4', 'nn_code': None, 'rate_pct': 20.0},
    # DT2 — theo ngành (GTGT / TNCN trên doanh thu ngành)
    {'scope': 'hkd_nn_gtgt', 'revenue_tier': 'DT2', 'nn_code': 'NN1', 'rate_pct': 1.0},
    {'scope': 'hkd_nn_tncn', 'revenue_tier': 'DT2', 'nn_code': 'NN1', 'rate_pct': 0.5},
    {'scope': 'hkd_nn_gtgt', 'revenue_tier': 'DT2', 'nn_code': 'NN2', 'rate_pct': 5.0},
    {'scope': 'hkd_nn_tncn', 'revenue_tier': 'DT2', 'nn_code': 'NN2', 'rate_pct': 2.0},
    {'scope': 'hkd_nn_gtgt', 'revenue_tier': 'DT2', 'nn_code': 'NN3', 'rate_pct': 3.0},
    {'scope': 'hkd_nn_tncn', 'revenue_tier': 'DT2', 'nn_code': 'NN3', 'rate_pct': 1.5},
    {'scope': 'hkd_nn_gtgt', 'revenue_tier': 'DT2', 'nn_code': 'NN4', 'rate_pct': 2.0},
    {'scope': 'hkd_nn_tncn', 'revenue_tier': 'DT2', 'nn_code': 'NN4', 'rate_pct': 1.0},
)

_SCHEMA_READY = False


def ensure_tax_rate_schema(conn=None) -> None:
    global _SCHEMA_READY
    own = conn is None
    if conn is None:
        conn = get_main_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tax_rate_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            revenue_tier TEXT,
            nn_code TEXT,
            rate_pct REAL NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tax_rate_lookup "
        "ON tax_rate_schedules(scope, revenue_tier, nn_code, effective_from)"
    )
    conn.commit()
    _seed_defaults(conn)
    if own:
        conn.close()
    _SCHEMA_READY = True


def _seed_defaults(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM tax_rate_schedules").fetchone()
    count = row[0] if not hasattr(row, 'keys') else row['c']
    if count:
        return
    for item in DEFAULT_TAX_RATES:
        conn.execute(
            """
            INSERT INTO tax_rate_schedules
                (scope, revenue_tier, nn_code, rate_pct, effective_from, note, created_by)
            VALUES (?, ?, ?, ?, '2020-01-01', 'Seed mặc định', 'system')
            """,
            (item['scope'], item['revenue_tier'], item['nn_code'], item['rate_pct']),
        )
    conn.commit()


def list_tax_rate_schedules(active_only: bool = False) -> list[dict]:
    ensure_tax_rate_schema()
    conn = get_main_db_connection()
    try:
        sql = "SELECT * FROM tax_rate_schedules"
        if active_only:
            sql += " WHERE effective_to IS NULL OR effective_to = '' OR effective_to >= date('now','localtime')"
        sql += " ORDER BY scope, revenue_tier, nn_code, effective_from DESC"
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_tax_rate_schedule(payload: dict[str, Any], created_by: str = 'master') -> dict:
    """Thêm mức thuế mới; đóng mức cũ cùng scope/tier/nn nếu còn mở."""
    ensure_tax_rate_schema()
    scope = (payload.get('scope') or '').strip()
    tier = (payload.get('revenue_tier') or '').strip() or None
    nn = (payload.get('nn_code') or '').strip() or None
    rate = float(payload.get('rate_pct') or 0)
    eff_from = (payload.get('effective_from') or '').strip()[:10]
    note = (payload.get('note') or '').strip()
    if not scope or not eff_from:
        raise ValueError('Thiếu scope hoặc effective_from')
    if rate < 0:
        raise ValueError('rate_pct không hợp lệ')

    conn = get_main_db_connection()
    try:
        # Đóng các dòng đang mở cùng khóa, hiệu lực trước ngày mới
        conn.execute(
            """
            UPDATE tax_rate_schedules
            SET effective_to = date(?, '-1 day')
            WHERE scope = ?
              AND IFNULL(revenue_tier, '') = IFNULL(?, '')
              AND IFNULL(nn_code, '') = IFNULL(?, '')
              AND (effective_to IS NULL OR effective_to = '')
              AND effective_from < ?
            """,
            (eff_from, scope, tier, nn, eff_from),
        )
        cur = conn.execute(
            """
            INSERT INTO tax_rate_schedules
                (scope, revenue_tier, nn_code, rate_pct, effective_from, note, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (scope, tier, nn, rate, eff_from, note, created_by),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM tax_rate_schedules WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_tax_rate_pct(
    scope: str,
    *,
    revenue_tier: str | None = None,
    nn_code: str | None = None,
    as_of: str | None = None,
    default: float | None = None,
) -> float | None:
    """Lấy % thuế còn hiệu lực tại as_of (YYYY-MM-DD)."""
    ensure_tax_rate_schema()
    as_of = (as_of or datetime.now().strftime('%Y-%m-%d'))[:10]
    conn = get_main_db_connection()
    try:
        row = conn.execute(
            """
            SELECT rate_pct FROM tax_rate_schedules
            WHERE scope = ?
              AND IFNULL(revenue_tier, '') = IFNULL(?, '')
              AND IFNULL(nn_code, '') = IFNULL(?, '')
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to = '' OR effective_to >= ?)
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            (scope, revenue_tier, nn_code, as_of, as_of),
        ).fetchone()
        if row:
            return float(row['rate_pct'] if hasattr(row, 'keys') else row[0])
        return default
    finally:
        conn.close()
