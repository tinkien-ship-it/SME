"""Schema danh mục tài khoản SME (TT99) — không dùng chung bảng HKD."""
from __future__ import annotations

import sqlite3

_SCHEMA_FLAG = 'sme_coa_schema_v2'


def _coa_schema_present(conn: sqlite3.Connection) -> bool:
    from db_utils import sqlite_table_exists

    if not all(
        sqlite_table_exists(conn, name)
        for name in ('sme_chart_of_accounts', 'sme_coa_seed_meta', 'sme_account_roles')
    ):
        return False
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_chart_of_accounts)').fetchall()}
    return 'is_default_posting' in cols


def _apply_coa_schema(conn: sqlite3.Connection) -> None:
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_chart_of_accounts (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            parent_code TEXT,
            level INTEGER NOT NULL DEFAULT 1,
            account_class TEXT NOT NULL,
            normal_balance TEXT NOT NULL DEFAULT 'debit',
            is_postable INTEGER NOT NULL DEFAULT 1,
            is_system INTEGER NOT NULL DEFAULT 0,
            is_recommended INTEGER NOT NULL DEFAULT 0,
            is_custom INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            legal_source TEXT NOT NULL DEFAULT 'TT99',
            bctc_line_code TEXT,
            track_customer INTEGER NOT NULL DEFAULT 0,
            track_supplier INTEGER NOT NULL DEFAULT 0,
            track_employee INTEGER NOT NULL DEFAULT 0,
            track_bank INTEGER NOT NULL DEFAULT 0,
            track_currency INTEGER NOT NULL DEFAULT 0,
            track_warehouse INTEGER NOT NULL DEFAULT 0,
            track_product INTEGER NOT NULL DEFAULT 0,
            track_project INTEGER NOT NULL DEFAULT 0,
            track_department INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            description TEXT,
            custom_reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (parent_code) REFERENCES sme_chart_of_accounts(code)
        )
        """
    )
    c.execute('CREATE INDEX IF NOT EXISTS idx_sme_coa_parent ON sme_chart_of_accounts(parent_code)')
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_coa_active_level ON sme_chart_of_accounts(is_active, level)'
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_coa_seed_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    coa_cols = {r[1] for r in c.execute('PRAGMA table_info(sme_chart_of_accounts)').fetchall()}
    if 'is_default_posting' not in coa_cols:
        try:
            c.execute(
                'ALTER TABLE sme_chart_of_accounts '
                'ADD COLUMN is_default_posting INTEGER NOT NULL DEFAULT 0'
            )
        except sqlite3.OperationalError:
            pass
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_account_roles (
            role_key TEXT PRIMARY KEY,
            root_hint TEXT NOT NULL,
            default_account TEXT NOT NULL,
            label TEXT NOT NULL,
            description TEXT,
            category TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    c.execute('CREATE INDEX IF NOT EXISTS idx_sme_account_roles_root ON sme_account_roles(root_hint)')
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_coa_default_posting
        ON sme_chart_of_accounts(is_default_posting, is_active)
        """
    )


def ensure_sme_coa_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    from db_utils import sqlite_is_ready, sqlite_mark_ready, with_sqlite_write

    if sqlite_is_ready(conn, _SCHEMA_FLAG):
        return
    try:
        if _coa_schema_present(conn):
            sqlite_mark_ready(conn, _SCHEMA_FLAG)
            return
    except sqlite3.Error:
        pass
    with_sqlite_write(conn, _apply_coa_schema, commit=commit, label='sme_coa_schema')
    sqlite_mark_ready(conn, _SCHEMA_FLAG)
