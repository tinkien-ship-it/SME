"""Lập lịch backup DB, kiểm tra tenant hết hạn, queue kế toán, sync HĐ mua.

Quan trọng (SQLite):
- Chỉ MỘT process được chạy scheduler (file lock) — tránh Gunicorn N worker
  × poll DB liên tục → database is locked trên trang bán hàng / F&B / settings.
- Flask debug reloader: chỉ chạy ở process con (WERKZEUG_RUN_MAIN=true).
- Accounting queue: probe timeout ngắn, bỏ qua DB đang bận; không chờ 60s.
"""
from __future__ import annotations

import atexit
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from db.errors import INTEGRITY_ERROR, OPERATIONAL_ERROR

logger = logging.getLogger(__name__)

from apscheduler.schedulers.background import BackgroundScheduler
from flask_apscheduler import APScheduler

from db_utils import (
    BASE_DIR,
    MAIN_DB_PATH,
    REGISTRY_PATH,
    _raw_sqlite_conn,
    get_main_db_connection,
    open_sqlite,
    sqlite_write_retry,
)

# Giữ file handle lock sống suốt đời process (không để GC đóng → mất lock).
_leader_lock_fh = None
_schedulers_started = False
_expiry_scheduler = None
_backup_scheduler = None
_backup_root_ref = None
_accounting_app_ref = None
_queue_tick_lock = None  # threading.Lock — chặn chồng tick trong cùng process

# Probe nhanh: nếu DB đang phục vụ user thì bỏ qua, không tranh khóa lâu.
_QUEUE_PROBE_TIMEOUT_SEC = float(os.environ.get('SME_ACCT_QUEUE_PROBE_TIMEOUT', '0.25') or 0.25)
_QUEUE_PROCESS_TIMEOUT_SEC = float(os.environ.get('SME_ACCT_QUEUE_PROCESS_TIMEOUT', '8') or 8)
_QUEUE_YIELD_SEC = float(os.environ.get('SME_ACCT_QUEUE_YIELD_SEC', '0.05') or 0.05)
_QUEUE_MAX_DBS_PER_TICK = int(os.environ.get('SME_ACCT_QUEUE_MAX_DBS', '8') or 8)
_QUEUE_BATCH_SIZE = int(os.environ.get('SME_ACCT_QUEUE_BATCH', '5') or 5)

try:
    _DEFAULT_QUEUE_SEC = int(os.environ.get('SME_ACCOUNTING_QUEUE_SEC', '30') or 30)
except ValueError:
    _DEFAULT_QUEUE_SEC = 30


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _scheduler_lock_path() -> str:
    lock_dir = os.path.join(BASE_DIR, 'logs')
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.join(lock_dir, 'scheduler.leader.lock')


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == 'nt':
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lock_pid(path: str) -> int:
    try:
        with open(path, 'rb') as f:
            raw = f.read().decode('utf-8', errors='ignore').strip()
        if not raw or raw == '0':
            return 0
        return int(raw.splitlines()[0])
    except (OSError, ValueError):
        return 0


def cleanup_stale_scheduler_lock() -> bool:
    """Xóa lock scheduler nếu process ghi PID đã chết (sau crash / tắt cửa sổ CMD)."""
    path = _scheduler_lock_path()
    if not os.path.isfile(path):
        return False
    old_pid = _read_lock_pid(path)
    if old_pid <= 0:
        return False
    if old_pid == os.getpid() or _pid_alive(old_pid):
        return False
    try:
        os.remove(path)
        logger.info('Đã xóa scheduler lock cũ (pid=%s không còn chạy)', old_pid)
        return True
    except OSError as exc:
        logger.warning('Không xóa được scheduler lock cũ: %s', exc)
        return False


def try_acquire_scheduler_leadership() -> bool:
    """Chỉ một process trên máy giữ quyền chạy background jobs.

    Windows: kết hợp msvcrt.locking + PID stale check (tránh orphan sau crash).
    """
    global _leader_lock_fh
    if _leader_lock_fh is not None:
        return True

    cleanup_stale_scheduler_lock()
    path = _scheduler_lock_path()
    try:
        fh = open(path, 'a+b')
    except OSError as exc:
        logger.warning('Không mở được scheduler lock %s: %s', path, exc)
        return False

    # Nếu PID cũ còn sống mà ta không lấy được lock → bỏ qua.
    # Nếu PID cũ chết → xóa nội dung, thử khóa lại.
    try:
        fh.seek(0)
        raw = fh.read().decode('utf-8', errors='ignore').strip()
        old_pid = int(raw.splitlines()[0]) if raw else 0
    except (ValueError, IndexError, OSError):
        old_pid = 0

    locked = False
    try:
        if os.name == 'nt':
            import msvcrt

            fh.seek(0)
            data = fh.read(1)
            if not data:
                fh.write(b'0')
                fh.flush()
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                if old_pid and old_pid != os.getpid() and not _pid_alive(old_pid):
                    # Stale lock file — process chết không unlock
                    try:
                        fh.close()
                    except Exception:
                        pass
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    fh = open(path, 'a+b')
                    fh.write(b'0')
                    fh.flush()
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                else:
                    raise
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if old_pid and old_pid != os.getpid() and not _pid_alive(old_pid):
                    try:
                        fh.close()
                    except Exception:
                        pass
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    fh = open(path, 'a+b')
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                else:
                    raise
    except OSError:
        try:
            fh.close()
        except Exception:
            pass
        logger.info(
            'Scheduler bỏ qua process pid=%s — process khác đang giữ leadership (old_pid=%s)',
            os.getpid(),
            old_pid or '?',
        )
        return False

    if not locked:
        try:
            fh.close()
        except Exception:
            pass
        return False

    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f'{os.getpid()}\n'.encode('utf-8'))
        fh.flush()
    except OSError:
        pass

    _leader_lock_fh = fh

    def _release():
        global _leader_lock_fh
        if _leader_lock_fh is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt

                _leader_lock_fh.seek(0)
                msvcrt.locking(_leader_lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(_leader_lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            _leader_lock_fh.close()
        except Exception:
            pass
        _leader_lock_fh = None

    atexit.register(_release)
    logger.info('Scheduler leadership acquired (pid=%s, lock=%s)', os.getpid(), path)
    return True


def should_run_schedulers(app=None) -> bool:
    """Quyết định process này có được phép start scheduler không.

    Không dựa vào app.debug (lúc import thường vẫn False trước app.run).
    """
    if _env_truthy('SME_DISABLE_SCHEDULERS'):
        logger.info('SME_DISABLE_SCHEDULERS=1 — không start scheduler')
        return False

    if _env_truthy('SME_FORCE_SCHEDULERS'):
        return try_acquire_scheduler_leadership()

    run_main = os.environ.get('WERKZEUG_RUN_MAIN')
    # Reloader parent / lần import đầu trước khi fork
    if run_main == 'false':
        return False

    # Khi chạy python app.py + use_reloader: chỉ child có =true mới được gọi init
    # (app.py đã gate). Ở đây vẫn acquire lock cho gunicorn / child.
    return try_acquire_scheduler_leadership()


def _job_backup_database():
    backup_database(_backup_root_ref)


def _job_accounting_queue():
    """Named job — log rõ, chống chồng tick trong cùng process."""
    import threading

    global _queue_tick_lock
    if _queue_tick_lock is None:
        _queue_tick_lock = threading.Lock()
    if not _queue_tick_lock.acquire(blocking=False):
        logger.debug('accounting_queue: tick trước chưa xong — bỏ qua')
        return
    try:
        _process_accounting_queue(_accounting_app_ref)
    finally:
        _queue_tick_lock.release()


def _job_crm_reminders():
    """07:30 mỗi ngày: nhắc liên hệ đến hạn, sinh nhật KH, ticket quá SLA."""
    try:
        from Services.crm_ops import run_reminders_all_tenants
        result = run_reminders_all_tenants()
        logger.info(
            'CRM reminders: scanned=%s created=%s',
            result.get('scanned'), result.get('created'),
        )
    except Exception as exc:
        logger.warning('CRM reminders failed: %s', exc)


def check_tenant_expirations():
    today = datetime.now().strftime('%Y-%m-%d')

    def _write():
        with get_main_db_connection() as conn:
            from db_utils import begin_immediate, sqlite_commit
            begin_immediate(conn, label='check_tenant_expirations')
            conn.execute("""
                UPDATE tenants
                SET is_active = 0
                WHERE expiry_date < ? AND is_active = 1
            """, (today,))
            sqlite_commit(conn, label='check_tenant_expirations')

    sqlite_write_retry(_write, label='check_tenant_expirations')
    print(f"--- Đã kiểm tra và khóa các Tenant hết hạn ngày {today} ---")


def backup_database(backup_root):
    """Quét Registry và backup cho tất cả Tenant + Main DB."""
    try:
        from db.dialect import is_postgres
        if is_postgres():
            # pg_dump toàn DB — không copy file .db
            try:
                out = Path(backup_root) / 'pg'
                rc = subprocess.call([
                    sys.executable,
                    str(Path(BASE_DIR) / 'scripts' / 'pg_dump_backup.py'),
                    '--out', str(out),
                ])
                if rc == 0:
                    print(f"[{datetime.now()}] Backup PostgreSQL OK → {out}")
                else:
                    print(
                        f"[{datetime.now()}] Backup PostgreSQL thất bại (rc={rc}) "
                        "— kiểm tra pg_dump / DATABASE_URL"
                    )
            except Exception as exc:
                print(f"[{datetime.now()}] Backup PostgreSQL lỗi: {exc}")
            return
        if not os.path.exists(backup_root):
            os.makedirs(backup_root, exist_ok=True)

        tasks = [('main', MAIN_DB_PATH)]

        try:
            with get_main_db_connection() as conn_main:
                tenants = conn_main.execute(
                    "SELECT tenant_id, db_path FROM tenants WHERE is_active=1"
                ).fetchall()
        except Exception as db_err:
            print(f"Lỗi truy cập Registry: {db_err}")
            return

        for t_id, t_path in tenants:
            if not t_id or not t_path:
                continue
            t_id = str(t_id).strip()
            abs_path = t_path if os.path.isabs(t_path) else os.path.join(BASE_DIR, t_path)
            tasks.append((t_id, abs_path))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for tenant_id, db_path in tasks:
            try:
                if not os.path.exists(db_path):
                    print(f"Bỏ qua {tenant_id}: File không tồn tại tại {db_path}")
                    continue

                tenant_backup_dir = os.path.join(backup_root, tenant_id)
                os.makedirs(tenant_backup_dir, exist_ok=True)

                filename = f"{tenant_id}_auto_{timestamp}.db"
                dest = os.path.join(tenant_backup_dir, filename)
                # Timeout ngắn: đang bận phục vụ user thì bỏ qua tenant này, không chờ khóa.
                try:
                    with open_sqlite(db_path, timeout=2.0) as src:
                        with open_sqlite(dest, timeout=5.0) as dst:
                            _raw_sqlite_conn(src).backup(_raw_sqlite_conn(dst))
                except OPERATIONAL_ERROR as e:
                    if 'locked' in str(e).lower():
                        print(f"Bỏ qua backup {tenant_id}: database đang bận")
                        continue
                    raise

                cutoff = (datetime.now() - timedelta(days=10)).timestamp()
                for f in os.listdir(tenant_backup_dir):
                    f_path = os.path.join(tenant_backup_dir, f)
                    if os.path.isfile(f_path) and f.endswith('.db'):
                        if os.path.getctime(f_path) < cutoff:
                            os.remove(f_path)

                print(f"[{datetime.now()}] Backup OK: {tenant_id}")

            except Exception as e:
                print(f"Lỗi khi backup tenant {tenant_id}: {e}")
                continue

    except Exception as e:
        print(f"Lỗi hệ thống Backup (Tổng quát): {e}")


def init_schedulers(app, backup_root):
    """Khởi tạo APScheduler một lần / một process leader.

    An toàn gọi nhiều lần (idempotent). Không chiếm khóa SQLite của request HTTP.
    """
    global _schedulers_started, _expiry_scheduler, _backup_scheduler
    global _backup_root_ref, _accounting_app_ref

    if _schedulers_started:
        logger.debug('init_schedulers: already started in this process')
        return _expiry_scheduler, _backup_scheduler

    if not should_run_schedulers(app):
        return None, None

    _schedulers_started = True
    _backup_root_ref = backup_root
    _accounting_app_ref = app

    # Giảm spam log "Running job ..." mỗi 10–30s (đặc biệt khi debug)
    logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)
    logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)

    expiry_scheduler = APScheduler()

    @expiry_scheduler.task('cron', id='do_check_expiry', hour=0, minute=1)
    def scheduled_task():
        with app.app_context():
            check_tenant_expirations()

    expiry_scheduler.init_app(app)
    expiry_scheduler.start()
    _expiry_scheduler = expiry_scheduler

    backup_scheduler = BackgroundScheduler(
        daemon=True,
        job_defaults={
            'coalesce': True,
            'max_instances': 1,
            'misfire_grace_time': 300,
        },
    )
    backup_scheduler.add_job(
        func=_job_backup_database,
        trigger="cron",
        hour=20,
        minute=0,
        id='backup_database_daily',
        name='backup_database_daily',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    backup_scheduler.add_job(
        func=_scheduled_knowledge_rss_sync,
        trigger="cron",
        hour=6,
        minute=30,
        id='knowledge_rss_sync_daily',
        name='knowledge_rss_sync_daily',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    backup_scheduler.add_job(
        func=_scheduled_sme_auto_posting,
        trigger="cron",
        day=1,
        hour=1,
        minute=15,
        id='sme_auto_posting_monthly',
        name='sme_auto_posting_monthly',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    backup_scheduler.add_job(
        func=_scheduled_sme_vat_filing_alert,
        trigger="cron",
        day=1,
        month=1,
        hour=2,
        minute=0,
        id='sme_vat_filing_alert_yearly',
        name='sme_vat_filing_alert_yearly',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    backup_scheduler.add_job(
        func=_scheduled_purchase_invoice_sync,
        trigger="cron",
        hour='0,9,15',
        minute=0,
        id='purchase_invoice_sync_slots',
        name='purchase_invoice_sync_slots',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    backup_scheduler.add_job(
        func=_job_accounting_queue,
        trigger="interval",
        seconds=max(5, _DEFAULT_QUEUE_SEC),
        id='accounting_queue_worker',
        name='accounting_queue_worker',
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    backup_scheduler.add_job(
        func=_job_crm_reminders,
        trigger='cron',
        hour=7,
        minute=30,
        id='crm_reminders_daily',
        name='crm_reminders_daily',
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    backup_scheduler.start()
    _backup_scheduler = backup_scheduler

    # Invoice batch 17:00 — cùng leadership, không start ở mọi worker
    try:
        from routes.invoice import start_invoice_batch_scheduler
        start_invoice_batch_scheduler()
    except Exception as exc:
        logger.warning('Không start invoice batch scheduler: %s', exc)

    def _shutdown():
        try:
            if _backup_scheduler is not None and _backup_scheduler.running:
                _backup_scheduler.shutdown(wait=False)
        except Exception:
            pass

    atexit.register(_shutdown)
    logger.info(
        'Schedulers started (pid=%s, accounting_queue every %ss)',
        os.getpid(),
        max(5, _DEFAULT_QUEUE_SEC),
    )
    return expiry_scheduler, backup_scheduler


def _scheduled_purchase_invoice_sync():
    """09:00, 15:00, 00:00 (VN): đồng bộ HĐ mua qua kênh portal Mắt Bảo."""
    try:
        from Services.purchase_invoice_schedule import run_purchase_sync_for_all_tenants
        result = run_purchase_sync_for_all_tenants()
        if result.get('skipped'):
            print(f"[{datetime.now()}] Purchase invoice sync skipped: {result.get('reason')}")
            return
        print(
            f"[{datetime.now()}] Purchase invoice sync: {result.get('summary')} "
            f"months={result.get('months')}"
        )
    except Exception as e:
        print(f"[{datetime.now()}] Purchase invoice sync failed: {e}")


def _collect_tenant_db_paths() -> list[str]:
    """Danh sách DB tenant (registry + tenants/*.db), không trùng.

    Trên PostgreSQL: trả đường dẫn logic (tenants/<id>.db) để open_sqlite → schema.
    """
    from db.dialect import is_postgres

    seen: set[str] = set()
    out: list[str] = []

    def _add(path: str) -> None:
        if not path:
            return
        if is_postgres():
            # Giữ path logic — không yêu cầu file tồn tại
            key = str(path).replace('\\', '/')
            if key in seen:
                return
            seen.add(key)
            out.append(path if os.path.isabs(path) else os.path.join(BASE_DIR, path))
            return
        full = os.path.abspath(path if os.path.isabs(path) else os.path.join(BASE_DIR, path))
        if full in seen or not os.path.isfile(full):
            return
        seen.add(full)
        out.append(full)

    if is_postgres():
        try:
            with open_sqlite(REGISTRY_PATH, timeout=_QUEUE_PROBE_TIMEOUT_SEC) as reg:
                rows = reg.execute(
                    "SELECT db_path, tenant_id FROM tenants "
                    "WHERE db_path IS NOT NULL AND TRIM(db_path) != ''"
                ).fetchall()
            for row in rows:
                if hasattr(row, 'keys'):
                    p = row['db_path'] if 'db_path' in row.keys() else row[0]
                    tid = row['tenant_id'] if 'tenant_id' in row.keys() else None
                else:
                    p, tid = row[0], (row[1] if len(row) > 1 else None)
                if p:
                    _add(p)
                elif tid:
                    _add(os.path.join(BASE_DIR, 'tenants', f'{tid}.db'))
        except Exception as exc:
            logger.warning('collect tenant schemas (pg): %s', exc)
        return out

    tenants_dir = os.path.join(BASE_DIR, 'tenants')
    if os.path.isdir(tenants_dir):
        for fn in os.listdir(tenants_dir):
            if fn.endswith('.db') and fn != 'registry.db':
                _add(os.path.join(tenants_dir, fn))

    if os.path.isfile(REGISTRY_PATH):
        try:
            with open_sqlite(REGISTRY_PATH, timeout=_QUEUE_PROBE_TIMEOUT_SEC) as reg:
                rows = reg.execute(
                    "SELECT db_path FROM tenants WHERE db_path IS NOT NULL AND TRIM(db_path) != ''"
                ).fetchall()
            for row in rows:
                p = row[0] if not isinstance(row, sqlite3.Row) else row['db_path']
                _add(p)
        except Exception:
            pass

    return out


def _db_has_pending_accounting_jobs(db_path: str) -> bool | None:
    """
    True = có job; False = không; None = không kiểm tra được (locked / lỗi) → bỏ qua.
    Timeout ngắn để không chặn request user.
    """
    try:
        with open_sqlite(db_path, timeout=_QUEUE_PROBE_TIMEOUT_SEC) as conn:
            conn.row_factory = sqlite3.Row
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounting_jobs'"
            ).fetchone()
            if not has_table:
                return False
            pending = conn.execute(
                "SELECT 1 FROM accounting_jobs WHERE status IN ('pending','retry') LIMIT 1"
            ).fetchone()
            return bool(pending)
    except OPERATIONAL_ERROR as e:
        if 'locked' in str(e).lower():
            return None
        return False
    except Exception as e:
        msg = str(e).lower()
        if 'lock' in msg or 'deadlock' in msg or 'timeout' in msg:
            return None
        return False


def _process_accounting_queue(app):
    """Background worker: chỉ xử lý DB có job pending; nhường khóa nếu đang bận."""
    try:
        tenant_dbs = _collect_tenant_db_paths()
        if not tenant_dbs:
            return

        # Round-robin nhẹ theo giây để không luôn đụng cùng vài DB đầu danh sách
        if len(tenant_dbs) > _QUEUE_MAX_DBS_PER_TICK:
            offset = int(time.time() / max(5, _DEFAULT_QUEUE_SEC)) % len(tenant_dbs)
            rotated = tenant_dbs[offset:] + tenant_dbs[:offset]
        else:
            rotated = tenant_dbs

        processed_dbs = 0
        for db_path in rotated:
            if processed_dbs >= _QUEUE_MAX_DBS_PER_TICK:
                break

            pending = _db_has_pending_accounting_jobs(db_path)
            if pending is None:
                logger.debug('accounting_queue skip busy %s', os.path.basename(db_path))
                continue
            if not pending:
                continue

            try:
                with open_sqlite(db_path, timeout=_QUEUE_PROCESS_TIMEOUT_SEC) as conn:
                    conn.row_factory = sqlite3.Row
                    from Services.accounting_queue import process_accounting_jobs
                    result = process_accounting_jobs(conn, batch_size=_QUEUE_BATCH_SIZE)
                    if result.get('processed') or result.get('failed'):
                        logger.info(
                            'accounting_queue [%s]: processed=%d failed=%d',
                            os.path.basename(db_path),
                            result.get('processed', 0),
                            result.get('failed', 0),
                        )
                processed_dbs += 1
            except OPERATIONAL_ERROR as e:
                if 'locked' in str(e).lower():
                    logger.debug('accounting_queue skip locked %s', os.path.basename(db_path))
                else:
                    logger.debug('accounting_queue skip %s: %s', db_path, e)
            except Exception as e:
                logger.debug('accounting_queue skip %s: %s', db_path, e)

            if _QUEUE_YIELD_SEC > 0:
                time.sleep(_QUEUE_YIELD_SEC)

    except Exception as e:
        logger.error('accounting_queue worker error: %s', e)


def _scheduled_sme_auto_posting():
    """Ngày 1 hàng tháng: chạy KH/PB kỳ tháng trước.

    Ngày ghi sổ bút toán = ngày cuối tháng trước (VD chạy 01/09 → ghi 31/08),
    không dùng ngày chạy lịch.
    """
    try:
        from Services.sme.auto_posting import run_sme_automation_for_all_tenants
        result = run_sme_automation_for_all_tenants()
        posted = sum(1 for r in result.get('results') or [] if r.get('posted'))
        print(
            f"[{datetime.now()}] SME auto posting {result.get('period')}/{result.get('fiscal_year')} "
            f"posting_date={result.get('posting_date')}: "
            f"{posted}/{result.get('tenants', 0)} tenants posted"
        )
    except Exception as e:
        print(f"[{datetime.now()}] SME auto posting failed: {e}")


def _scheduled_sme_vat_filing_alert():
    """Ngày 1/1: tự chuyển kỳ kê khai GTGT sang tháng nếu DT năm trước > 50 tỷ mà user chưa đổi."""
    try:
        from Services.sme.vat_filing_alert import run_vat_filing_alerts_for_all_tenants
        result = run_vat_filing_alerts_for_all_tenants()
        applied = sum(1 for r in result.get('results') or [] if r.get('applied'))
        print(
            f"[{datetime.now()}] SME VAT filing alerts: "
            f"{applied} auto-applied / {result.get('tenants', 0)} tenants"
        )
    except Exception as e:
        print(f"[{datetime.now()}] SME VAT filing alerts failed: {e}")


def _scheduled_knowledge_rss_sync():
    """Đồng bộ RSS pháp luật vào hàng chờ nháp — thứ Hai hàng tuần."""
    try:
        from Services.knowledge_service import run_scheduled_rss_sync
        result = run_scheduled_rss_sync()
        print(
            f"[{datetime.now()}] Knowledge RSS sync: "
            f"+{result.get('inserted', 0)} new ({result.get('published', 0)} published), "
            f"skip {result.get('skipped', 0)}"
        )
    except Exception as e:
        print(f"[{datetime.now()}] Knowledge RSS sync failed: {e}")
