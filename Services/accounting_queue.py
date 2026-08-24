"""
CQRS Accounting Queue — tách việc ghi sổ kế toán khỏi luồng bán hàng.

Cơ chế: bảng `accounting_jobs` trong tenant DB làm queue.
- Endpoint bán hàng chỉ INSERT job → trả response ngay cho thu ngân.
- Background worker (APScheduler) poll jobs pending → gọi sync_sale_journals().
- Idempotent: mỗi sale_id chỉ có 1 job active (UNIQUE constraint).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BATCH_SIZE = 5  # Giữ batch nhỏ — background worker không giữ khóa SQLite lâu

_SKIP_REASONS = frozenset({
    'not_sme', 'journal_posting_disabled', 'already_posted',
    'sale_not_completed', 'return_import_sale',
})


def ensure_accounting_queue_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounting_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            job_type TEXT NOT NULL DEFAULT 'sale_journal',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            started_at TEXT,
            completed_at TEXT,
            accounting_regime TEXT,
            created_by TEXT,
            replace_existing INTEGER DEFAULT 0,
            features_json TEXT,
            UNIQUE(sale_id, job_type, status)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_acctjob_status
        ON accounting_jobs(status, created_at)
    """)
    if commit:
        from db_utils import begin_immediate, sqlite_write_retry, sqlite_commit

        def _commit_schema():
            begin_immediate(conn, label='acct_queue_schema')
            sqlite_commit(conn, label='accounting_queue')

        sqlite_write_retry(_commit_schema, label='acct_queue_schema')


def enqueue_accounting_job(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    job_type: str = 'sale_journal',
    accounting_regime: str | None = None,
    created_by: str | None = None,
    replace_existing: bool = False,
    features: dict | None = None,
    commit: bool = True,
) -> int | None:
    """
    Thêm job kế toán vào queue. Nếu đã có job pending/processing cho sale_id thì bỏ qua.
    Trả về job_id hoặc None nếu đã tồn tại.
    """
    ensure_accounting_queue_schema(conn, commit=False)

    def _enqueue():
        from db_utils import begin_immediate, sqlite_commit

        begin_immediate(conn, label='enqueue_accounting_job')

        existing = conn.execute(
            "SELECT id FROM accounting_jobs WHERE sale_id = ? AND job_type = ? AND status IN ('pending', 'processing')",
            (sale_id, job_type),
        ).fetchone()
        if existing:
            if replace_existing:
                conn.execute(
                    "UPDATE accounting_jobs SET status = 'cancelled' WHERE id = ?",
                    (existing[0] if not isinstance(existing, sqlite3.Row) else existing['id'],),
                )
            else:
                return None

        features_json = json.dumps(features) if features else None
        cursor = conn.execute(
            """
            INSERT INTO accounting_jobs (sale_id, job_type, status, accounting_regime, created_by, replace_existing, features_json)
            VALUES (?, ?, 'pending', ?, ?, ?, ?)
            """,
            (sale_id, job_type, accounting_regime, created_by, 1 if replace_existing else 0, features_json),
        )
        sqlite_commit(conn, label='accounting_queue')
        return cursor.lastrowid

    if commit:
        from db_utils import sqlite_write_retry
        return sqlite_write_retry(_enqueue, label='enqueue_accounting_job')

    existing = conn.execute(
        "SELECT id FROM accounting_jobs WHERE sale_id = ? AND job_type = ? AND status IN ('pending', 'processing')",
        (sale_id, job_type),
    ).fetchone()
    if existing:
        if replace_existing:
            conn.execute(
                "UPDATE accounting_jobs SET status = 'cancelled' WHERE id = ?",
                (existing[0] if not isinstance(existing, sqlite3.Row) else existing['id'],),
            )
        else:
            return None

    features_json = json.dumps(features) if features else None
    cursor = conn.execute(
        """
        INSERT INTO accounting_jobs (sale_id, job_type, status, accounting_regime, created_by, replace_existing, features_json)
        VALUES (?, ?, 'pending', ?, ?, ?, ?)
        """,
        (sale_id, job_type, accounting_regime, created_by, 1 if replace_existing else 0, features_json),
    )
    return cursor.lastrowid


def get_sale_accounting_status(conn: sqlite3.Connection, sale_id: int) -> dict:
    """Trả về trạng thái kế toán của một sale."""
    ensure_accounting_queue_schema(conn, commit=False)
    row = conn.execute(
        """
        SELECT status, attempts, last_error, completed_at
        FROM accounting_jobs
        WHERE sale_id = ? AND job_type = 'sale_journal'
        ORDER BY id DESC LIMIT 1
        """,
        (sale_id,),
    ).fetchone()
    if not row:
        return {'status': 'none', 'posted': False}
    status = row['status'] if isinstance(row, sqlite3.Row) else row[0]
    error = row['last_error'] if isinstance(row, sqlite3.Row) else row[2]
    completed = row['completed_at'] if isinstance(row, sqlite3.Row) else row[3]
    return {
        'status': status,
        'posted': status == 'completed',
        'error': error if status == 'failed' else None,
        'completed_at': completed,
    }


def _process_one_job(conn: sqlite3.Connection, job: sqlite3.Row) -> str:
    """Xử lý 1 job trong **một** transaction (BEGIN IMMEDIATE → ghi sổ → cập nhật status → COMMIT)."""
    from db_utils import begin_immediate, rollback_quietly, sqlite_commit
    from Services.sme.sale_journal import sync_sale_journals

    job_id = job['id']
    sale_id = job['sale_id']
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    begin_immediate(conn, label='accounting_job')
    conn.execute(
        "UPDATE accounting_jobs SET status = 'processing', started_at = ?, attempts = attempts + 1 WHERE id = ?",
        (now, job_id),
    )

    features = None
    if job['features_json']:
        features = json.loads(job['features_json'])

    result = sync_sale_journals(
        conn,
        sale_id,
        accounting_regime=job['accounting_regime'],
        created_by=job['created_by'],
        replace_existing=bool(job['replace_existing']),
        features=features,
    )

    if result.get('posted') or result.get('reason') in _SKIP_REASONS:
        conn.execute(
            "UPDATE accounting_jobs SET status = 'completed', completed_at = ?, last_error = NULL WHERE id = ?",
            (now, job_id),
        )
        sqlite_commit(conn, label='accounting_queue')
        return 'processed'

    conn.execute(
        "UPDATE accounting_jobs SET status = 'retry', last_error = ? WHERE id = ?",
        (str(result)[:500], job_id),
    )
    sqlite_commit(conn, label='accounting_queue')
    return 'skipped'


def process_accounting_jobs(conn: sqlite3.Connection, *, batch_size: int = BATCH_SIZE) -> dict:
    """
    Xử lý batch jobs pending. Gọi từ background worker.
    Mỗi job = 1 transaction ngắn (không commit rời từng bước).
    """
    from db_utils import _is_locked_error, rollback_quietly, sqlite_write_retry

    ensure_accounting_queue_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row

    jobs = conn.execute(
        """
        SELECT * FROM accounting_jobs
        WHERE status IN ('pending', 'retry')
          AND attempts < ?
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (MAX_ATTEMPTS, batch_size),
    ).fetchall()

    processed = 0
    failed = 0
    skipped = 0

    for job in jobs:
        job_id = job['id']
        sale_id = job['sale_id']

        try:
            def _run():
                return _process_one_job(conn, job)

            outcome = sqlite_write_retry(_run, label=f'accounting_job_{job_id}')
            if outcome == 'processed':
                processed += 1
            else:
                skipped += 1
        except Exception as e:
            rollback_quietly(conn)
            error_msg = str(e)[:500]
            attempts = (job['attempts'] or 0) + 1
            new_status = 'failed' if attempts >= MAX_ATTEMPTS else 'retry'
            try:
                def _fail():
                    from db_utils import begin_immediate
                    begin_immediate(conn, label='accounting_job_fail')
                    conn.execute(
                        "UPDATE accounting_jobs SET status = ?, last_error = ?, attempts = ? WHERE id = ?",
                        (new_status, error_msg, attempts, job_id),
                    )
                    sqlite_commit(conn, label='accounting_queue')

                sqlite_write_retry(_fail, label=f'accounting_job_fail_{job_id}')
            except Exception:
                pass
            failed += 1
            if not _is_locked_error(e):
                logger.warning('accounting_job #%d sale=%d failed: %s', job_id, sale_id, error_msg)

    return {'processed': processed, 'failed': failed, 'skipped': skipped}
