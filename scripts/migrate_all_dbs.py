#!/usr/bin/env python3
"""Migrate schema cho database.db + mọi tenant — chạy trên VPS sau git pull."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.init import migrate_all_databases

if __name__ == '__main__':
    ok, fail = migrate_all_databases(verbose=True)
    sys.exit(1 if fail else 0)
