"""Backup PostgreSQL bằng pg_dump (toàn DB hoặc schema registry).

Usage:
  export DATABASE_URL=postgresql://...
  python scripts/pg_dump_backup.py
  python scripts/pg_dump_backup.py --out backups/pg
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def _database_url() -> str:
    for key in ('SME_PG_URL', 'DATABASE_URL'):
        url = (os.environ.get(key) or '').strip()
        if not url:
            continue
        if url.startswith('postgres://'):
            url = 'postgresql://' + url[len('postgres://'):]
        if url.startswith('postgresql+psycopg://'):
            url = 'postgresql://' + url[len('postgresql+psycopg://'):]
        if url.startswith('postgresql://'):
            return url
    raise SystemExit('Thiếu SME_PG_URL / DATABASE_URL dạng postgresql://...')


def find_pg_dump() -> str:
    env = (os.environ.get('SME_PG_DUMP') or '').strip()
    if env and Path(env).exists():
        return env
    which = shutil.which('pg_dump')
    if which:
        return which
    raise SystemExit('Không tìm thấy pg_dump — cài PostgreSQL client hoặc set SME_PG_DUMP')


def run_backup(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = out_dir / f'sme_pg_{stamp}.dump'
    pg_dump = find_pg_dump()
    url = _database_url()
    cmd = [
        pg_dump,
        '--format=custom',
        '--no-owner',
        '--no-acl',
        f'--file={dest}',
        url,
    ]
    print('Running:', pg_dump, '--format=custom ...')
    subprocess.check_call(cmd)
    print('OK:', dest)
    # Giữ tối đa 14 bản
    dumps = sorted(out_dir.glob('sme_pg_*.dump'), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dumps[14:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def main():
    parser = argparse.ArgumentParser(description='pg_dump backup SME')
    parser.add_argument(
        '--out',
        default=str(BASE / 'backups' / 'pg'),
        help='Thư mục lưu dump',
    )
    args = parser.parse_args()
    run_backup(Path(args.out))


if __name__ == '__main__':
    main()
