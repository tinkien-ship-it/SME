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
from db.dialect import table_exists
from Services.sme.journal_schema import ensure_sme_journal_schema

from db_utils import (
    begin_immediate,
    rollback_quietly,
    sqlite_commit,
    sqlite_is_ready,
    sqlite_mark_ready,
    sqlite_write_retry,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BATCH_SIZE = 5  # Giữ batch nhỏ — background worker không giữ khóa SQLite lâu
_ACCT_QUEUE_FLAG = 'accounting_queue_schema_v1'

_SKIP_REASONS = frozenset({
    'not_sme', 'journal_posting_disabled', 'already_posted',
    'sale_not_completed', 'return_import_sale',
})


def ensure_accounting_queue_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    from db.dialect import is_postgres
    from db.schema_helpers import add_column_if_missing, table_exists

    if not is_postgres() and sqlite_is_ready(conn, _ACCT_QUEUE_FLAG):
        return

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
    if table_exists(conn, 'accounting_jobs'):
        for col, typ in (
            ('accounting_regime', 'TEXT'),
            ('created_by', 'TEXT'),
            ('replace_existing', 'INTEGER DEFAULT 0'),
            ('features_json', 'TEXT'),
            ('started_at', 'TEXT'),
            ('completed_at', 'TEXT'),
            ('last_error', 'TEXT'),
            ('attempts', 'INTEGER DEFAULT 0'),
            ('created_at', "TEXT DEFAULT (datetime('now','localtime'))"),
            ('status', "TEXT DEFAULT 'pending'"),
            ('job_type', "TEXT DEFAULT 'sale_journal'"),
        ):
            add_column_if_missing(conn, 'accounting_jobs', col, typ)
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_acctjob_status
            ON accounting_jobs(status, created_at)
        """)
    except Exception:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_acctjob_status
            ON accounting_jobs(status)
        """)
    if commit:
        def _commit_schema():
            begin_immediate(conn, label='acct_queue_schema', retries=1)
            sqlite_commit(conn, label='accounting_queue')

        sqlite_write_retry(_commit_schema, label='acct_queue_schema')
    if not is_postgres():
        sqlite_mark_ready(conn, _ACCT_QUEUE_FLAG)


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

        # Đã ghi sổ xong trước đó → không tạo job mới trừ khi replace
        done = conn.execute(
            """
            SELECT id FROM accounting_jobs
            WHERE sale_id = ? AND job_type = ? AND status = 'completed'
            ORDER BY id DESC LIMIT 1
            """,
            (sale_id, job_type),
        ).fetchone()
        if done and not replace_existing:
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
        return sqlite_write_retry(
            _enqueue,
            label='enqueue_accounting_job',
        )

    # ---------------------------------------------------------
    # commit=False:
    # Caller đang quản lý transaction.
    # Logic phải giống hoàn toàn nhánh commit=True.
    # ---------------------------------------------------------
    existing = conn.execute(
        """
        SELECT id
        FROM accounting_jobs
        WHERE sale_id = ?
          AND job_type = ?
          AND status IN ('pending', 'processing')
        """,
        (sale_id, job_type),
    ).fetchone()

    if existing:
        existing_id = (
            existing['id']
            if hasattr(existing, 'keys')
            else existing[0]
        )

        if replace_existing:
            conn.execute(
                """
                UPDATE accounting_jobs
                SET status = 'cancelled'
                WHERE id = ?
                """,
                (existing_id,),
            )
        else:
            return None

    # Giống nhánh commit=True:
    # completed job cũ thì không tạo lại,
    # trừ khi caller chủ động replace.
    done = conn.execute(
        """
        SELECT id
        FROM accounting_jobs
        WHERE sale_id = ?
          AND job_type = ?
          AND status = 'completed'
        ORDER BY id DESC
        LIMIT 1
        """,
        (sale_id, job_type),
    ).fetchone()

    if done and not replace_existing:
        return None

    features_json = json.dumps(features) if features else None

    cursor = conn.execute(
        """
        INSERT INTO accounting_jobs
        (
            sale_id,
            job_type,
            status,
            accounting_regime,
            created_by,
            replace_existing,
            features_json
        )
        VALUES (?, ?, 'pending', ?, ?, ?, ?)
        """,
        (
            sale_id,
            job_type,
            accounting_regime,
            created_by,
            1 if replace_existing else 0,
            features_json,
        ),
    )

    return cursor.lastrowid

def reconcile_missing_sale_accounting(
    conn,
    *,
    batch_size: int = 100,
    accounting_regime: str | None = None,
    features: dict | None = None,
    created_by: str = 'pos_worker_reconcile',
) -> dict:
    """
    Safety-net cho Accounting Queue.

    Tìm các sale:
      - status = completed
      - chưa có bút toán SALE_REVENUE đang posted
      - chưa có job pending / processing / retry

    SALE_COGS không được dùng làm dấu hiệu "đã hạch toán xong", vì có thể
    xảy ra trạng thái dở dang: SALE_COGS đã có nhưng SALE_REVENUE bị lỗi.

    Sau đó enqueue sale_journal để worker xử lý.

    Journal thực tế là nguồn xác nhận cuối cùng, không phải trạng thái
    completed của accounting_jobs.

    replace_existing=True chỉ được dùng sau khi đã xác nhận sale
    không có active journal, nhờ đó có thể phục hồi trường hợp:
        accounting_jobs = completed
        nhưng journal thực tế bị thiếu.
    """

    ensure_accounting_queue_schema(conn, commit=False)

    try:
        batch_size = int(batch_size or 100)
    except (TypeError, ValueError):
        batch_size = 100

    batch_size = max(1, min(batch_size, 1000))

    # ---------------------------------------------------------
    # 1. Kiểm tra schema.
    # ---------------------------------------------------------
    if not table_exists(conn, 'sale'):
        return {
            'success': True,
            'scanned': 0,
            'enqueued': 0,
            'skipped': 0,
            'errors': 0,
            'reason': 'sale_table_missing',
        }

    try:
        from Services.sme.journal_schema import (
            ensure_sme_journal_schema,
        )

        ensure_sme_journal_schema(
            conn,
            commit=True,
        )

    except Exception as exc:
        logging.exception(
            'Không thể khởi tạo SME journal schema: %s',
            exc,
        )

        return {
            'success': False,
            'scanned': 0,
            'enqueued': 0,
            'skipped': 0,
            'errors': 1,
            'reason': 'sme_journal_schema_init_failed',
            'error': str(exc),
        }

    # ---------------------------------------------------------
    # 2. Tìm completed sale chưa thực sự được ghi sổ.
    #
    # SALE_REVENUE là bút toán bắt buộc để xác nhận sale đã được
    # hạch toán doanh thu. Không được coi SALE_COGS đơn lẻ là hoàn tất,
    # vì sale có thể đã ghi giá vốn nhưng bị lỗi ở bước doanh thu.
    # SALE_COGS không bắt buộc vì sale dịch vụ có thể không phát sinh COGS.
    #
    # Đồng thời bỏ qua sale đang có job sống để không phá job
    # worker đang xử lý.
    # ---------------------------------------------------------
    rows = conn.execute(
        """
        SELECT s.id
        FROM sale s
        WHERE LOWER(TRIM(COALESCE(s.status, ''))) = 'completed'

          AND NOT EXISTS (
              SELECT 1
              FROM sme_journal_entries je
              WHERE je.document_id = s.id
                AND je.document_type = 'SALE_REVENUE'
                AND LOWER(TRIM(COALESCE(je.status, ''))) = 'posted'
                AND je.reverses_id IS NULL
          )

          AND NOT EXISTS (
              SELECT 1
              FROM accounting_jobs aj
              WHERE aj.sale_id = s.id
                AND aj.job_type = 'sale_journal'
                AND aj.status IN ('pending', 'processing', 'retry')
          )

        ORDER BY s.id ASC
        LIMIT ?
        """,
        (batch_size,),
    ).fetchall()

    scanned = len(rows)
    enqueued = 0
    skipped = 0
    error_count = 0
    error_details = []

    # ---------------------------------------------------------
    # 3. Enqueue từng sale bị thiếu.
    # ---------------------------------------------------------
    for row in rows:
        try:
            sale_id = int(
                row['id']
                if hasattr(row, 'keys')
                else row[0]
            )

            job_id = enqueue_accounting_job(
                conn,
                sale_id,
                job_type='sale_journal',
                accounting_regime=accounting_regime,
                created_by=created_by,

                # Query phía trên đã xác nhận không có active
                # journal. Cho phép vượt qua completed job cũ.
                replace_existing=True,

                features=features,
                commit=False,
            )

            if job_id is not None:
                enqueued += 1
            else:
                skipped += 1

        except Exception as exc:
            error_count += 1

            if len(error_details) < 20:
                error_details.append({
                    'sale_id': (
                        row['id']
                        if hasattr(row, 'keys')
                        else row[0]
                    ),
                    'error': str(exc),
                })

            logging.exception(
                'Accounting reconcile enqueue failed sale_id=%s',
                (
                    row['id']
                    if hasattr(row, 'keys')
                    else row[0]
                ),
            )

    # Commit toàn bộ batch một lần.
    conn.commit()

    return {
        'success': True,
        'scanned': scanned,
        'enqueued': enqueued,
        'skipped': skipped,
        'errors': error_count,
        'error_details': error_details,
    }

def ensure_sale_accounting_posted(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    accounting_regime: str | None = None,
    features: dict | None = None,
    created_by: str | None = None,
    replace_existing: bool = False,
    sync_now: bool = True,
) -> dict:
    """Đảm bảo đơn completed có job kế toán; mặc định ghi sổ ngay (không chờ scheduler).

    Dùng sau checkout / đồng bộ offline / dedupe client_uuid — tránh đơn đã lưu
    mà chưa có bút toán vì enqueue lỗi hoặc worker chưa chạy.
    """
    out: dict = {'sale_id': sale_id, 'enqueued': False, 'posted': False}
    try:
        job_id = enqueue_accounting_job(
            conn,
            sale_id,
            accounting_regime=accounting_regime,
            features=features,
            created_by=created_by,
            replace_existing=replace_existing,
            commit=True,
        )
        out['enqueued'] = job_id is not None
        out['job_id'] = job_id
    except Exception as exc:
        logger.warning('enqueue_accounting_job sale %s: %s', sale_id, exc)
        out['enqueue_error'] = str(exc)

    if not sync_now:
        return out

    try:
        from Services.sme.sale_journal import sync_sale_journals

        begin_immediate(conn, label='ensure_sale_acct')
        result = sync_sale_journals(
            conn,
            sale_id,
            accounting_regime=accounting_regime,
            created_by=created_by,
            replace_existing=replace_existing,
            features=features,
        )
        out.update(result or {})
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ensure_accounting_queue_schema(conn, commit=False)
        if result.get('posted') or result.get('reason') in _SKIP_REASONS:
            conn.execute(
                """
                UPDATE accounting_jobs
                SET status = 'completed', completed_at = ?, last_error = NULL
                WHERE sale_id = ? AND job_type = 'sale_journal'
                  AND status IN ('pending', 'processing', 'retry')
                """,
                (now, sale_id),
            )
        elif result.get('error') or result.get('reason'):
            conn.execute(
                """
                UPDATE accounting_jobs
                SET status = 'retry', last_error = ?
                WHERE sale_id = ? AND job_type = 'sale_journal'
                  AND status IN ('pending', 'processing')
                """,
                (str(result.get('error') or result.get('reason') or '')[:500], sale_id),
            )
        sqlite_commit(conn, label='ensure_sale_acct')
    except Exception as exc:
        rollback_quietly(conn)
        logger.warning('ensure_sale_accounting_posted sale %s: %s', sale_id, exc, exc_info=True)
        out['posted'] = False
        out['error'] = str(exc)
        try:
            ensure_accounting_queue_schema(conn, commit=False)
            begin_immediate(conn, label='ensure_sale_acct_err')
            conn.execute(
                """
                UPDATE accounting_jobs
                SET status = 'retry', last_error = ?
                WHERE sale_id = ? AND job_type = 'sale_journal'
                  AND status IN ('pending', 'processing')
                """,
                (str(exc)[:500], sale_id),
            )
            sqlite_commit(conn, label='ensure_sale_acct_err')
        except Exception as fail_exc:
            rollback_quietly(conn)
            logger.warning(
                'ensure_sale_accounting_posted mark-retry sale %s: %s',
                sale_id, fail_exc, exc_info=True,
            )
    return out


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
    from db_utils import _is_locked_error

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
                    begin_immediate(conn, label='accounting_job_fail')
                    conn.execute(
                        "UPDATE accounting_jobs SET status = ?, last_error = ?, attempts = ? WHERE id = ?",
                        (new_status, error_msg, attempts, job_id),
                    )
                    sqlite_commit(conn, label='accounting_queue')

                sqlite_write_retry(_fail, label=f'accounting_job_fail_{job_id}')
            except Exception as fail_exc:
                logger.warning(
                    'accounting_job_fail persist #%d sale=%d: %s',
                    job_id, sale_id, fail_exc, exc_info=True,
                )
            failed += 1
            if not _is_locked_error(e):
                logger.warning('accounting_job #%d sale=%d failed: %s', job_id, sale_id, error_msg)

    return {'processed': processed, 'failed': failed, 'skipped': skipped}
