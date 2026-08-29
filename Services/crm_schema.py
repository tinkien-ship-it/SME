# -*- coding: utf-8 -*-
"""Schema CRM đầy đủ — vận hành + analytics + helpdesk + loyalty."""
from __future__ import annotations

import sqlite3

from db.schema_helpers import add_column_if_missing, table_exists


_CRM_SCHEMA_FLAG = 'crm_schema_contracts_items_v1'

CONTRACT_EXTRA_COLS = (
    ('subtotal', 'REAL DEFAULT 0'),
    ('tax_amount', 'REAL DEFAULT 0'),
    ('place', 'TEXT'),
    ('payment_method', 'TEXT'),
    ('payment_term', 'TEXT'),
    ('delivery_place', 'TEXT'),
    ('delivery_schedule', 'TEXT'),
    ('shipping_party', 'TEXT'),
    ('warranty_months', 'TEXT'),
    ('quality_notes', 'TEXT'),
    ('packaging_notes', 'TEXT'),
    ('buyer_rep', 'TEXT'),
    ('buyer_title', 'TEXT'),
)

CUSTOMER_CRM_COLS = (
    ('crm_source', 'TEXT'),
    ('crm_owner', 'TEXT'),
    ('crm_segment', "TEXT DEFAULT 'standard'"),
    ('crm_lifecycle', "TEXT DEFAULT 'active'"),
    ('crm_notes', 'TEXT'),
    ('crm_next_contact_at', 'TEXT'),
    ('crm_tags', 'TEXT'),
    ('crm_created_at', 'TEXT'),
    ('crm_updated_at', 'TEXT'),
    ('crm_birthday', 'TEXT'),
    ('crm_member_code', 'TEXT'),
    ('crm_member_tier', "TEXT DEFAULT 'standard'"),
    ('crm_loyalty_points', 'REAL DEFAULT 0'),
    ('crm_csat_score', 'REAL'),
    ('crm_nps_score', 'REAL'),
    ('crm_last_survey_at', 'TEXT'),
)

LEAD_EXTRA_COLS = (
    ('campaign_id', 'INTEGER'),
    ('utm_source', 'TEXT'),
    ('utm_medium', 'TEXT'),
    ('utm_campaign', 'TEXT'),
    ('channel', 'TEXT'),
    ('score', 'INTEGER DEFAULT 0'),
    ('assigned_at', 'TEXT'),
    ('external_id', 'TEXT'),
)

OPP_EXTRA_COLS = (
    ('campaign_id', 'INTEGER'),
)

_DDL = """
CREATE TABLE IF NOT EXISTS crm_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    contact_name TEXT NOT NULL,
    company_name TEXT,
    phone TEXT,
    email TEXT,
    source TEXT,
    status TEXT DEFAULT 'new',
    owner TEXT,
    customer_id INTEGER,
    expected_value REAL DEFAULT 0,
    notes TEXT,
    next_contact_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    converted_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    customer_id INTEGER,
    lead_id INTEGER,
    stage TEXT DEFAULT 'approach',
    amount REAL DEFAULT 0,
    probability REAL DEFAULT 0,
    owner TEXT,
    expected_close_date TEXT,
    notes TEXT,
    lost_reason TEXT,
    sale_id INTEGER,
    quote_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    lead_id INTEGER,
    opportunity_id INTEGER,
    activity_type TEXT DEFAULT 'note',
    subject TEXT,
    content TEXT,
    activity_at TEXT,
    next_contact_at TEXT,
    status TEXT DEFAULT 'done',
    owner TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_no TEXT,
    customer_id INTEGER,
    opportunity_id INTEGER,
    quote_date TEXT,
    valid_until TEXT,
    status TEXT DEFAULT 'draft',
    subtotal REAL DEFAULT 0,
    tax_amount REAL DEFAULT 0,
    total REAL DEFAULT 0,
    notes TEXT,
    owner TEXT,
    sale_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_quote_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id INTEGER NOT NULL,
    product_id INTEGER,
    product_name TEXT,
    unit TEXT,
    qty REAL DEFAULT 1,
    unit_price REAL DEFAULT 0,
    tax_rate REAL DEFAULT 0,
    line_total REAL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS crm_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    channel TEXT,
    status TEXT DEFAULT 'active',
    start_date TEXT,
    end_date TEXT,
    budget REAL DEFAULT 0,
    spend REAL DEFAULT 0,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_type TEXT DEFAULT 'month',
    period_key TEXT NOT NULL,
    owner TEXT,
    target_amount REAL DEFAULT 0,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(period_type, period_key, owner)
);

CREATE TABLE IF NOT EXISTS crm_contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no TEXT,
    customer_id INTEGER,
    quote_id INTEGER,
    opportunity_id INTEGER,
    sale_id INTEGER,
    title TEXT,
    signed_date TEXT,
    start_date TEXT,
    end_date TEXT,
    amount REAL DEFAULT 0,
    subtotal REAL DEFAULT 0,
    tax_amount REAL DEFAULT 0,
    status TEXT DEFAULT 'draft',
    file_path TEXT,
    notes TEXT,
    owner TEXT,
    place TEXT,
    payment_method TEXT,
    payment_term TEXT,
    delivery_place TEXT,
    delivery_schedule TEXT,
    shipping_party TEXT,
    warranty_months TEXT,
    quality_notes TEXT,
    packaging_notes TEXT,
    buyer_rep TEXT,
    buyer_title TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_contract_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    product_id INTEGER,
    product_name TEXT,
    unit TEXT,
    qty REAL DEFAULT 1,
    unit_price REAL DEFAULT 0,
    tax_rate REAL DEFAULT 0,
    line_subtotal REAL DEFAULT 0,
    vat_amount REAL DEFAULT 0,
    line_total REAL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS crm_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no TEXT,
    customer_id INTEGER,
    subject TEXT NOT NULL,
    description TEXT,
    category TEXT DEFAULT 'general',
    priority TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'open',
    assignee TEXT,
    opened_at TEXT,
    first_response_at TEXT,
    resolved_at TEXT,
    closed_at TEXT,
    csat_score REAL,
    notes TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    event_type TEXT DEFAULT 'note',
    content TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_surveys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    survey_type TEXT DEFAULT 'csat',
    score REAL,
    comment TEXT,
    channel TEXT,
    related_ticket_id INTEGER,
    related_sale_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notif_type TEXT DEFAULT 'reminder',
    title TEXT,
    body TEXT,
    owner TEXT,
    customer_id INTEGER,
    lead_id INTEGER,
    opportunity_id INTEGER,
    ticket_id INTEGER,
    due_at TEXT,
    is_read INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_assign_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_owner_index INTEGER DEFAULT -1,
    owners_csv TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS crm_visit_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    visit_session_id TEXT NOT NULL,
    customer_id INTEGER NOT NULL,
    employee_id INTEGER,
    owner TEXT NOT NULL,
    check_type TEXT NOT NULL DEFAULT 'in',
    lat REAL,
    lng REAL,
    accuracy REAL,
    ward TEXT,
    district TEXT,
    province TEXT,
    formatted_address TEXT,
    note TEXT,
    crm_activity_id INTEGER,
    device_info TEXT,
    punched_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crm_inbound_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    source TEXT,
    status TEXT DEFAULT 'ok',
    lead_id INTEGER,
    owner TEXT,
    external_id TEXT,
    contact_name TEXT,
    phone TEXT,
    error TEXT,
    payload_preview TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS crm_email_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    ref_id INTEGER,
    to_email TEXT,
    subject TEXT,
    status TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_crm_leads_status ON crm_leads(status);
CREATE INDEX IF NOT EXISTS idx_crm_leads_customer ON crm_leads(customer_id);
CREATE INDEX IF NOT EXISTS idx_crm_leads_source ON crm_leads(source);
CREATE INDEX IF NOT EXISTS idx_crm_opp_stage ON crm_opportunities(stage);
CREATE INDEX IF NOT EXISTS idx_crm_opp_customer ON crm_opportunities(customer_id);
CREATE INDEX IF NOT EXISTS idx_crm_act_customer ON crm_activities(customer_id);
CREATE INDEX IF NOT EXISTS idx_crm_act_next ON crm_activities(next_contact_at);
CREATE INDEX IF NOT EXISTS idx_crm_quotes_customer ON crm_quotes(customer_id);
CREATE INDEX IF NOT EXISTS idx_crm_quotes_status ON crm_quotes(status);
CREATE INDEX IF NOT EXISTS idx_crm_quote_items_qid ON crm_quote_items(quote_id);
CREATE INDEX IF NOT EXISTS idx_crm_tickets_status ON crm_tickets(status);
CREATE INDEX IF NOT EXISTS idx_crm_tickets_customer ON crm_tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_crm_notif_owner ON crm_notifications(owner, is_read);
CREATE INDEX IF NOT EXISTS idx_crm_campaigns_status ON crm_campaigns(status);
CREATE INDEX IF NOT EXISTS idx_crm_visit_customer ON crm_visit_checkins(customer_id);
CREATE INDEX IF NOT EXISTS idx_crm_visit_owner ON crm_visit_checkins(owner, punched_at);
CREATE INDEX IF NOT EXISTS idx_crm_visit_session ON crm_visit_checkins(visit_session_id);
CREATE INDEX IF NOT EXISTS idx_crm_inbound_logs_created ON crm_inbound_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_crm_inbound_logs_channel ON crm_inbound_logs(channel);
CREATE INDEX IF NOT EXISTS idx_crm_contract_items_cid ON crm_contract_items(contract_id);
CREATE INDEX IF NOT EXISTS idx_crm_contracts_customer ON crm_contracts(customer_id);
CREATE INDEX IF NOT EXISTS idx_crm_email_logs_created ON crm_email_logs(created_at);
"""


def _migrate_stages(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, 'crm_opportunities'):
        return
    mapping = (
        ('lead', 'approach'),
        ('consult', 'consulting'),
        ('quote', 'quoting'),
    )
    for old, new in mapping:
        try:
            conn.execute(
                'UPDATE crm_opportunities SET stage = ? WHERE stage = ?',
                (new, old),
            )
        except sqlite3.Error:
            pass


def ensure_crm_email_logs(conn: sqlite3.Connection) -> None:
    """Tạo bảng log email — không bump schema flag (tránh storm migrate đa worker)."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crm_email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT,
                ref_id INTEGER,
                to_email TEXT,
                subject TEXT,
                status TEXT,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_crm_email_logs_created ON crm_email_logs(created_at)'
        )
    except sqlite3.Error:
        pass


def ensure_crm_schema(conn: sqlite3.Connection, commit: bool = True) -> None:
    from db_utils import (
        is_postgres,
        sqlite_file_write_lock,
        sqlite_is_ready,
        sqlite_mark_ready,
    )

    if not is_postgres() and sqlite_is_ready(conn, _CRM_SCHEMA_FLAG):
        ensure_crm_email_logs(conn)
        return

    def _apply() -> None:
        if not is_postgres() and sqlite_is_ready(conn, _CRM_SCHEMA_FLAG):
            ensure_crm_email_logs(conn)
            return
        conn.executescript(_DDL)
        if table_exists(conn, 'customers'):
            for col, col_type in CUSTOMER_CRM_COLS:
                add_column_if_missing(conn, 'customers', col, col_type)
        if table_exists(conn, 'sale'):
            add_column_if_missing(conn, 'sale', 'customer_id', 'INTEGER')
        if table_exists(conn, 'crm_leads'):
            for col, col_type in LEAD_EXTRA_COLS:
                add_column_if_missing(conn, 'crm_leads', col, col_type)
            try:
                conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_crm_leads_external_id ON crm_leads(external_id)'
                )
            except sqlite3.Error:
                pass
        if table_exists(conn, 'crm_opportunities'):
            for col, col_type in OPP_EXTRA_COLS:
                add_column_if_missing(conn, 'crm_opportunities', col, col_type)
        if table_exists(conn, 'crm_contracts'):
            for col, col_type in CONTRACT_EXTRA_COLS:
                add_column_if_missing(conn, 'crm_contracts', col, col_type)
        ensure_crm_email_logs(conn)
        _migrate_stages(conn)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO crm_assign_state (id, last_owner_index, owners_csv) VALUES (1, -1, '')"
            )
        except sqlite3.Error:
            pass
        if commit:
            conn.commit()
        if not is_postgres():
            sqlite_mark_ready(conn, _CRM_SCHEMA_FLAG)

    if is_postgres():
        _apply()
        return

    # Khóa ngắn — không chờ 45s (gây 504 Nginx). Worker khác đang migrate thì bỏ qua.
    try:
        with sqlite_file_write_lock(conn, timeout=2.0):
            _apply()
    except sqlite3.OperationalError:
        # Đã có schema sẵn hoặc worker khác đang chạy — cố gắng tạo bảng phụ
        try:
            ensure_crm_email_logs(conn)
        except sqlite3.Error:
            pass
