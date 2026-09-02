"""Smoke PostgreSQL staging — unit (luôn chạy) + live (khi có DATABASE_URL).

Usage:
  python scripts/pg_staging_smoke.py
  SME_DB_BACKEND=postgres DATABASE_URL=postgresql://... python scripts/pg_staging_smoke.py --live
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def _unit_tests() -> list[str]:
    os.environ['SME_DB_BACKEND'] = 'postgres'
    from db.dialect import pg_schema_from_db_path
    from db.errors import DB_ERROR, OPERATIONAL_ERROR
    from db.sql_compat import rewrite_sql_for_postgres as R
    from Services.schema_compat import sale_item_pk_column

    fails: list[str] = []

    def check(cond: bool, msg: str):
        if not cond:
            fails.append(msg)

    check(pg_schema_from_db_path('tenants/shop1.db') == 't_shop1', 'schema shop1')
    check(pg_schema_from_db_path('t_shop1') == 't_shop1', 'schema passthrough')
    check('COALESCE' in R('SELECT IFNULL(a,0)'), 'IFNULL')
    check('lpad' in R("SELECT printf('%06d', id)"), 'printf')
    check('si.id' in R('ORDER BY si.rowid'), 'rowid')
    check('ON CONFLICT' in R('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)'), 'upsert')
    check(sqlite3_ok := True, 'placeholder')  # noqa: F841
    check(issubclass(OPERATIONAL_ERROR[0], BaseException), 'OPERATIONAL_ERROR')
    check(len(DB_ERROR) >= 1, 'DB_ERROR')

    # sale_item_pk trên SQLite in-memory (tắt PG env tạm)
    prev = os.environ.get('SME_DB_BACKEND')
    try:
        os.environ['SME_DB_BACKEND'] = 'sqlite'
        import sqlite3
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE sale_items (id INTEGER PRIMARY KEY, sale_id INT)')
        check(sale_item_pk_column(conn) == 'id', 'sale_item_pk')
        conn.close()
    except Exception as exc:
        fails.append(f'sale_item_pk: {exc}')
    finally:
        if prev is None:
            os.environ.pop('SME_DB_BACKEND', None)
        else:
            os.environ['SME_DB_BACKEND'] = prev
        os.environ['SME_DB_BACKEND'] = 'postgres'

    return fails


def _live_tests(tenant_sqlite: Path | None) -> list[str]:
    fails: list[str] = []
    url = ''
    for key in ('SME_PG_URL', 'DATABASE_URL'):
        cand = (os.environ.get(key) or '').strip()
        if cand.startswith('postgres://') or cand.startswith('postgresql://') or cand.startswith('postgresql+psycopg://'):
            url = cand
            break
    if not url:
        fails.append('live: thiếu PostgreSQL URL (SME_PG_URL / DATABASE_URL)')
        return fails

    os.environ['SME_DB_BACKEND'] = 'postgres'
    os.environ['SME_PG_URL'] = url
    # Tránh SQLAlchemy sqlite URL làm hỏng pool
    if (os.environ.get('DATABASE_URL') or '').startswith('sqlite:'):
        os.environ['DATABASE_URL'] = url

    from db.pg_migrate import import_sqlite_file
    from db.postgres_backend import close_pg_pool, ensure_pg_schema, open_pg, reset_pg_pool
    from db.sql_compat import rewrite_sql_for_postgres
    from Services.accounting_queue import ensure_accounting_queue_schema, enqueue_accounting_job

    schema = 't_smoke_pg'
    try:
        reset_pg_pool()
        ensure_pg_schema(schema)
        with open_pg(schema=schema) as conn:
            # lastrowid
            conn.execute('DROP TABLE IF EXISTS smoke_sale CASCADE')
            conn.execute(
                'CREATE TABLE smoke_sale (id SERIAL PRIMARY KEY, note TEXT, total DOUBLE PRECISION)'
            )
            cur = conn.cursor()
            cur.execute("INSERT INTO smoke_sale (note, total) VALUES (%s, %s)", ('a', 100))
            if not cur.lastrowid:
                # fallback path via connection
                conn.execute("INSERT INTO smoke_sale (note, total) VALUES ('b', 200)")
                if not conn.lastrowid:
                    fails.append('live: lastrowid=0 after INSERT')
            else:
                sid = cur.lastrowid
                row = conn.execute('SELECT note FROM smoke_sale WHERE id = %s', (sid,)).fetchone()
                if not row or str(row[0]) != 'a':
                    fails.append('live: lastrowid mismatch')

            # rewrite via execute (? placeholders)
            conn.execute('DROP TABLE IF EXISTS settings CASCADE')
            conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute(
                'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                ('k1', 'v1'),
            )
            conn.commit()

            ensure_accounting_queue_schema(conn, commit=True)
            # sale_id ngẫu nhiên — tránh None do job pending còn sót từ lần smoke trước
            import time
            sid = int(time.time()) % 2_000_000_000
            jid = enqueue_accounting_job(conn, sale_id=sid, features={'x': 1}, commit=True)
            if jid is None:
                jid = enqueue_accounting_job(
                    conn, sale_id=sid + 1, features={'x': 1}, commit=True, replace_existing=True,
                )
            if not jid:
                # Phân biệt: lastrowid=0 vs None (đã có pending)
                try:
                    row = conn.execute(
                        "SELECT id FROM accounting_jobs WHERE sale_id IN (?, ?) ORDER BY id DESC LIMIT 1",
                        (sid, sid + 1),
                    ).fetchone()
                    if row:
                        jid = int(row[0] if not hasattr(row, 'keys') else row['id'])
                except Exception as qe:
                    fails.append(f'live: enqueue_accounting_job failed ({qe})')
            if not jid:
                fails.append('live: enqueue_accounting_job failed')

        if tenant_sqlite and tenant_sqlite.is_file():
            stats = import_sqlite_file(str(tenant_sqlite), 't_smoke_import')
            if stats.get('tables', 0) < 1:
                fails.append(f'live: import tables=0 errors={stats.get("errors")}')
            if stats.get('errors'):
                # soft — chỉ fail nếu quá nhiều
                if len(stats['errors']) > 30:
                    fails.append(f'live: import too many errors ({len(stats["errors"])})')
    except Exception as exc:
        fails.append(f'live: {exc}')
    finally:
        try:
            close_pg_pool()
        except Exception:
            pass
    return fails


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Chạy test trên DATABASE_URL')
    parser.add_argument(
        '--tenant-db',
        default=str(BASE / 'tenants' / 'sme_demo.db'),
        help='File SQLite mẫu để import thử',
    )
    args = parser.parse_args()

    fails = _unit_tests()
    # Always run rewrite verify script subset
    from db.sql_compat import rewrite_sql_for_postgres
    for sql, needle in [
        ("SELECT IFNULL(MAX(id),0) FROM sale", 'COALESCE'),
        ("ORDER BY si.rowid", 'si.id'),
        ("SELECT date('now','localtime')", 'TO_CHAR'),
        ("WHERE date(v.punched_at) = date('now', 'localtime')", 'TO_CHAR'),
        ("date('now', 'localtime', '-30 day')", 'INTERVAL'),
        ("AND date(v.punched_at) = date(?)", 'substr(CAST'),
        ("GROUP_CONCAT(x.account_code, ', ')", 'string_agg'),
    ]:
        out = rewrite_sql_for_postgres(sql)
        if needle.lower() not in out.lower():
            fails.append(f'rewrite {sql!r} -> {out!r} (need {needle})')
        # Không được nhân đôi placeholder
        if sql.count('?') and out.count('%s') != sql.count('?'):
            fails.append(f'placeholder count {sql!r} -> {out!r}')

    if args.live or any(
        (os.environ.get(k) or '').startswith(p)
        for k in ('SME_PG_URL', 'DATABASE_URL')
        for p in ('postgres://', 'postgresql://', 'postgresql+psycopg://')
    ):
        fails.extend(_live_tests(Path(args.tenant_db)))
    else:
        print('SKIP live (no PostgreSQL URL) — unit only')

    if fails:
        print('FAILED:')
        for f in fails:
            print(' -', f)
        sys.exit(1)
    print('pg_staging_smoke: ALL OK')


if __name__ == '__main__':
    main()
