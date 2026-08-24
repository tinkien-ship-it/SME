"""Chuyển SQL SQLite → PostgreSQL — PRAGMA, sqlite_master, datetime, upsert, DDL."""
from __future__ import annotations

import re
from typing import Any

# --- DDL SQLite → PostgreSQL ---
def convert_sqlite_ddl(sql: str) -> str:
    """Chuyển CREATE TABLE SQLite sang PostgreSQL (cơ bản)."""
    text = sql.strip().rstrip(';')
    text = re.sub(r'\bAUTOINCREMENT\b', '', text, flags=re.I)
    text = re.sub(r'INTEGER PRIMARY KEY(?!\s*\()', 'SERIAL PRIMARY KEY', text, flags=re.I)
    text = re.sub(r'\bINTEGER\b', 'BIGINT', text, flags=re.I)
    text = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', text, flags=re.I)
    text = re.sub(r'\bBLOB\b', 'BYTEA', text, flags=re.I)
    text = re.sub(r'\bDATETIME\b', 'TIMESTAMP', text, flags=re.I)
    text = re.sub(
        r"datetime\s*\(\s*'now'\s*(?:,\s*'localtime'\s*)?\)",
        'CURRENT_TIMESTAMP',
        text,
        flags=re.I,
    )
    text = re.sub(r'\bUNIQUE\s*\([^)]+\)\s*ON CONFLICT REPLACE', 'UNIQUE', text, flags=re.I)
    return text


_PARAM_RE = re.compile(r'\?(?=(?:[^\']*\'[^\']*\')*[^\']*$)')


def _adapt_params(sql: str) -> str:
    return _PARAM_RE.sub('%s', sql)
_PRAGMA_TABLE_INFO = re.compile(
    r'PRAGMA\s+table_info\s*\(\s*["`]?([\w]+)["`]?\s*\)',
    re.IGNORECASE,
)
_PRAGMA_NOOP = re.compile(
    r'PRAGMA\s+(?:foreign_keys\s*=\s*(?:ON|OFF)|'
    r'journal_mode(?:\s*=\s*WAL)?|wal_autocheckpoint|journal_size_limit|'
    r'wal_checkpoint(?:\([^)]*\))?|busy_timeout|synchronous|temp_store|locking_mode)\s*[^;]*',
    re.IGNORECASE,
)
_PRAGMA_JOURNAL_FETCH = re.compile(r'PRAGMA\s+journal_mode\s*$', re.IGNORECASE)
_SQLITE_MASTER_EXISTS = re.compile(
    r"SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*['\"]table['\"]\s+AND\s+name\s*=\s*\?\s+LIMIT\s+1",
    re.IGNORECASE,
)
_SQLITE_MASTER_EXISTS_NO_LIMIT = re.compile(
    r"SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*['\"]table['\"]\s+AND\s+name\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_SQLITE_MASTER_LIST = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*['\"]table['\"]"
    r"(?:\s+AND\s+name\s+NOT\s+LIKE\s+['\"]sqlite_%['\"])?",
    re.IGNORECASE,
)
_SQLITE_SEQ_DELETE = re.compile(
    r"DELETE\s+FROM\s+sqlite_sequence\s+WHERE\s+name\s*=\s*\?",
    re.IGNORECASE,
)
_SQLITE_SEQ_INSERT = re.compile(
    r"INSERT\s+INTO\s+sqlite_sequence\s*\(\s*name\s*,\s*seq\s*\)\s*VALUES\s*\(\s*\?\s*,\s*\?\s*\)",
    re.IGNORECASE,
)

# --- rowid (SQLite only) ---
_ROWID_UPDATE = re.compile(
    r'UPDATE\s+[\w"]+\s+SET\s+id\s*=\s*rowid\b',
    re.IGNORECASE,
)

# --- datetime SQLite ---
_DATETIME_NOW = re.compile(r"datetime\s*\(\s*['\"]now['\"]\s*(?:,\s*['\"]localtime['\"])?\s*\)", re.IGNORECASE)
_CURRENT_TS = re.compile(r"CURRENT_TIMESTAMP(?!\s*\()", re.IGNORECASE)

# --- INSERT OR REPLACE / IGNORE ---
TABLE_UPSERT_KEYS: dict[str, list[str]] = {
    'settings': ['key'],
    'tenants': ['tenant_id'],
    'user_tenant_mapping': ['username'],
    'firm_users': ['login_email', 'firm_tenant_id'],
    'firm_user_client_access': ['firm_user_id', 'client_id'],
    'invoice_settings': ['provider_name'],
    'knowledge_sync_meta': ['key'],
    'import_sequence': ['id'],
    'menu_recipes': ['menu_id', 'product_id'],
    'tenant_branch_links': ['tenant_id', 'branch_code'],
    'hkd_nganh_nghe': ['ma_nganh'],
    'business_info': ['id'],
    'user_branches': ['user_id', 'branch_code'],
    'user_trusted_devices': ['username', 'device_fingerprint'],
    'inventory': ['product_id'],
    'voucher_seq': ['type'],
    'sme_tt58_tax_rates': ['sector_code', 'effective_from'],
}

_IOR_RE = re.compile(
    r'INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+["`]?([\w]+)["`]?\s*\(([^)]+)\)\s*VALUES\s*\((.+)\)',
    re.IGNORECASE | re.DOTALL,
)

_CREATE_TABLE = re.compile(r'^\s*CREATE\s+TABLE\b', re.IGNORECASE)


def _quote_ident(name: str) -> str:
    n = str(name or '').strip().strip('`"')
    return f'"{n}"'


def _quote_literal(value: str) -> str:
    v = str(value or '').replace("'", "''")
    return f"'{v}'"


def _pragma_table_info_sql(table: str, schema: str) -> str:
    sch = _quote_literal(schema)
    tbl = _quote_literal(table)
    return f"""
        SELECT
            (c.ordinal_position - 1)::bigint AS cid,
            c.column_name AS name,
            c.data_type AS type,
            CASE WHEN c.is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull,
            c.column_default AS dflt_value,
            CASE WHEN pk.column_name IS NOT NULL THEN 1 ELSE 0 END AS pk
        FROM information_schema.columns c
        LEFT JOIN (
            SELECT ku.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage ku
              ON tc.constraint_name = ku.constraint_name
             AND tc.table_schema = ku.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = {sch}
              AND tc.table_name = {tbl}
        ) pk ON pk.column_name = c.column_name
        WHERE c.table_schema = {sch} AND c.table_name = {tbl}
        ORDER BY c.ordinal_position
    """


def _convert_insert_or_replace(sql: str) -> str | None:
    m = _IOR_RE.search(sql)
    if not m:
        return None
    mode, raw_table, raw_cols, _vals = m.groups()
    table = raw_table.strip().strip('`"')
    cols = [c.strip().strip('`"') for c in raw_cols.split(',')]
    keys = TABLE_UPSERT_KEYS.get(table.lower()) or TABLE_UPSERT_KEYS.get(table)
    if not keys:
        return None
    col_sql = ', '.join(_quote_ident(c) for c in cols)
    conflict = ', '.join(_quote_ident(k) for k in keys)
    placeholders = ', '.join(['%s'] * len(cols))
    if mode.upper() == 'IGNORE':
        return (
            f'INSERT INTO {_quote_ident(table)} ({col_sql}) VALUES ({placeholders}) '
            f'ON CONFLICT ({conflict}) DO NOTHING'
        )
    updates = [c for c in cols if c not in keys]
    if not updates:
        return (
            f'INSERT INTO {_quote_ident(table)} ({col_sql}) VALUES ({placeholders}) '
            f'ON CONFLICT ({conflict}) DO NOTHING'
        )
    set_clause = ', '.join(f'{_quote_ident(c)} = EXCLUDED.{_quote_ident(c)}' for c in updates)
    return (
        f'INSERT INTO {_quote_ident(table)} ({col_sql}) VALUES ({placeholders}) '
        f'ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}'
    )


def rewrite_sql_for_postgres(sql: str, *, schema: str = 'public') -> str:
    """Chuyển câu SQL SQLite sang PostgreSQL (placeholder, PRAGMA, upsert, …)."""
    text = (sql or '').strip()
    if not text:
        return text

    # PRAGMA table_info
    m = _PRAGMA_TABLE_INFO.search(text)
    if m and text.upper().startswith('PRAGMA'):
        return _pragma_table_info_sql(m.group(1), schema)

    # PRAGMA journal_mode fetch (health check)
    if _PRAGMA_JOURNAL_FETCH.match(text):
        return "SELECT 'wal' AS journal_mode"

    # PRAGMA no-op
    if text.upper().startswith('PRAGMA') and _PRAGMA_NOOP.match(text):
        return 'SELECT 1 AS _pragma_ok'

    # sqlite_master → information_schema
    if _SQLITE_MASTER_EXISTS.search(text):
        sch = _quote_literal(schema)
        return (
            f'SELECT 1 FROM information_schema.tables '
            f'WHERE table_schema = {sch} AND table_name = %s LIMIT 1'
        )
    m = _SQLITE_MASTER_EXISTS_NO_LIMIT.search(text)
    if m:
        sch = _quote_literal(schema)
        table_name = _quote_literal(m.group(1))
        return (
            f'SELECT 1 FROM information_schema.tables '
            f'WHERE table_schema = {sch} AND table_name = {table_name}'
        )
    if _SQLITE_MASTER_LIST.search(text):
        sch = _quote_literal(schema)
        return (
            f"SELECT table_name AS name FROM information_schema.tables "
            f"WHERE table_schema = {sch} AND table_type = 'BASE TABLE' "
            f"AND table_name NOT LIKE 'pg_%' AND table_name NOT LIKE 'sql_%'"
        )

    # sqlite_sequence → no-op (sequence tự quản)
    if _SQLITE_SEQ_DELETE.match(text) or _SQLITE_SEQ_INSERT.match(text):
        return 'SELECT 1 AS _seq_ok'

    # rowid mirror → no-op trên PostgreSQL
    if _ROWID_UPDATE.search(text):
        return 'SELECT 1 AS _rowid_ok'

    text = _adapt_params(text)

    # datetime('now')
    text = _DATETIME_NOW.sub('CURRENT_TIMESTAMP', text)

    # CREATE TABLE SQLite DDL
    if _CREATE_TABLE.match(text):
        text = convert_sqlite_ddl(text)

    # INSERT OR REPLACE / IGNORE
    ior = _convert_insert_or_replace(text)
    if ior:
        return ior

    return text


class CompatRow:
    """Row hỗ trợ ``row[0]`` và ``row['col']`` — tương thích sqlite3.Row."""

    __slots__ = ('_values', '_names')

    def __init__(self, values: tuple[Any, ...], names: tuple[str, ...]):
        self._values = tuple(values)
        self._names = tuple(names)

    def __getitem__(self, key: int | str):
        if isinstance(key, int):
            return self._values[key]
        if isinstance(key, str):
            try:
                idx = self._names.index(key)
            except ValueError as exc:
                raise KeyError(key) from exc
            return self._values[idx]
        raise TypeError(key)

    def get(self, key: str, default: Any = None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return self._names

    def values(self):
        return self._values

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f'CompatRow({dict(zip(self._names, self._values))})'


def compat_row_factory(cursor):
    """Row factory psycopg — index + tên cột."""
    names = [d[0] for d in cursor.description] if cursor.description else []

    def make_row(values: tuple[Any, ...]):
        return CompatRow(values, tuple(names))

    return make_row
