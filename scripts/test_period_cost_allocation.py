#!/usr/bin/env python3
"""Smoke test phân bổ 622/627 cuối kỳ (PA1 công suất bình thường + PA2 actual).

Ví dụ PA1: CS 500 SP/tháng, 25 ngày LV, phạm vi 10 ngày → CS phạm vi 200.
NC 14.400.000 → suất 72.000/SP; SX 150 SP → vào GT 10.800.000; idle 3.600.000.

Usage:
  python scripts/test_period_cost_allocation.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding='utf-8')


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _setup_minimal(conn: sqlite3.Connection) -> int:
    """Tạo bảng tối thiểu + 1 lệnh SX để preview (không cần journal)."""
    conn.executescript(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT, product_code TEXT, unit TEXT,
            product_type TEXT DEFAULT 'finished_goods',
            costing_equivalent_factor REAL DEFAULT 1
        );
        CREATE TABLE production_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT,
            production_date TEXT,
            finished_product_id INTEGER,
            qty_planned REAL,
            qty_completed REAL,
            qty_received REAL DEFAULT 0,
            defer_fg_receipt INTEGER DEFAULT 1,
            total_material_cost REAL DEFAULT 0,
            labor_cost REAL DEFAULT 0,
            other_cost REAL DEFAULT 0,
            total_cost REAL DEFAULT 0,
            unit_cost REAL DEFAULT 0,
            status TEXT DEFAULT 'in_progress',
            cost_finalized INTEGER DEFAULT 0,
            allocation_id INTEGER,
            oh_fixed_cost REAL DEFAULT 0,
            oh_variable_cost REAL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO products (id, name, product_code, unit, product_type) "
        "VALUES (1, 'SP A', 'SPA', 'cái', 'finished_goods')"
    )
    conn.execute(
        """
        INSERT INTO production_orders (
            voucher_no, production_date, finished_product_id,
            qty_planned, qty_completed, total_material_cost, total_cost, status
        ) VALUES ('SX-TEST-1', '2026-08-05', 1, 150, 150, 5000000, 5000000, 'in_progress')
        """
    )
    conn.commit()
    return 1


def test_preview_methods() -> None:
    from Services.sme.period_cost_allocation import (
        METHOD_ACTUAL,
        METHOD_NORMAL,
        ensure_period_cost_allocation_schema,
        preview_allocation,
        save_costing_settings,
    )

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        db_path = tmp.name

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _setup_minimal(conn)
    ensure_period_cost_allocation_schema(conn, commit=True)
    save_costing_settings(
        conn,
        allocation_method=METHOD_NORMAL,
        normal_capacity_month=500,
        working_days_month=25,
        require_finalize_before_fg=True,
        commit=True,
    )

    # --- PA1 ---
    p1 = preview_allocation(
        conn,
        fiscal_year=2026,
        period=8,
        date_from='2026-08-01',
        date_to='2026-08-10',
        labor_amount=14_400_000,
        oh_fixed_amount=0,
        oh_variable_amount=0,
        allocation_method=METHOD_NORMAL,
    )
    print('PA1 preview:')
    print(f"  capacity_in_scope={p1['capacity_in_scope']}")
    print(f"  labor_rate={p1['labor_rate']}")
    print(f"  labor_allocated={p1['labor_allocated']:,.0f}")
    print(f"  labor_idle={p1['labor_idle']:,.0f}")
    print(f"  line0 labor={p1['lines'][0]['labor_allocated']:,.0f} total={p1['lines'][0]['total_cost']:,.0f}")

    _assert(p1['days_count'] == 10, f"days_count={p1['days_count']}")
    _assert(abs(p1['capacity_in_scope'] - 200) < 1e-6, f"cap={p1['capacity_in_scope']}")
    _assert(abs(p1['labor_rate'] - 72_000) < 0.01, f"rate={p1['labor_rate']}")
    _assert(abs(p1['labor_allocated'] - 10_800_000) < 0.02, f"alloc={p1['labor_allocated']}")
    _assert(abs(p1['labor_idle'] - 3_600_000) < 0.02, f"idle={p1['labor_idle']}")
    _assert(abs(p1['lines'][0]['total_cost'] - 15_800_000) < 0.02, 'total cost PA1')

    # --- PA2 ---
    p2 = preview_allocation(
        conn,
        fiscal_year=2026,
        period=8,
        date_from='2026-08-01',
        date_to='2026-08-10',
        labor_amount=14_400_000,
        oh_fixed_amount=2_000_000,
        oh_variable_amount=500_000,
        allocation_method=METHOD_ACTUAL,
    )
    print('PA2 preview:')
    print(f"  labor_allocated={p2['labor_allocated']:,.0f} idle={p2['labor_idle']}")
    print(f"  oh_f={p2['oh_fixed_allocated']:,.0f} oh_v={p2['oh_variable_allocated']:,.0f}")

    _assert(p2['labor_idle'] == 0 and p2['oh_fixed_idle'] == 0, 'PA2 must have no idle')
    _assert(abs(p2['labor_allocated'] - 14_400_000) < 0.02, 'PA2 labor all allocated')
    _assert(abs(p2['oh_fixed_allocated'] - 2_000_000) < 0.02, 'PA2 oh fixed')
    _assert(abs(p2['oh_variable_allocated'] - 500_000) < 0.02, 'PA2 oh var')
    _assert(
        abs(p2['lines'][0]['total_cost'] - (5_000_000 + 14_400_000 + 2_000_000 + 500_000)) < 0.02,
        'PA2 total',
    )

    # Gate helper
    from Services.sme.period_cost_allocation import require_cost_finalized_for_fg
    _assert(require_cost_finalized_for_fg(conn) is True, 'require finalize default')

    conn.close()
    Path(db_path).unlink(missing_ok=True)
    print('OK: period cost allocation smoke passed')


if __name__ == '__main__':
    try:
        test_preview_methods()
    except Exception as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        raise
