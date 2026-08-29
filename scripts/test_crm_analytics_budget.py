# -*- coding: utf-8 -*-
"""Smoke: analytics_bundle không treo quá budget."""
import sqlite3
import time

from Services.crm_analytics import analytics_bundle
from Services.crm_schema import ensure_crm_schema


def main() -> None:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_crm_schema(conn)
    t0 = time.monotonic()
    out = analytics_bundle(conn, budget_sec=5)
    elapsed = time.monotonic() - t0
    assert 'source_pie' in out and 'retention' in out
    assert elapsed < 5.5, elapsed
    print('analytics_bundle ok', round(elapsed, 3), 's', out.get('_meta'))


if __name__ == '__main__':
    main()
