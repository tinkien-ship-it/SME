"""Load .env PG keys into os.environ (no print of secrets) then run smoke --live if URL set."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
KEYS = ('SME_DB_BACKEND', 'DATABASE_URL', 'SME_PG_URL', 'DATABASE_BACKEND')


def load_dotenv_keys() -> None:
    path = BASE / '.env'
    if not path.is_file():
        return
    for raw in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        key = key.strip()
        if key not in KEYS:
            continue
        if key in os.environ and os.environ.get(key):
            continue
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        os.environ[key] = val


def _pg_url() -> str:
    for key in ('SME_PG_URL', 'DATABASE_URL'):
        url = (os.environ.get(key) or '').strip()
        if url.startswith('postgres://') or url.startswith('postgresql://') or url.startswith('postgresql+psycopg://'):
            return url
    return ''


def main() -> int:
    load_dotenv_keys()
    url = _pg_url()
    cmd = [sys.executable, str(BASE / 'scripts' / 'pg_staging_smoke.py')]
    if url:
        os.environ.setdefault('SME_DB_BACKEND', 'postgres')
        # Prefer real PG URL even if DATABASE_URL is sqlite:/// for SQLAlchemy
        if not (os.environ.get('SME_PG_URL') or '').strip():
            os.environ['SME_PG_URL'] = url
        cmd.append('--live')
        print('Running pg_staging_smoke --live (PostgreSQL URL present)', flush=True)
    else:
        print('Running pg_staging_smoke unit only (no PostgreSQL URL)', flush=True)
    return subprocess.call(cmd)


if __name__ == '__main__':
    raise SystemExit(main())
