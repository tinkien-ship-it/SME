# -*- coding: utf-8 -*-
"""Smoke test các luồng dễ treo / database locked.

Chạy: python scripts/smoke_hang_test.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Tránh migrate 10 DB khi import app (server khác đang giữ lock)
os.environ.setdefault('SME_SKIP_RUNTIME_MIGRATE', '1')

DB = ROOT / 'tenants' / 'sme_demo.db'
TIMEOUT_SEC = 15


def _timed(label: str, fn, timeout: float = TIMEOUT_SEC) -> tuple[bool, str, float]:
    t0 = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(fn).result(timeout=timeout)
        ms = (time.perf_counter() - t0) * 1000
        return True, f'OK ({ms:.0f}ms)', ms
    except FuturesTimeout:
        return False, f'TIMEOUT >{timeout}s', timeout * 1000
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return False, f'{type(e).__name__}: {e}', ms


def _conn_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{DB.as_posix()}?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _conn_rw() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def test_work_calendar_schema():
    from Services.hrm.work_calendar import ensure_work_calendar_schema, get_work_calendar_config
    conn = _conn_rw()
    try:
        ensure_work_calendar_schema(conn)
        ensure_work_calendar_schema(conn)  # lần 2 không được treo
        cfg = get_work_calendar_config(conn)
        assert cfg['work_start'] == '08:00', cfg['work_start']
    finally:
        conn.close()


def test_hrm_schema():
    from Services.hrm.schema import ensure_hrm_schema
    conn = _conn_rw()
    try:
        ensure_hrm_schema(conn)
        ensure_hrm_schema(conn)
    finally:
        conn.close()


def test_contracts_crud():
    from Services.hrm.contracts import list_contracts, get_contract
    conn = _conn_ro()
    try:
        rows = list_contracts(conn, status=None)
        if rows:
            get_contract(conn, rows[0]['id'])
    finally:
        conn.close()


def test_contract_print():
    from Services.hrm.contract_templates import build_contract_print_context
    conn = _conn_ro()
    try:
        build_contract_print_context(conn, 1)
    finally:
        conn.close()


def test_single_db_migrate():
    from db.init import apply_schema_migrations
    conn = sqlite3.connect(str(DB), timeout=10)
    try:
        apply_schema_migrations(conn)
    finally:
        conn.close()


def test_parallel_schema_reads():
    def _read():
        conn = _conn_ro()
        try:
            from Services.hrm.work_calendar import get_work_calendar_config
            get_work_calendar_config(conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_read) for _ in range(4)]
        for f in futs:
            f.result(timeout=TIMEOUT_SEC)


def test_port_guard():
    from Services.runtime_guard import dev_server_port_taken
    dev_server_port_taken(5000)


def test_startup_migrate_once():
    os.environ['SME_SKIP_RUNTIME_MIGRATE'] = '1'
    # Import app nặng — chỉ chạy khi cần; bỏ qua nếu timeout
    from app import _run_startup_migrations
    _run_startup_migrations()
    _run_startup_migrations()


def main() -> int:
    if not DB.is_file():
        print(f'Not found: {DB}')
        return 1

    print(f'Smoke hang test - {DB.name}\n')
    tests = [
        ('work_calendar schema (×2)', test_work_calendar_schema),
        ('hrm schema (×2)', test_hrm_schema),
        ('contracts list/get', test_contracts_crud),
        ('contract print context', test_contract_print),
        ('apply_schema_migrations 1 DB', test_single_db_migrate),
        ('4 parallel calendar reads', test_parallel_schema_reads),
        ('port 5000 guard', test_port_guard),
    ]

    if os.environ.get('SMOKE_TEST_APP_IMPORT') == '1':
        tests.append(('startup migrate once (import app)', test_startup_migrate_once))

    failed = 0
    for label, fn in tests:
        ok, msg, _ = _timed(label, fn)
        mark = 'PASS' if ok else 'FAIL'
        print(f'  [{mark}] {label}: {msg}')
        if not ok:
            failed += 1

    # Kiểm tra process app.py trùng
    try:
        import subprocess
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*app.py*' } | Measure-Object | "
             "Select-Object -ExpandProperty Count"],
            text=True,
            timeout=10,
        ).strip()
        n = int(out or '0')
        if n > 1:
            print(f'\n  [WARN] {n} app.py processes - risk database locked')
        elif n == 1:
            print(f'\n  [OK] 1 app.py process')
        else:
            print(f'\n  [INFO] no app.py running')
    except Exception as e:
        print(f'\n  [SKIP] process check: {e}')

    print(f'\nResult: {len(tests) - failed}/{len(tests)} pass')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
