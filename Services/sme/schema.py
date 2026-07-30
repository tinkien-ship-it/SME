"""Schema danh mục tài khoản SME (TT99) — không dùng chung bảng HKD."""
from __future__ import annotations

import sqlite3


def ensure_sme_coa_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
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
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_coa_parent
        ON sme_chart_of_accounts(parent_code)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_coa_active_level
        ON sme_chart_of_accounts(is_active, level)
        """
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
    if commit:
        conn.commit()
