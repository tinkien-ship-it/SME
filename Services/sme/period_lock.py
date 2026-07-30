"""Khóa sổ kỳ kế toán SME — chặn ghi/đảo bút toán khi kỳ đã chốt."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


def ensure_period_lock_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_period_locks (
            fiscal_year INTEGER NOT NULL,
            period INTEGER NOT NULL,
            locked_at TEXT NOT NULL,
            locked_by TEXT,
            reason TEXT,
            PRIMARY KEY (fiscal_year, period)
        )
        """
    )
    if commit:
        conn.commit()


def is_period_locked(conn: sqlite3.Connection, fiscal_year: int, period: int) -> bool:
    ensure_period_lock_schema(conn, commit=False)
    row = conn.execute(
        "SELECT 1 FROM sme_period_locks WHERE fiscal_year = ? AND period = ?",
        (fiscal_year, period),
    ).fetchone()
    return bool(row)


def get_period_lock(
    conn: sqlite3.Connection, fiscal_year: int, period: int,
) -> dict[str, Any] | None:
    ensure_period_lock_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sme_period_locks WHERE fiscal_year = ? AND period = ?",
        (fiscal_year, period),
    ).fetchone()
    return dict(row) if row else None


def lock_period(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    locked_by: str | None = None,
    reason: str = 'Chốt kỳ tự động',
) -> dict[str, Any]:
    ensure_period_lock_schema(conn, commit=False)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """
        INSERT INTO sme_period_locks (fiscal_year, period, locked_at, locked_by, reason)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fiscal_year, period) DO UPDATE SET
            locked_at = excluded.locked_at,
            locked_by = excluded.locked_by,
            reason = excluded.reason
        """,
        (fiscal_year, period, now, locked_by, reason),
    )
    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'locked_at': now,
        'locked_by': locked_by,
        'reason': reason,
    }


def unlock_period(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
) -> bool:
    ensure_period_lock_schema(conn, commit=False)
    cur = conn.execute(
        "DELETE FROM sme_period_locks WHERE fiscal_year = ? AND period = ?",
        (fiscal_year, period),
    )
    return cur.rowcount > 0


def assert_period_open(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
    *,
    action: str = 'ghi sổ',
) -> None:
    if is_period_locked(conn, fiscal_year, period):
        raise ValueError(
            f'Kỳ {period:02d}/{fiscal_year} đã khóa sổ — không thể {action}. '
            f'Mở khóa tại /SME_auto_posting nếu cần chỉnh sửa.'
        )


def list_locked_periods(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
) -> list[dict[str, Any]]:
    ensure_period_lock_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    if fiscal_year:
        rows = conn.execute(
            "SELECT * FROM sme_period_locks WHERE fiscal_year = ? ORDER BY period",
            (fiscal_year,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sme_period_locks ORDER BY fiscal_year DESC, period DESC"
        ).fetchall()
    return [dict(r) for r in rows]
