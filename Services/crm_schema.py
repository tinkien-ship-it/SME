# -*- coding: utf-8 -*-
"""Schema CRM đầy đủ — vận hành + analytics + helpdesk + loyalty."""
from __future__ import annotations

import sqlite3

from db.schema_helpers import add_column_if_missing, table_exists


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
    status TEXT DEFAULT 'draft',
    file_path TEXT,
    notes TEXT,
    owner TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
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


def ensure_crm_schema(conn: sqlite3.Connection, commit: bool = True) -> None:
    conn.executescript(_DDL)
    if table_exists(conn, 'customers'):
        for col, col_type in CUSTOMER_CRM_COLS:
            add_column_if_missing(conn, 'customers', col, col_type)
    if table_exists(conn, 'sale'):
        add_column_if_missing(conn, 'sale', 'customer_id', 'INTEGER')
    if table_exists(conn, 'crm_leads'):
        for col, col_type in LEAD_EXTRA_COLS:
            add_column_if_missing(conn, 'crm_leads', col, col_type)
    if table_exists(conn, 'crm_opportunities'):
        for col, col_type in OPP_EXTRA_COLS:
            add_column_if_missing(conn, 'crm_opportunities', col, col_type)
    _migrate_stages(conn)
    # seed assign state row
    try:
        conn.execute(
            "INSERT OR IGNORE INTO crm_assign_state (id, last_owner_index, owners_csv) VALUES (1, -1, '')"
        )
    except sqlite3.Error:
        pass
    if commit:
        conn.commit()
