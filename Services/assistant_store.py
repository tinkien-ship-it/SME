"""Lưu trữ trợ lý AI trên main DB — log chat, FAQ động, cấu hình Zalo OA."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from db_utils import BASE_DIR, get_main_db_connection

ASSISTANT_SETTING_KEYS = (
    'assistant_ai_mode',
    'assistant_openai_model',
    'zalo_oa_app_id',
    'zalo_oa_secret',
    'zalo_oa_refresh_token',
    'zalo_oa_access_token',
    'zalo_oa_token_expires',
    'zalo_oa_id',
    'zalo_webhook_verify_token',
    'assistant_escalation_enabled',
)

_SCHEMA_READY = False


def ensure_assistant_schema(conn=None) -> None:
    global _SCHEMA_READY
    own = conn is None
    if conn is None:
        conn = get_main_db_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS assistant_chat_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL DEFAULT 'web',
            tenant_id TEXT,
            username TEXT,
            zalo_user_id TEXT,
            page TEXT,
            user_message TEXT NOT NULL,
            bot_reply TEXT,
            source TEXT,
            confidence REAL DEFAULT 0,
            needs_review INTEGER DEFAULT 0,
            context_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_assist_log_review ON assistant_chat_logs(needs_review, created_at);
        CREATE INDEX IF NOT EXISTS idx_assist_log_tenant ON assistant_chat_logs(tenant_id, created_at);

        CREATE TABLE IF NOT EXISTS assistant_faq_dynamic (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            keywords TEXT,
            pages TEXT,
            source_log_id INTEGER,
            status TEXT DEFAULT 'approved',
            created_by TEXT,
            approved_at TEXT,
            hit_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_assist_faq_status ON assistant_faq_dynamic(status);

        CREATE TABLE IF NOT EXISTS assistant_zalo_sessions (
            zalo_user_id TEXT PRIMARY KEY,
            display_name TEXT,
            last_message_at TEXT,
            message_count INTEGER DEFAULT 0,
            escalated INTEGER DEFAULT 0,
            context_json TEXT
        );

        CREATE TABLE IF NOT EXISTS assistant_health_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            overall_status TEXT,
            score INTEGER DEFAULT 0,
            summary_json TEXT NOT NULL,
            fixes_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_assist_health_created ON assistant_health_runs(created_at DESC);
    """)
    conn.commit()
    if own:
        conn.close()
    _SCHEMA_READY = True


def _get_setting(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return (row['value'] if row and row['value'] is not None else default) or default


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
        (key, str(value)),
    )


def get_assistant_settings() -> dict[str, str]:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    data = {k: _get_setting(conn, k, '') for k in ASSISTANT_SETTING_KEYS}
    data['assistant_escalation_enabled'] = data.get('assistant_escalation_enabled') or '1'
    data['assistant_ai_mode'] = data.get('assistant_ai_mode') or 'free'
    data['assistant_openai_model'] = data.get('assistant_openai_model') or 'gpt-4o-mini'
    conn.close()
    return data


def save_assistant_settings(payload: dict[str, Any]) -> dict[str, str]:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    for key in ASSISTANT_SETTING_KEYS:
        if key not in payload:
            continue
        val = payload.get(key)
        if val is None:
            continue
        if key in ('zalo_oa_secret', 'zalo_oa_refresh_token', 'zalo_oa_access_token') and str(val).strip() == '':
            continue
        _set_setting(conn, key, str(val).strip())
    conn.commit()
    result = get_assistant_settings()
    conn.close()
    return result


def save_zalo_tokens(access_token: str, expires_at: str) -> None:
    conn = get_main_db_connection()
    _set_setting(conn, 'zalo_oa_access_token', access_token)
    _set_setting(conn, 'zalo_oa_token_expires', expires_at)
    conn.commit()
    conn.close()


def log_chat(
    *,
    channel: str,
    user_message: str,
    bot_reply: str,
    source: str,
    confidence: float,
    needs_review: bool,
    tenant_id: str | None = None,
    username: str | None = None,
    zalo_user_id: str | None = None,
    page: str | None = None,
    context: dict | None = None,
) -> int:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    cur = conn.execute(
        """INSERT INTO assistant_chat_logs
           (channel, tenant_id, username, zalo_user_id, page, user_message, bot_reply,
            source, confidence, needs_review, context_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            channel,
            tenant_id,
            username,
            zalo_user_id,
            page,
            user_message,
            bot_reply,
            source,
            confidence,
            1 if needs_review else 0,
            json.dumps(context or {}, ensure_ascii=False),
        ),
    )
    log_id = cur.lastrowid
    conn.commit()
    conn.close()
    return log_id


def list_pending_reviews(*, limit: int = 50) -> list[dict]:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    rows = conn.execute(
        """SELECT id, channel, tenant_id, username, page, user_message, bot_reply,
                  source, confidence, created_at, context_json
           FROM assistant_chat_logs
           WHERE needs_review = 1
             AND id NOT IN (SELECT COALESCE(source_log_id, 0) FROM assistant_faq_dynamic WHERE source_log_id IS NOT NULL)
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_dynamic_faq(*, status: str = 'approved') -> list[dict]:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    rows = conn.execute(
        """SELECT id, question, answer, keywords, pages, hit_count, created_at, created_by
           FROM assistant_faq_dynamic WHERE status = ? ORDER BY id DESC""",
        (status,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_faq_from_log(
    log_id: int,
    *,
    question: str,
    answer: str,
    keywords: list[str] | None = None,
    pages: list[str] | None = None,
    created_by: str = 'master',
) -> dict:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur = conn.execute(
        """INSERT INTO assistant_faq_dynamic
           (question, answer, keywords, pages, source_log_id, status, created_by, approved_at)
           VALUES (?, ?, ?, ?, ?, 'approved', ?, ?)""",
        (
            question.strip(),
            answer.strip(),
            json.dumps(keywords or [], ensure_ascii=False),
            json.dumps(pages or [], ensure_ascii=False),
            log_id,
            created_by,
            now,
        ),
    )
    conn.execute(
        'UPDATE assistant_chat_logs SET needs_review = 0 WHERE id = ?',
        (log_id,),
    )
    conn.commit()
    faq_id = cur.lastrowid
    row = conn.execute('SELECT * FROM assistant_faq_dynamic WHERE id = ?', (faq_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def dismiss_review(log_id: int) -> bool:
    conn = get_main_db_connection()
    cur = conn.execute(
        'UPDATE assistant_chat_logs SET needs_review = 0 WHERE id = ?',
        (log_id,),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def bump_faq_hit(faq_id: int) -> None:
    conn = get_main_db_connection()
    conn.execute(
        'UPDATE assistant_faq_dynamic SET hit_count = hit_count + 1 WHERE id = ?',
        (faq_id,),
    )
    conn.commit()
    conn.close()


def touch_zalo_session(zalo_user_id: str, *, display_name: str | None = None) -> None:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """INSERT INTO assistant_zalo_sessions (zalo_user_id, display_name, last_message_at, message_count)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(zalo_user_id) DO UPDATE SET
             display_name = COALESCE(excluded.display_name, assistant_zalo_sessions.display_name),
             last_message_at = excluded.last_message_at,
             message_count = assistant_zalo_sessions.message_count + 1""",
        (zalo_user_id, display_name or '', now),
    )
    conn.commit()
    conn.close()


def set_zalo_escalated(zalo_user_id: str) -> None:
    conn = get_main_db_connection()
    conn.execute(
        'UPDATE assistant_zalo_sessions SET escalated = 1 WHERE zalo_user_id = ?',
        (zalo_user_id,),
    )
    conn.commit()
    conn.close()


def assistant_stats() -> dict[str, int]:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    total = conn.execute('SELECT COUNT(*) FROM assistant_chat_logs').fetchone()[0]
    pending = conn.execute('SELECT COUNT(*) FROM assistant_chat_logs WHERE needs_review = 1').fetchone()[0]
    faq_dyn = conn.execute(
        "SELECT COUNT(*) FROM assistant_faq_dynamic WHERE status = 'approved'"
    ).fetchone()[0]
    zalo_users = conn.execute('SELECT COUNT(*) FROM assistant_zalo_sessions').fetchone()[0]
    health_runs = 0
    try:
        health_runs = conn.execute('SELECT COUNT(*) FROM assistant_health_runs').fetchone()[0]
    except sqlite3.OperationalError:
        pass
    conn.close()
    return {
        'total_chats': total,
        'pending_review': pending,
        'dynamic_faq': faq_dyn,
        'zalo_users': zalo_users,
        'health_runs': health_runs,
    }


def log_health_run(report: dict[str, Any]) -> int:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    cur = conn.execute(
        """INSERT INTO assistant_health_runs (overall_status, score, summary_json, fixes_json)
           VALUES (?, ?, ?, ?)""",
        (
            report.get('overall') or 'unknown',
            int(report.get('score') or 0),
            json.dumps(report, ensure_ascii=False),
            json.dumps(report.get('fixes_applied') or [], ensure_ascii=False),
        ),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def get_latest_health_report() -> dict[str, Any] | None:
    ensure_assistant_schema()
    conn = get_main_db_connection()
    try:
        row = conn.execute(
            """SELECT id, overall_status, score, summary_json, fixes_json, created_at
               FROM assistant_health_runs ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.OperationalError:
        conn.close()
        return None
    conn.close()
    if not row:
        return None
    data = dict(row)
    try:
        data['report'] = json.loads(data.pop('summary_json') or '{}')
    except json.JSONDecodeError:
        data['report'] = {}
    try:
        data['fixes_applied'] = json.loads(data.get('fixes_json') or '[]')
    except json.JSONDecodeError:
        data['fixes_applied'] = []
    data.pop('fixes_json', None)
    return data
