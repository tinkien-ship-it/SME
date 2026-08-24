"""Lịch xuất hóa đơn điện tử tự động — 17:00 hằng ngày, quét từng tenant.

Job của APScheduler chạy trong thread nền, không có request context nên không
thể dựa vào g.db_path/session. Module này chịu trách nhiệm liệt kê đúng các
tenant đã bật lịch và giữ khóa để nhiều worker gunicorn không xuất trùng.
"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db_utils import (
    MAIN_DB_PATH,
    _normalize_db_path,
    db_path_available,
    get_main_db_connection,
    open_sqlite,
    sqlite_commit,
)

logger = logging.getLogger(__name__)

BATCH_INVOICE_JOB_ID = 'batch_invoice_daily'
SCHEDULE_TZ_NAME = 'Asia/Ho_Chi_Minh'
SCHEDULE_TZ = ZoneInfo(SCHEDULE_TZ_NAME)
SCHEDULE_HOUR = 17
SCHEDULE_MINUTE = 0
SCHEDULE_LABEL = '17:00 hằng ngày (giờ Việt Nam)'
LOCK_RETENTION_DAYS = 30

# Main DB là registry của hệ thống, không phải cửa hàng — chỉ quét khi được
# bật tường minh (hữu ích cho môi trường dev chạy trực tiếp trên database.db).
INCLUDE_MAIN_DB = os.getenv('INVOICE_SCHEDULE_INCLUDE_MAIN', '0').strip() in ('1', 'true', 'yes')

# is_active / auto_issue_* có DB lưu dạng TEXT, có DB lưu INTEGER — so cả hai.
_SCHEDULED_CONFIG_SQL = """
    SELECT * FROM invoice_settings
    WHERE COALESCE(is_active, 0) IN (1, '1')
      AND COALESCE(auto_issue_invoice, 0) IN (1, '1')
      AND COALESCE(auto_issue_schedule, 0) IN (1, '1')
    ORDER BY updated_at DESC
    LIMIT 1
"""


def now_vn() -> datetime:
    return datetime.now(SCHEDULE_TZ)


def next_run_at(reference: datetime | None = None) -> datetime:
    """Lần chạy kế tiếp theo giờ Việt Nam."""
    ref = reference or now_vn()
    candidate = ref.replace(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE, second=0, microsecond=0)
    if candidate <= ref:
        candidate += timedelta(days=1)
    return candidate


def current_run_key(reference: datetime | None = None) -> str:
    """Khóa định danh một lượt chạy — mỗi ngày một lượt."""
    return (reference or now_vn()).strftime('%Y-%m-%d')


def describe_schedule() -> dict:
    return {
        'job_id': BATCH_INVOICE_JOB_ID,
        'hour': SCHEDULE_HOUR,
        'minute': SCHEDULE_MINUTE,
        'timezone': SCHEDULE_TZ_NAME,
        'label': SCHEDULE_LABEL,
        'next_run_at': next_run_at().strftime('%Y-%m-%d %H:%M'),
    }


def _ensure_lock_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_runs (
            job_id TEXT NOT NULL,
            run_key TEXT NOT NULL,
            started_at TEXT,
            host TEXT,
            pid INTEGER,
            finished_at TEXT,
            summary TEXT,
            PRIMARY KEY (job_id, run_key)
        )
        """
    )


def claim_job_run(job_id: str = BATCH_INVOICE_JOB_ID, run_key: str | None = None) -> bool:
    """
    Giành quyền chạy một lượt. Chỉ đúng một process thắng nhờ PRIMARY KEY,
    nhờ đó nhiều worker gunicorn không cùng xuất một hóa đơn.
    """
    key = run_key or current_run_key()
    conn = get_main_db_connection()
    try:
        _ensure_lock_schema(conn)
        conn.execute(
            "INSERT INTO scheduler_runs (job_id, run_key, started_at, host, pid) VALUES (?, ?, ?, ?, ?)",
            (job_id, key, now_vn().isoformat(timespec='seconds'), socket.gethostname(), os.getpid()),
        )
        cutoff = (now_vn() - timedelta(days=LOCK_RETENTION_DAYS)).strftime('%Y-%m-%d')
        conn.execute("DELETE FROM scheduler_runs WHERE job_id = ? AND run_key < ?", (job_id, cutoff))
        sqlite_commit(conn, label='invoice_schedule')
        return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.Error as exc:
        logger.warning('claim_job_run(%s, %s): %s', job_id, key, exc)
        return False
    finally:
        conn.close()


def finish_job_run(summary: str, job_id: str = BATCH_INVOICE_JOB_ID, run_key: str | None = None) -> None:
    key = run_key or current_run_key()
    conn = get_main_db_connection()
    try:
        _ensure_lock_schema(conn)
        conn.execute(
            "UPDATE scheduler_runs SET finished_at = ?, summary = ? WHERE job_id = ? AND run_key = ?",
            (now_vn().isoformat(timespec='seconds'), summary[:2000], job_id, key),
        )
        sqlite_commit(conn, label='invoice_schedule')
    except sqlite3.Error as exc:
        logger.warning('finish_job_run(%s, %s): %s', job_id, key, exc)
    finally:
        conn.close()


def last_run_info(job_id: str = BATCH_INVOICE_JOB_ID) -> dict | None:
    conn = get_main_db_connection()
    try:
        _ensure_lock_schema(conn)
        row = conn.execute(
            "SELECT run_key, started_at, finished_at, summary FROM scheduler_runs "
            "WHERE job_id = ? ORDER BY run_key DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def read_scheduled_config(conn: sqlite3.Connection) -> dict | None:
    """Cấu hình HĐĐT của một DB nếu DB đó đã bật xuất theo lịch."""
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(_SCHEDULED_CONFIG_SQL).fetchone()
    except sqlite3.Error as exc:
        logger.warning('read_scheduled_config: %s', exc)
        return None
    if not row:
        return None
    config = dict(row)
    if not config.get('invoice_series'):
        config['invoice_series'] = 'C26MES'
    if not config.get('invoice_type'):
        config['invoice_type'] = '2'
    return config


def _config_for_db(db_path: str) -> dict | None:
    if not db_path_available(db_path):
        return None
    conn = open_sqlite(db_path)
    try:
        return read_scheduled_config(conn)
    finally:
        conn.close()


def iter_auto_invoice_targets() -> list[dict]:
    """Các tenant đang hoạt động và đã bật xuất hóa đơn theo lịch."""
    candidates: list[tuple[str, str]] = []

    conn = get_main_db_connection()
    try:
        rows = conn.execute(
            "SELECT tenant_id, business_name, db_path FROM tenants "
            "WHERE COALESCE(is_active, 0) IN (1, '1')"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.error('iter_auto_invoice_targets: doc bang tenants loi: %s', exc)
        rows = []
    finally:
        conn.close()

    names: dict[str, str] = {}
    for row in rows:
        tenant_id = (row['tenant_id'] or '').strip()
        db_path = _normalize_db_path(row['db_path'])
        if not tenant_id or not db_path:
            continue
        candidates.append((tenant_id, db_path))
        names[tenant_id] = row['business_name'] or tenant_id

    if INCLUDE_MAIN_DB:
        candidates.append(('__main__', MAIN_DB_PATH))
        names['__main__'] = 'Main DB'

    targets = []
    seen = set()
    for tenant_id, db_path in candidates:
        if db_path in seen:
            continue
        seen.add(db_path)
        config = _config_for_db(db_path)
        if not config:
            continue
        targets.append({
            'tenant_id': tenant_id,
            'business_name': names.get(tenant_id, tenant_id),
            'db_path': db_path,
            'config': config,
        })
    return targets


def get_schedule_state(conn: sqlite3.Connection) -> dict:
    """Trạng thái lịch của tenant hiện tại — dùng cho trang Settings."""
    state = {
        'schedule_enabled': False,
        'auto_issue_invoice': False,
        'has_active_provider': False,
        'provider_name': '',
    }
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT provider_name, auto_issue_invoice, auto_issue_schedule FROM invoice_settings "
            "WHERE COALESCE(is_active, 0) IN (1, '1') ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning('get_schedule_state: %s', exc)
        return state
    if not row:
        return state
    state['has_active_provider'] = True
    state['provider_name'] = row['provider_name'] or ''
    state['auto_issue_invoice'] = str(row['auto_issue_invoice'] or '0') in ('1', 'True', 'true')
    state['schedule_enabled'] = str(row['auto_issue_schedule'] or '0') in ('1', 'True', 'true')
    return state


def set_schedule_enabled(conn: sqlite3.Connection, enabled: bool) -> dict:
    """Bật/tắt lịch xuất hóa đơn cho provider đang active của tenant hiện tại."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT provider_name FROM invoice_settings "
        "WHERE COALESCE(is_active, 0) IN (1, '1') ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {
            'success': False,
            'error': 'Chưa có cấu hình hóa đơn điện tử đang hoạt động. Hãy lưu cấu hình nhà cung cấp trước.',
        }

    provider = row['provider_name']
    value = 1 if enabled else 0
    conn.execute(
        "UPDATE invoice_settings SET auto_issue_schedule = ?, updated_at = datetime('now') "
        "WHERE provider_name = ?",
        (value, provider),
    )
    if enabled:
        conn.execute(
            "UPDATE invoice_settings SET auto_issue_schedule = 0 WHERE provider_name != ?",
            (provider,),
        )
    sqlite_commit(conn, label='invoice_schedule')
    return {
        'success': True,
        'schedule_enabled': bool(value),
        'provider_name': provider,
        'schedule': describe_schedule(),
    }
