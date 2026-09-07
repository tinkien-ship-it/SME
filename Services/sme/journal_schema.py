"""Schema sổ nhật ký / bút toán SME — tách biệt HKD và bảng accounting_* cũ."""
from __future__ import annotations

import sqlite3

_SCHEMA_FLAG = 'sme_journal_schema_v4'


def _journal_schema_present(conn: sqlite3.Connection) -> bool:
    from db_utils import sqlite_table_exists

    needed = (
        'sme_journal_entries',
        'sme_journal_lines',
        'sme_posting_rules',
        'sme_account_balances',
        'sme_journal_seed_meta',
    )
    if not all(sqlite_table_exists(conn, name) for name in needed):
        return False
    je_cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_journal_entries)').fetchall()}
    jl_cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_journal_lines)').fetchall()}
    return 'branch_code' in je_cols and 'warehouse_code' in jl_cols


def _apply_journal_schema(conn: sqlite3.Connection) -> None:
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_uuid TEXT NOT NULL UNIQUE,
            entry_no TEXT,
            fiscal_year INTEGER NOT NULL,
            period INTEGER NOT NULL,
            posting_date TEXT NOT NULL,
            document_date TEXT,
            document_type TEXT NOT NULL,
            document_no TEXT,
            document_id INTEGER,
            business_type TEXT,
            currency TEXT NOT NULL DEFAULT 'VND',
            exchange_rate REAL NOT NULL DEFAULT 1,
            description TEXT,
            reference_document TEXT,
            status TEXT NOT NULL DEFAULT 'posted',
            total_debit REAL NOT NULL DEFAULT 0,
            total_credit REAL NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reversed_by_id INTEGER,
            reverses_id INTEGER
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_journal_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL DEFAULT 1,
            account_code TEXT NOT NULL,
            debit REAL NOT NULL DEFAULT 0,
            credit REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'VND',
            exchange_rate REAL NOT NULL DEFAULT 1,
            debit_fc REAL NOT NULL DEFAULT 0,
            credit_fc REAL NOT NULL DEFAULT 0,
            partner_id INTEGER,
            partner_type TEXT,
            warehouse_code TEXT,
            product_id INTEGER,
            employee_id INTEGER,
            project_code TEXT,
            department_code TEXT,
            tax_code TEXT,
            tax_rate REAL,
            vat_invoice_no TEXT,
            description TEXT,
            FOREIGN KEY (entry_id) REFERENCES sme_journal_entries(id),
            FOREIGN KEY (account_code) REFERENCES sme_chart_of_accounts(code)
        )
        """
    )
    c.execute('CREATE INDEX IF NOT EXISTS idx_sme_jl_entry ON sme_journal_lines(entry_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sme_jl_account_date ON sme_journal_lines(account_code)')
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_je_doc ON sme_journal_entries(document_type, document_id)'
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_posting_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_type TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            debit_account_code TEXT NOT NULL,
            credit_account_code TEXT NOT NULL,
            vat_account_code TEXT,
            import_tax_credit_account TEXT,
            is_vat_applicable INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            UNIQUE(business_type, payment_method)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_account_balances (
            account_code TEXT NOT NULL,
            fiscal_year INTEGER NOT NULL,
            period INTEGER NOT NULL,
            opening_debit REAL NOT NULL DEFAULT 0,
            opening_credit REAL NOT NULL DEFAULT 0,
            period_debit REAL NOT NULL DEFAULT 0,
            period_credit REAL NOT NULL DEFAULT 0,
            closing_debit REAL NOT NULL DEFAULT 0,
            closing_credit REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (account_code, fiscal_year, period),
            FOREIGN KEY (account_code) REFERENCES sme_chart_of_accounts(code)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_journal_seed_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    je_cols = {r[1] for r in c.execute('PRAGMA table_info(sme_journal_entries)').fetchall()}
    if 'branch_code' not in je_cols:
        try:
            c.execute('ALTER TABLE sme_journal_entries ADD COLUMN branch_code TEXT')
        except sqlite3.OperationalError:
            pass
    jl_cols = {r[1] for r in c.execute('PRAGMA table_info(sme_journal_lines)').fetchall()}
    if 'warehouse_code' not in jl_cols:
        try:
            c.execute('ALTER TABLE sme_journal_lines ADD COLUMN warehouse_code TEXT')
        except sqlite3.OperationalError:
            pass
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_je_branch
        ON sme_journal_entries(branch_code, fiscal_year, period)
        """
    )


def ensure_sme_journal_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    from Services.sme.description_templates import ensure_description_template_schema
    from db_utils import sqlite_is_ready, sqlite_mark_ready, with_sqlite_write
    from db.schema_helpers import add_column_if_missing

    ensure_description_template_schema(conn, commit=False)

    # Luôn bổ sung cột legacy dù flag đã sẵn (tenant cũ).
    add_column_if_missing(conn, 'sme_journal_entries', 'branch_code', 'TEXT')
    add_column_if_missing(conn, 'sme_journal_lines', 'warehouse_code', 'TEXT')
    add_column_if_missing(conn, 'sme_journal_lines', 'product_id', 'INTEGER')
    add_column_if_missing(conn, 'sme_journal_lines', 'partner_id', 'INTEGER')
    add_column_if_missing(conn, 'sme_journal_lines', 'partner_type', 'TEXT')

    if sqlite_is_ready(conn, _SCHEMA_FLAG):
        return
    try:
        if _journal_schema_present(conn):
            sqlite_mark_ready(conn, _SCHEMA_FLAG)
            return
    except sqlite3.Error:
        pass
    with_sqlite_write(conn, _apply_journal_schema, commit=commit, label='sme_journal_schema')
    sqlite_mark_ready(conn, _SCHEMA_FLAG)
