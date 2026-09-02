#!/usr/bin/env python3
"""Smoke: 3 PA policy + standards + PA1 math + auto_close flag."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')


def main() -> None:
    from Services.sme.costing_policy import (
        assert_can_change_method,
        ensure_costing_policy_schema,
        get_costing_policy,
        lock_policy_on_first_order,
        save_costing_policy,
    )
    from Services.sme.product_cost_standards import (
        METHOD_ACTUAL,
        METHOD_NORMAL,
        METHOD_STANDARD,
        ensure_product_cost_standards_schema,
        preview_order_from_standard,
        save_standard,
    )
    from Services.sme.period_cost_allocation import preview_allocation

    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        db = tmp.name
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ensure_costing_policy_schema(conn, commit=True)
    ensure_product_cost_standards_schema(conn, commit=True)

    conn.executescript(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY, name TEXT, product_code TEXT, unit TEXT,
            product_type TEXT DEFAULT 'finished_goods'
        );
        CREATE TABLE production_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT, production_date TEXT, finished_product_id INTEGER,
            qty_planned REAL, qty_completed REAL, qty_received REAL DEFAULT 0,
            defer_fg_receipt INTEGER DEFAULT 1,
            total_material_cost REAL DEFAULT 0, labor_cost REAL DEFAULT 0,
            other_cost REAL DEFAULT 0, total_cost REAL DEFAULT 0, unit_cost REAL DEFAULT 0,
            status TEXT DEFAULT 'in_progress', cost_finalized INTEGER DEFAULT 0,
            provisional_labor REAL DEFAULT 0, provisional_oh_fixed REAL DEFAULT 0,
            provisional_oh_variable REAL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO products VALUES (1,'SPA','SPA','cai','finished_goods')"
    )
    conn.execute(
        "INSERT INTO products VALUES (2,'NVL','NVL1','kg','material')"
    )
    conn.commit()

    # Standards for all 3 methods
    for method in (METHOD_NORMAL, METHOD_ACTUAL, METHOD_STANDARD):
        save_standard(
            conn,
            allocation_method=method,
            finished_product_id=1,
            labor_std_per_unit=72000,
            oh_fixed_std_per_unit=10000,
            oh_variable_std_per_unit=0,
            materials=[{'material_product_id': 2, 'qty_per_unit': 1}],
            commit=True,
        )

    # Mock get_wac by patching preview - materials will use 0 WAC without inventory
    # Just check labor/oh math
    from Services.sme import product_cost_standards as pcs
    orig = pcs.preview_order_from_standard

    # Policy PA3 + auto
    pol = save_costing_policy(
        conn, allocation_method=METHOD_STANDARD, auto_close=True, commit=True,
    )
    assert pol['allocation_method'] == METHOD_STANDARD
    assert int(pol['auto_close']) == 1

    # Lock after fake order
    conn.execute(
        "INSERT INTO production_orders (voucher_no, production_date, finished_product_id, "
        "qty_planned, qty_completed, total_material_cost, status) "
        "VALUES ('SX1','2026-08-05',1,150,150,5000000,'in_progress')"
    )
    conn.commit()
    lock_policy_on_first_order(conn, commit=True)
    try:
        assert_can_change_method(conn)
        raise AssertionError('should lock method change')
    except ValueError:
        pass

    # Can still switch settings same method
    save_costing_policy(
        conn, allocation_method=METHOD_STANDARD, auto_close=False, commit=True,
    )
    assert get_costing_policy(conn)['auto_close'] == 0

    # PA1 preview math
    p1 = preview_allocation(
        conn, fiscal_year=2026, period=8,
        date_from='2026-08-01', date_to='2026-08-10',
        labor_amount=14_400_000, oh_fixed_amount=0, oh_variable_amount=0,
        allocation_method=METHOD_NORMAL,
        normal_capacity_month=500, working_days_month=25,
    )
    assert abs(p1['capacity_in_scope'] - 200) < 1e-6
    assert abs(p1['labor_allocated'] - 10_800_000) < 1
    assert abs(p1['labor_idle'] - 3_600_000) < 1

    # PA2 no idle
    p2 = preview_allocation(
        conn, fiscal_year=2026, period=8,
        date_from='2026-08-01', date_to='2026-08-10',
        labor_amount=14_400_000, oh_fixed_amount=0, oh_variable_amount=0,
        allocation_method=METHOD_ACTUAL,
    )
    assert p2['labor_idle'] == 0
    assert abs(p2['labor_allocated'] - 14_400_000) < 1

    print('OK: 3-method costing policy + standards + PA1/PA2 math')
    conn.close()
    Path(db).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
