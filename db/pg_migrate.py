"""Safe SQLite -> PostgreSQL tenant bootstrap. Never overwrite an existing schema."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from psycopg import sql as pgsql
from db.dialect import sanitize_pg_schema
from db.postgres_backend import get_pool
from db.sql_compat import convert_sqlite_ddl


def _tables(src):
    return [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _indexes(src):
    return [(r[0], r[1]) for r in src.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _index_sql(raw):
    ddl = convert_sqlite_ddl(raw.strip().rstrip(';'))
    if re.match(r'^\s*CREATE\s+(UNIQUE\s+)?INDEX\b', ddl, re.I) and 'IF NOT EXISTS' not in ddl.upper():
        ddl = re.sub(r'^\s*CREATE\s+(UNIQUE\s+)?INDEX\b', lambda m: 'CREATE ' + (m.group(1) or '') + 'INDEX IF NOT EXISTS', ddl, count=1, flags=re.I)
    return ddl


def _fallback(ddl):
    text = convert_sqlite_ddl(ddl)
    text = re.sub(r'GENERATED\s+ALWAYS\s+AS\s*\([^)]*\)\s*STORED', '', text, flags=re.I)
    return re.sub(r'\s+AFTER\s+[`"\']?[\w]+[`"\']?', '', text, flags=re.I)


def import_sqlite_file(sqlite_path: str, pg_schema: str, *, skip_tables: set[str] | None = None, batch_size: int = 500) -> dict[str, Any]:
    """Create a NEW tenant schema atomically. Existing schema -> refuse, never drop."""
    if not 1 <= batch_size <= 5000:
        raise ValueError('batch_size must be 1..5000')
    schema = sanitize_pg_schema(pg_schema)
    if schema in {'public', 'registry', 'information_schema', 'pg_catalog'} or not schema.startswith(('t_', 'firm_')):
        raise ValueError(f'Unsafe tenant schema: {schema}')
    stats = {'schema': schema, 'tables': 0, 'rows': 0, 'indexes': 0, 'errors': []}
    with sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True) as src:
        src.row_factory = sqlite3.Row
        tables = [t for t in _tables(src) if t not in (skip_tables or set())]
        with get_pool().connection() as pg:
            # Entire schema and all its tables commit together. No autocommit.
            if pg.autocommit:
                pg.autocommit = False
            with pg.transaction():
                exists = pg.execute('SELECT 1 FROM pg_namespace WHERE nspname = %s', (schema,)).fetchone()
                if exists:
                    raise RuntimeError(f'Schema {schema} already exists. Refusing destructive import; inspect/reconcile it first.')
                pg.execute(pgsql.SQL('CREATE SCHEMA {}').format(pgsql.Identifier(schema)))
                pg.execute(pgsql.SQL('SET LOCAL search_path TO {}, public').format(pgsql.Identifier(schema)))
                for table in tables:
                    raw = src.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()
                    if not raw or not raw[0]:
                        raise RuntimeError(f'Missing SQLite DDL for {table}')
                    ddl = convert_sqlite_ddl(raw[0])
                    # Savepoint permits DDL fallback without rolling back the whole import.
                    pg.execute('SAVEPOINT ddl_fallback')
                    try:
                        pg.execute(ddl)
                        pg.execute('RELEASE SAVEPOINT ddl_fallback')
                    except Exception:
                        pg.execute('ROLLBACK TO SAVEPOINT ddl_fallback')
                        pg.execute('RELEASE SAVEPOINT ddl_fallback')
                        pg.execute(_fallback(raw[0]))
                    source_cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
                    pg_cols = {r[0] for r in pg.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s AND is_generated=\'NEVER\'', (schema, table))}
                    cols = [c for c in source_cols if c in pg_cols]
                    if len(cols) != len(source_cols):
                        missing = set(source_cols) - pg_cols
                        # SQLite generated columns are excluded by PRAGMA table_info.
                        raise RuntimeError(f'{table}: target missing source columns: {sorted(missing)}')
                    if cols:
                        identifiers = pgsql.SQL(', ').join(map(pgsql.Identifier, cols))
                        stmt = pgsql.SQL('INSERT INTO {} ({}) VALUES ({})').format(pgsql.Identifier(table), identifiers, pgsql.SQL(', ').join(pgsql.Placeholder() for _ in cols))
                        cursor = src.execute('SELECT ' + ', '.join('"' + c.replace('"','""') + '"' for c in cols) + ' FROM "' + table.replace('"','""') + '"')
                        while True:
                            rows = cursor.fetchmany(batch_size)
                            if not rows:
                                break
                            with pg.cursor() as dest:
                                with dest.copy(pgsql.SQL('COPY {} ({}) FROM STDIN').format(pgsql.Identifier(table), identifiers)) as copy:
                                    for row in rows:
                                        copy.write_row(tuple(row))
                            stats['rows'] += len(rows)
                    stats['tables'] += 1
                for name, raw in _indexes(src):
                    pg.execute(_index_sql(raw))
                    stats['indexes'] += 1
                for table in tables:
                    sequence = pg.execute('SELECT pg_get_serial_sequence(%s,%s)', (f'"{schema}"."{table}"', 'id')).fetchone()[0]
                    if sequence:
                        pg.execute(pgsql.SQL('SELECT setval(%s, COALESCE((SELECT MAX(id) FROM {}), 1), %s)').format(pgsql.Identifier(table)), (sequence, bool(pg.execute(pgsql.SQL('SELECT MAX(id) IS NOT NULL FROM {}').format(pgsql.Identifier(table))).fetchone()[0])))
    return stats
