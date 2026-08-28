#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bật WAL cho database.db và mọi tenants/*.db (dev / SQLite VPS)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from db.sqlite_wal import ensure_all_sqlite_wal

    ok, fail = ensure_all_sqlite_wal(verbose=True)
    print(f'Done: {ok} OK, {fail} failed')
    return 1 if fail else 0


if __name__ == '__main__':
    raise SystemExit(main())
