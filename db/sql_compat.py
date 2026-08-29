"""Chuyển SQL SQLite → PostgreSQL — PRAGMA, sqlite_master, datetime, upsert, DDL."""
from __future__ import annotations

import re
from typing import Any

# --- DDL SQLite → PostgreSQL ---
def convert_sqlite_ddl(sql: str) -> str:
    """Chuyển CREATE TABLE SQLite sang PostgreSQL (cơ bản)."""
    text = sql.strip().rstrip(';')

    # 1) datetime/date('now') TRƯỚC khi đổi kiểu DATETIME → TIMESTAMP
    #    (nếu đảo thứ tự sẽ thành TIMESTAMP('now') → Postgres syntax error)
    text = re.sub(
        r"datetime\s*\(\s*['\"]now['\"]\s*(?:,\s*['\"]localtime['\"])?\s*\)",
        'CURRENT_TIMESTAMP',
        text,
        flags=re.I,
    )
    text = re.sub(
        r"date\s*\(\s*['\"]now['\"]\s*(?:,\s*['\"]localtime['\"])?\s*\)",
        'CURRENT_DATE',
        text,
        flags=re.I,
    )
    # Phòng hờ bản đã bị convert nhầm trước đó
    text = re.sub(
        r"TIMESTAMP\s*\(\s*['\"]now['\"]\s*(?:,\s*['\"]localtime['\"])?\s*\)",
        'CURRENT_TIMESTAMP',
        text,
        flags=re.I,
    )

    # 2) MySQL: col ... AFTER other_col — Postgres không hỗ trợ
    text = re.sub(r'\s+AFTER\s+[`"\']?[\w]+[`"\']?', '', text, flags=re.I)

    text = re.sub(r'\bAUTOINCREMENT\b', '', text, flags=re.I)
    text = re.sub(r'INTEGER PRIMARY KEY(?!\s*\()', 'SERIAL PRIMARY KEY', text, flags=re.I)
    text = re.sub(r'\bINTEGER\b', 'BIGINT', text, flags=re.I)
    text = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', text, flags=re.I)
    text = re.sub(r'\bBLOB\b', 'BYTEA', text, flags=re.I)
    text = re.sub(r'\bDATETIME\b', 'TIMESTAMP', text, flags=re.I)
    text = re.sub(r'\bNUMERIC\b', 'NUMERIC', text, flags=re.I)
    text = re.sub(r'\bDOUBLE\b(?!\s+PRECISION)', 'DOUBLE PRECISION', text, flags=re.I)
    text = re.sub(r'\s+WITHOUT\s+ROWID\b', '', text, flags=re.I)
    text = re.sub(r'\s+ON\s+CONFLICT\s+(?:REPLACE|IGNORE|ABORT|FAIL|ROLLBACK)\b', '', text, flags=re.I)
    text = re.sub(r'\bUNIQUE\s*\([^)]+\)\s*ON CONFLICT REPLACE', 'UNIQUE', text, flags=re.I)
    text = re.sub(r'\s+COLLATE\s+NOCASE\b', '', text, flags=re.I)
    # Index / DDL: IFNULL → COALESCE
    text = re.sub(r'\bIFNULL\s*\(', 'COALESCE(', text, flags=re.I)

    # 3) Cột generated SQLite → Postgres STORED
    #    GENERATED ALWAYS AS (expr) VIRTUAL|STORED
    text = re.sub(
        r'GENERATED\s+ALWAYS\s+AS\s*\(([^)]*)\)\s*(?:VIRTUAL|STORED)?',
        r'GENERATED ALWAYS AS (\1) STORED',
        text,
        flags=re.I,
    )
    #    Shorthand: col TYPE[(prec)] AS (expr) [VIRTUAL|STORED]
    #    Hỗ trợ DECIMAL(18,2) / NUMERIC(18,2) / DOUBLE PRECISION / REAL…
    text = re.sub(
        r'(\b(?:DOUBLE PRECISION|BIGINT|SERIAL|NUMERIC|DECIMAL|REAL|TEXT|BYTEA|TIMESTAMP|BOOLEAN)'
        r'(?:\s*\([^)]*\))?)\s+AS\s*\(([^)]*)\)\s*(?:VIRTUAL|STORED)?',
        r'\1 GENERATED ALWAYS AS (\2) STORED',
        text,
        flags=re.I,
    )

    return text


_PARAM_RE = re.compile(r'\?(?=(?:[^\']*\'[^\']*\')*[^\']*$)')


def _adapt_params(sql: str) -> str:
    """``?`` → ``%s``; escape ``%`` literal (LIKE 'DV%') thành ``%%`` cho psycopg.

    Idempotent: SQL đã có ``%s`` (rewrite 2 lần / early-return) không bị thành ``%%s``.
    """
    if '?' not in sql:
        # Đã adapt hoặc không có placeholder — không đụng %s/%b/%t
        if '%s' in sql or '%b' in sql or '%t' in sql:
            return sql
        if '%' in sql:
            return sql.replace('%', '%%')
        return sql
    marked = _PARAM_RE.sub('\x00PH\x00', sql)
    marked = marked.replace('%', '%%')
    return marked.replace('\x00PH\x00', '%s')
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
    r"SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*['\"]table['\"]\s+AND\s+name\s*=\s*\?"
    r"(?:\s+COLLATE\s+NOCASE)?(?:\s+LIMIT\s+1)?",
    re.IGNORECASE,
)
_SQLITE_MASTER_EXISTS_NO_LIMIT = re.compile(
    r"SELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*['\"]table['\"]\s+AND\s+name\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_SQLITE_MASTER_NAME_EQ_LIT = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*['\"]table['\"]\s+AND\s+name\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_SQLITE_MASTER_NAME_EQ_PARAM = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*['\"]table['\"]\s+AND\s+name\s*=\s*\?",
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
_SQLITE_SEQ_UPDATE = re.compile(
    r"UPDATE\s+sqlite_sequence\b",
    re.IGNORECASE,
)

# last_insert_rowid() → lastval()
_LAST_INSERT_ROWID = re.compile(
    r'SELECT\s+last_insert_rowid\s*\(\s*\)',
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
    'sme_payroll_runs__mb': ['id'],
    'accounting_jobs': ['sale_id', 'job_type', 'status'],
    'warehouses': ['code'],
    'crm_assign_state': ['id'],
    'crm_settings': ['key'],
}

_IOR_RE = re.compile(
    r'INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+["`]?([\w]+)["`]?\s*\(([^)]+)\)\s*VALUES\s*(.+)',
    re.IGNORECASE | re.DOTALL,
)

_CREATE_TABLE = re.compile(r'^\s*CREATE\s+TABLE\b', re.IGNORECASE)

# IFNULL(a, b) → COALESCE(a, b)
_IFNULL_RE = re.compile(r'\bIFNULL\s*\(', re.IGNORECASE)

# printf('%06d', expr) → lpad((expr)::text, 6, '0')
_PRINTF_PAD = re.compile(
    r"""printf\s*\(\s*'%0(\d+)d'\s*,\s*([^)]+?)\)""",
    re.IGNORECASE,
)

# COLLATE NOCASE (runtime) → bỏ
_COLLATE_NOCASE = re.compile(r'\s+COLLATE\s+NOCASE\b', re.IGNORECASE)

# Alias.rowid / table.rowid → .id (sau khi sale_items có cột id)
_ROWID_COL = re.compile(r'\b([a-zA-Z_][\w]*)\.rowid\b', re.IGNORECASE)
_BARE_ROWID_ORDER = re.compile(r'\bORDER\s+BY\s+rowid\b', re.IGNORECASE)


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
    mode, raw_table, raw_cols, raw_vals = m.groups()
    table = raw_table.strip().strip('`"')
    cols = [c.strip().strip('`"') for c in raw_cols.split(',')]
    keys = TABLE_UPSERT_KEYS.get(table.lower()) or TABLE_UPSERT_KEYS.get(table)
    if not keys:
        return None
    col_sql = ', '.join(_quote_ident(c) for c in cols)
    conflict = ', '.join(_quote_ident(k) for k in keys)
    # Giữ nguyên VALUES (placeholder hoặc literal, 1 hay nhiều hàng)
    vals = raw_vals.strip().rstrip(';').strip()
    if mode.upper() == 'IGNORE':
        return (
            f'INSERT INTO {_quote_ident(table)} ({col_sql}) VALUES {vals} '
            f'ON CONFLICT ({conflict}) DO NOTHING'
        )
    updates = [c for c in cols if c not in keys]
    if not updates:
        return (
            f'INSERT INTO {_quote_ident(table)} ({col_sql}) VALUES {vals} '
            f'ON CONFLICT ({conflict}) DO NOTHING'
        )
    set_clause = ', '.join(f'{_quote_ident(c)} = EXCLUDED.{_quote_ident(c)}' for c in updates)
    return (
        f'INSERT INTO {_quote_ident(table)} ({col_sql}) VALUES {vals} '
        f'ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}'
    )


def _rewrite_ifnull(sql: str) -> str:
    """IFNULL(...) → COALESCE(...) (cùng arity)."""
    if not _IFNULL_RE.search(sql):
        return sql
    out = []
    i = 0
    upper = sql
    while True:
        m = _IFNULL_RE.search(upper, i)
        if not m:
            out.append(sql[i:])
            break
        out.append(sql[i:m.start()])
        out.append('COALESCE(')
        i = m.end()
    return ''.join(out)


def _rewrite_printf(sql: str) -> str:
    def _pad(m: re.Match) -> str:
        width = m.group(1)
        expr = m.group(2).strip()
        return f"lpad(({expr})::text, {width}, '0')"

    return _PRINTF_PAD.sub(_pad, sql)


_DATE_NOW_MODS = re.compile(
    r"date\s*\(\s*['\"]now['\"]\s*"
    r"(?:,\s*['\"]localtime['\"])?"
    r"((?:\s*,\s*['\"][+\-]?\d+\s+days?['\"])*)"
    r"\s*\)",
    re.IGNORECASE,
)
_DATETIME_NOW_RUNTIME = re.compile(
    r"datetime\s*\(\s*['\"]now['\"]\s*(?:,\s*['\"]localtime['\"])?\s*\)",
    re.IGNORECASE,
)
_GROUP_CONCAT_2 = re.compile(
    r"GROUP_CONCAT\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
    re.IGNORECASE,
)
_GROUP_CONCAT_1 = re.compile(
    r"GROUP_CONCAT\s*\(\s*([^)]+?)\s*\)",
    re.IGNORECASE,
)


def _rewrite_date_now_forms(sql: str) -> str:
    """SQLite date('now', …) → text ISO YYYY-MM-DD (cột ngày trong app lưu TEXT).

    Dùng text thay vì CURRENT_DATE để tránh ``text >= date`` / nhân đôi ``?``.
    """

    def _repl(m: re.Match) -> str:
        extras = m.group(1) or ''
        days = 0
        for em in re.finditer(r"['\"]([+\-]?\d+)\s+days?['\"]", extras, re.I):
            days += int(em.group(1))
        if days == 0:
            return "TO_CHAR(CURRENT_DATE, 'YYYY-MM-DD')"
        return f"TO_CHAR(CURRENT_DATE + INTERVAL '{days} days', 'YYYY-MM-DD')"

    return _DATE_NOW_MODS.sub(_repl, sql)


def _rewrite_date_now_bound_param(sql: str) -> str:
    """date('now'[, 'localtime'], ?) → CURRENT_DATE + CAST(? AS interval)."""
    return re.sub(
        r"date\s*\(\s*['\"]now['\"]\s*(?:,\s*['\"]localtime['\"])?\s*,\s*\?\s*\)",
        "TO_CHAR(CURRENT_DATE + CAST(? AS interval), 'YYYY-MM-DD')",
        sql,
        flags=re.IGNORECASE,
    )


# date(?, '-1 day') / date(col, '+7 days') — modifier cố định (không phải ? động)
_DATE_EXPR_DAY_MOD = re.compile(
    r"date\s*\(\s*([^,]+?)\s*,\s*['\"]([+\-]?\d+)\s+days?['\"]\s*\)",
    re.IGNORECASE,
)


def _rewrite_date_expr_day_mod(sql: str) -> str:
    """date(expr, '±N day') → TO_CHAR((CAST(expr AS date) + INTERVAL), …)."""

    def _repl(m: re.Match) -> str:
        expr = m.group(1).strip()
        days = int(m.group(2))
        # Placeholder ? giữ nguyên — CAST(? AS date) hợp lệ trên PG sau adapt
        return (
            f"TO_CHAR((CAST(({expr}) AS date) + INTERVAL '{days} days'), 'YYYY-MM-DD')"
        )

    return _DATE_EXPR_DAY_MOD.sub(_repl, sql)


def _extract_fn_arg(sql: str, open_paren_idx: int) -> tuple[str, int] | None:
    """Từ vị trí '(' trả (inner, index_sau_')')."""
    n = len(sql)
    if open_paren_idx >= n or sql[open_paren_idx] != '(':
        return None
    depth = 1
    j = open_paren_idx + 1
    while j < n and depth:
        ch = sql[j]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        j += 1
    if depth != 0:
        return None
    return sql[open_paren_idx + 1:j - 1].strip(), j


def _rewrite_simple_trim(sql: str) -> str:
    """TRIM(x) / BTRIM(x) 1-arg → BTRIM(CAST(x AS text)) — tránh btrim(date)."""
    out: list[str] = []
    i = 0
    n = len(sql)
    lower = sql.lower()
    while i < n:
        # Tìm TRIM( hoặc BTRIM( (không phải FROM-form)
        m_trim = None
        for name in ('btrim(', 'trim('):
            idx = lower.find(name, i)
            if idx < 0:
                continue
            if idx > 0 and (sql[idx - 1].isalnum() or sql[idx - 1] == '_'):
                continue
            if m_trim is None or idx < m_trim[0]:
                m_trim = (idx, len(name) - 1)  # len without '('
        if m_trim is None:
            out.append(sql[i:])
            break
        idx, namelen = m_trim
        out.append(sql[i:idx])
        extracted = _extract_fn_arg(sql, idx + namelen)
        if extracted is None:
            out.append(sql[idx:idx + namelen + 1])
            i = idx + namelen + 1
            continue
        inner, end_j = extracted
        up = inner.upper().lstrip()
        # TRIM(BOTH … FROM …) / nhiều arg — bỏ qua
        if ',' in inner or up.startswith('BOTH') or up.startswith('LEADING') or up.startswith('TRAILING'):
            out.append(sql[idx:end_j])
            i = end_j
            continue
        # Đã CAST AS text
        if ' AS TEXT' in up or ' AS VARCHAR' in up:
            out.append(f'BTRIM({inner})')
        else:
            out.append(f'BTRIM(CAST(({inner}) AS text))')
        i = end_j
    return ''.join(out)


def _rewrite_coalesce_nullif_trim(sql: str) -> str:
    """COALESCE(NULLIF(BTRIM(CAST((a) AS text)), ''), b) → ép b sang text (tránh text/date).

    Chỉ xử lý định danh đơn giản ``a``/``b`` (vd. ``si.invoice_date``, ``si.date``).
    """
    return re.sub(
        r"COALESCE\s*\(\s*NULLIF\s*\(\s*BTRIM\s*\(\s*CAST\s*\(\s*\(([\w.]+)\)\s*AS\s*text\s*\)\s*\)\s*,\s*''\s*\)\s*,\s*"
        r"(?!CAST\s*\()([\w.]+)\s*\)",
        r"COALESCE(NULLIF(BTRIM(CAST((\1) AS text)), ''), CAST((\2) AS text))",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_date_fn_calls(sql: str) -> str:
    """date(expr) → LEFT(CAST(text), 10) — một lần expr (không nhân đôi placeholder ?).

    Không dùng BTRIM bọc ngoài (tránh lỗi kiểu); TRIM bên trong đã ép text.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    lower = sql.lower()
    while i < n:
        idx = lower.find('date(', i)
        if idx < 0:
            out.append(sql[i:])
            break
        if idx > 0 and (sql[idx - 1].isalnum() or sql[idx - 1] == '_'):
            out.append(sql[i:idx + 5])
            i = idx + 5
            continue
        out.append(sql[i:idx])
        extracted = _extract_fn_arg(sql, idx + 4)
        if extracted is None:
            out.append(sql[idx:idx + 5])
            i = idx + 5
            continue
        inner, j = extracted
        up = inner.upper()
        # Đã rewrite date('now') / date(expr, '±N day') → TO_CHAR(...)
        if up.startswith('TO_CHAR(') or up.startswith('CURRENT_DATE'):
            out.append(inner)
        else:
            # Không split theo ',' — COALESCE/NULLIF bên trong cũng có dấu phẩy
            out.append(f"LEFT(CAST(({inner}) AS text), 10)")
        i = j
    return ''.join(out)


def _glob_pattern_to_regex(pat: str) -> str:
    """SQLite GLOB → POSIX regex (neo ^$)."""
    out: list[str] = []
    i = 0
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == '*':
            out.append('.*')
        elif c == '?':
            out.append('.')
        elif c == '[':
            j = i + 1
            if j < n and pat[j] in ('!', '^'):
                j += 1
            if j < n and pat[j] == ']':
                j += 1
            while j < n and pat[j] != ']':
                j += 1
            if j >= n:
                out.append(re.escape(c))
            else:
                chunk = pat[i:j + 1]
                # GLOB [!abc] → regex [^abc]
                if len(chunk) > 2 and chunk[1] == '!':
                    chunk = '[^' + chunk[2:]
                out.append(chunk)
                i = j
        else:
            if c in r'.^$+{}|()\\':
                out.append('\\' + c)
            else:
                out.append(c)
        i += 1
    return '^' + ''.join(out) + '$'


def _rewrite_glob(sql: str) -> str:
    """col GLOB 'pat' → col ~ 'regex' (mọi dạng còn sót)."""

    def _repl_sq(m: re.Match) -> str:
        expr = m.group(1).strip()
        rx = _glob_pattern_to_regex(m.group(2)).replace("'", "''")
        return f"{expr} ~ '{rx}'"

    def _repl_dq(m: re.Match) -> str:
        expr = m.group(1).strip()
        rx = _glob_pattern_to_regex(m.group(2)).replace("'", "''")
        return f"{expr} ~ '{rx}'"

    text = re.sub(
        r"((?:substr|substring)\s*\([^)]+\)|[\w.]+)\s+GLOB\s+'([^']*)'",
        _repl_sq,
        sql,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'((?:substr|substring)\s*\([^)]+\)|[\w.]+)\s+GLOB\s+"([^"]*)"',
        _repl_dq,
        text,
        flags=re.IGNORECASE,
    )
    # Safety net nếu còn GLOB 'lit'
    if re.search(r'\bGLOB\b', text, re.I):
        text = re.sub(
            r"\bGLOB\s+'([^']*)'",
            lambda m: f"~ '{_glob_pattern_to_regex(m.group(1)).replace(chr(39), chr(39) * 2)}'",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r'\bGLOB\s+"([^"]*)"',
            lambda m: f"~ '{_glob_pattern_to_regex(m.group(1)).replace(chr(39), chr(39) * 2)}'",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _rewrite_julianday(sql: str) -> str:
    """julianday(x) → epoch/86400 (so sánh tương đối vẫn đúng)."""
    if 'julianday' not in sql.lower():
        return sql
    out: list[str] = []
    i = 0
    n = len(sql)
    lower = sql.lower()
    while i < n:
        idx = lower.find('julianday(', i)
        if idx < 0:
            out.append(sql[i:])
            break
        if idx > 0 and (sql[idx - 1].isalnum() or sql[idx - 1] == '_'):
            out.append(sql[i:idx + 10])
            i = idx + 10
            continue
        out.append(sql[i:idx])
        extracted = _extract_fn_arg(sql, idx + 9)
        if extracted is None:
            out.append(sql[idx:idx + 10])
            i = idx + 10
            continue
        inner, j = extracted
        low = inner.strip().lower()
        if low.startswith("'now'") or low.startswith('"now"') or low.startswith('now'):
            out.append("(EXTRACT(EPOCH FROM CLOCK_TIMESTAMP()) / 86400.0)")
        else:
            cleaned = inner.strip()
            while True:
                new = re.sub(
                    r"\s*,\s*['\"](?:localtime|[+\-]?\d+\s+days?)['\"]\s*$",
                    '',
                    cleaned,
                    flags=re.I,
                )
                if new == cleaned:
                    break
                cleaned = new
            first = cleaned.strip()
            out.append(
                f"(EXTRACT(EPOCH FROM CAST(NULLIF(BTRIM(CAST(({first}) AS text)), '') "
                f"AS timestamp)) / 86400.0)"
            )
        i = j
    return ''.join(out)


def _rewrite_coalesce_status_int(sql: str) -> str:
    """CAST(COALESCE(status|is_active, 1) AS TEXT) / = 1 → tránh text/int mismatch."""
    text = re.sub(
        r"CAST\s*\(\s*COALESCE\s*\(\s*(status|is_active)\s*,\s*1\s*\)\s*AS\s*TEXT\s*\)",
        r"CAST(COALESCE(CAST(\1 AS text), '1') AS TEXT)",
        sql,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"COALESCE\s*\(\s*(status|is_active)\s*,\s*1\s*\)\s*=\s*1\b",
        r"(CAST(COALESCE(CAST(\1 AS text), '1') AS text) IN ('1', 'true', 'True', '1.0'))",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _rewrite_group_concat(sql: str) -> str:
    text = _GROUP_CONCAT_2.sub(
        lambda m: f"string_agg(({m.group(1).strip()})::text, {m.group(2).strip()})",
        sql,
    )
    text = _GROUP_CONCAT_1.sub(
        lambda m: f"string_agg(({m.group(1).strip()})::text, ',')",
        text,
    )
    return text


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

    # PRAGMA database_list → giả lập 1 DB (schema hiện tại)
    if re.match(r'^\s*PRAGMA\s+database_list\s*$', text, re.I):
        sch = _quote_literal(schema)
        return (
            f"SELECT 0 AS seq, 'main' AS name, {sch} AS file"
        )

    # Mọi PRAGMA còn lại chưa map → no-op (tránh syntax error trên Postgres)
    if text.upper().startswith('PRAGMA'):
        if _PRAGMA_NOOP.match(text):
            return 'SELECT 1 AS _pragma_ok'
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
    m = _SQLITE_MASTER_NAME_EQ_LIT.search(text)
    if m:
        sch = _quote_literal(schema)
        table_name = _quote_literal(m.group(1))
        return (
            f"SELECT table_name AS name FROM information_schema.tables "
            f"WHERE table_schema = {sch} AND table_name = {table_name} LIMIT 1"
        )
    if _SQLITE_MASTER_NAME_EQ_PARAM.search(text):
        sch = _quote_literal(schema)
        return (
            f"SELECT table_name AS name FROM information_schema.tables "
            f"WHERE table_schema = {sch} AND table_name = %s LIMIT 1"
        )
    if _SQLITE_MASTER_LIST.search(text):
        sch = _quote_literal(schema)
        return (
            f"SELECT table_name AS name FROM information_schema.tables "
            f"WHERE table_schema = {sch} AND table_type = 'BASE TABLE' "
            f"AND table_name NOT LIKE 'pg_%' AND table_name NOT LIKE 'sql_%'"
        )

    # sqlite_sequence → no-op (sequence tự quản)
    if (
        _SQLITE_SEQ_DELETE.match(text)
        or _SQLITE_SEQ_INSERT.match(text)
        or _SQLITE_SEQ_UPDATE.match(text)
    ):
        return 'SELECT 1 AS _seq_ok'

    # rowid mirror → no-op trên PostgreSQL
    if _ROWID_UPDATE.search(text):
        return 'SELECT 1 AS _rowid_ok'

    # last_insert_rowid()
    if _LAST_INSERT_ROWID.search(text):
        return 'SELECT lastval() AS last_insert_rowid'

    # SQLite date/datetime TRƯỚC khi đổi ? → %s
    text = _rewrite_date_now_forms(text)
    text = _rewrite_date_now_bound_param(text)
    text = _rewrite_date_expr_day_mod(text)
    text = _DATETIME_NOW_RUNTIME.sub('CURRENT_TIMESTAMP', text)
    text = _DATETIME_NOW.sub('CURRENT_TIMESTAMP', text)
    # TRIM/BTRIM 1-arg + COALESCE(NULLIF(TRIM(a),''), b) → text (tránh btrim(date) / text+date)
    text = _rewrite_simple_trim(text)
    text = _rewrite_coalesce_nullif_trim(text)
    text = _rewrite_date_fn_calls(text)
    text = _rewrite_group_concat(text)
    text = _rewrite_glob(text)
    text = _rewrite_julianday(text)
    text = _rewrite_coalesce_status_int(text)
    # SQLite: "won" có thể là chuỗi; Postgres: "won" = tên cột → lỗi column does not exist
    text = re.sub(r'\bWHEN\s+"([^"]+)"', r"WHEN '\1'", text, flags=re.IGNORECASE)
    text = re.sub(r'\bLIKE\s+"([^"]+)"', r"LIKE '\1'", text, flags=re.IGNORECASE)

    # IFNULL / printf TRƯỚC khi escape % (printf('%06d') → lpad, không còn %)
    text = _rewrite_ifnull(text)
    text = _rewrite_printf(text)
    text = _COLLATE_NOCASE.sub('', text)
    text = _ROWID_COL.sub(r'\1.id', text)
    text = _BARE_ROWID_ORDER.sub('ORDER BY id', text)

    text = _adapt_params(text)

    # CREATE TABLE SQLite DDL
    if _CREATE_TABLE.match(text):
        text = convert_sqlite_ddl(text)

    # INSERT OR REPLACE / IGNORE
    ior = _convert_insert_or_replace(text)
    if ior:
        return ior

    # Fail-loud nếu còn INSERT OR mà chưa map upsert key
    if re.search(r'INSERT\s+OR\s+(?:REPLACE|IGNORE)\s+INTO\b', text, flags=re.IGNORECASE):
        raise ValueError(
            'PostgreSQL: INSERT OR REPLACE/IGNORE chưa map TABLE_UPSERT_KEYS — '
            f'sql={text[:180]!r}'
        )

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

    def __contains__(self, key):
        if isinstance(key, str):
            return key in self._names
        if isinstance(key, int):
            return 0 <= key < len(self._values)
        return False

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
