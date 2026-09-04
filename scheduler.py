"""Lập lịch backup DB, kiểm tra tenant hết hạn, queue kế toán, sync HĐ mua.

Quan trọng (SQLite):
- Chỉ MỘT process được chạy scheduler (file lock) — tránh Gunicorn N worker
  × poll DB liên tục → database is locked trên trang bán hàng / F&B / settings.
- Flask debug reloader: chỉ chạy ở process con (WERKZEUG_RUN_MAIN=true).
- Accounting queue: probe timeout ngắn, bỏ qua DB đang bận; không chờ 60s.
"""
from __future__ import annotations

import atexit
import json
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


# Accounting reconciliation:
# định kỳ tìm completed sale bị thiếu bút toán và đưa lại vào queue.
try:
    _RECONCILE_INTERVAL_SEC = int(
        os.environ.get(
            'SME_ACCOUNTING_RECONCILE_SEC',
            '300',
        ) or 300
    )
except ValueError:
    _RECONCILE_INTERVAL_SEC = 300

try:
    _RECONCILE_BATCH_SIZE = int(
        os.environ.get(
            'SME_ACCOUNTING_RECONCILE_BATCH',
            '100',
        ) or 100
    )
except ValueError:
    _RECONCILE_BATCH_SIZE = 100

_RECONCILE_BATCH_SIZE = max(
    1,
    min(_RECONCILE_BATCH_SIZE, 1000),
)

_reconcile_tick_lock = None


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _scheduler_lock_path() -> str:
    lock_dir = os.path.join(BASE_DIR, 'logs')
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError as exc:
        logger.warning('Không tạo được thư mục logs scheduler: %s', exc)
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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, 'a+b')
    except OSError as exc:
        # Race/missing dir trên VPS — thử tạo lại rồi mở 1 lần nữa
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            fh = open(path, 'a+b')
        except OSError as exc2:
            logger.warning('Không mở được scheduler lock %s: %s', path, exc2 or exc)
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


def _job_accounting_reconcile():
    """Named reconciliation job — chống chồng tick trong cùng process."""
    import threading

    global _reconcile_tick_lock
    if _reconcile_tick_lock is None:
        _reconcile_tick_lock = threading.Lock()

    if not _reconcile_tick_lock.acquire(blocking=False):
        logger.debug('accounting_reconcile: tick trước chưa xong — bỏ qua')
        return

    try:
        _reconcile_missing_sale_accounting()
        _reconcile_sale_revenue_account_integrity()
    finally:
        _reconcile_tick_lock.release()


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


def _job_hrm_compliance():
    """07:45 mỗi ngày: quét tuân thủ LĐ trên từng tenant SME."""
    try:
        from pathlib import Path
        from db_utils import open_sqlite
        from Services.hrm.compliance import scan_compliance

        root = Path(__file__).resolve().parent / 'tenants'
        if not root.is_dir():
            return
        total = 0
        for db in root.glob('*.db'):
            if db.name.lower() in ('registry.db',):
                continue
            try:
                conn = open_sqlite(str(db))
                try:
                    result = scan_compliance(conn)
                    total += int(result.get('count') or 0)
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug('HRM compliance skip %s: %s', db.name, exc)
        logger.info('HRM compliance scan events=%s', total)
    except Exception as exc:
        logger.warning('HRM compliance failed: %s', exc)


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
        func=_scheduled_sme_costing_auto_close,
        trigger="cron",
        day=1,
        hour=1,
        minute=45,
        id='sme_costing_auto_close_monthly',
        name='sme_costing_auto_close_monthly',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    backup_scheduler.add_job(
        func=_scheduled_sme_period_close_catchup,
        trigger="cron",
        hour=2,
        minute=30,
        id='sme_period_close_catchup_daily',
        name='sme_period_close_catchup_daily',
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
        func=_job_accounting_reconcile,
        trigger="interval",
        seconds=max(60, _RECONCILE_INTERVAL_SEC),
        id='accounting_reconcile_worker',
        name='accounting_reconcile_worker',
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
    backup_scheduler.add_job(
        func=_job_hrm_compliance,
        trigger='cron',
        hour=7,
        minute=45,
        id='hrm_compliance_daily',
        name='hrm_compliance_daily',
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    backup_scheduler.start()
    _backup_scheduler = backup_scheduler

    # Chạy reconcile ngay khi scheduler leader khởi động.
    try:
        _job_accounting_reconcile()
    except Exception as exc:
        logger.warning(
            'Initial accounting reconcile failed: %s',
            exc,
            exc_info=True,
        )

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
        'Schedulers started '
        '(pid=%s, accounting_queue=%ss, '
        'accounting_reconcile=%ss, reconcile_batch=%s)',
        os.getpid(),
        max(5, _DEFAULT_QUEUE_SEC),
        max(60, _RECONCILE_INTERVAL_SEC),
        _RECONCILE_BATCH_SIZE,
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



def _load_tenant_accounting_profile(
    db_path: str,
) -> dict:
    """
    Đọc accounting profile của tenant từ registry.

    tenants.settings / tenants.master_settings là JSON.
    Runtime settings ưu tiên hơn master settings.
    """

    profile = {
        'tenant_id': None,
        'accounting_regime': None,
        'features': {},
        'accounting_enabled': False,
        'journal_posting': False,
    }

    try:
        target_abs = os.path.abspath(db_path)

        with open_sqlite(
            REGISTRY_PATH,
            timeout=_QUEUE_PROBE_TIMEOUT_SEC,
        ) as reg:
            reg.row_factory = sqlite3.Row
            rows = reg.execute(
                """
                SELECT
                    tenant_id,
                    db_path,
                    settings,
                    master_settings,
                    is_active
                FROM tenants
                WHERE is_active = 1
                  AND db_path IS NOT NULL
                  AND TRIM(db_path) != ''
                """
            ).fetchall()

        matched = None

        for row in rows:
            raw_path = str(row['db_path'] or '').strip()
            if not raw_path:
                continue

            candidate_abs = os.path.abspath(
                raw_path
                if os.path.isabs(raw_path)
                else os.path.join(BASE_DIR, raw_path)
            )

            if candidate_abs == target_abs:
                matched = row
                break

        if matched is None:
            logger.warning(
                'accounting_profile: không tìm thấy tenant cho DB %s',
                os.path.basename(db_path),
            )
            return profile

        profile['tenant_id'] = str(
            matched['tenant_id'] or ''
        ).strip() or None

        settings = {}
        master_settings = {}

        raw_settings = matched['settings']
        if raw_settings:
            try:
                parsed = json.loads(raw_settings)
                if isinstance(parsed, dict):
                    settings = parsed
            except Exception as exc:
                logger.warning(
                    'accounting_profile [%s]: settings JSON lỗi: %s',
                    profile['tenant_id'],
                    exc,
                )

        raw_master = matched['master_settings']
        if raw_master:
            try:
                parsed = json.loads(raw_master)
                if isinstance(parsed, dict):
                    master_settings = parsed
            except Exception as exc:
                logger.warning(
                    'accounting_profile [%s]: master_settings JSON lỗi: %s',
                    profile['tenant_id'],
                    exc,
                )

        accounting_regime = (
            settings.get('accounting_regime')
            or master_settings.get('accounting_regime')
        )

        features = {}

        master_features = master_settings.get('features')
        if isinstance(master_features, dict):
            features.update(master_features)

        runtime_features = settings.get('features')
        if isinstance(runtime_features, dict):
            features.update(runtime_features)

        profile['accounting_regime'] = (
            str(accounting_regime).strip()
            if accounting_regime
            else None
        )
        profile['features'] = features
        profile['accounting_enabled'] = bool(
            features.get('accounting_enabled')
        )
        profile['journal_posting'] = bool(
            features.get('journal_posting')
        )

        return profile

    except Exception as exc:
        logger.warning(
            'accounting_profile [%s] load failed: %s',
            os.path.basename(db_path),
            exc,
            exc_info=True,
        )
        return profile


def _repair_pending_accounting_job_profile(
    conn,
    *,
    accounting_regime: str | None,
    features: dict | None,
) -> int:
    """
    Đồng bộ profile kế toán hiện tại vào job sale_journal pending/retry.

    Không sửa completed/cancelled/processing.
    """

    if not accounting_regime:
        return 0

    features_json = json.dumps(
        features if isinstance(features, dict) else {},
        ensure_ascii=False,
    )

    cursor = conn.execute(
        """
        UPDATE accounting_jobs
        SET accounting_regime = ?,
            features_json = ?
        WHERE job_type = 'sale_journal'
          AND status IN ('pending', 'retry')
        """,
        (
            accounting_regime,
            features_json,
        ),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def _reconcile_missing_sale_accounting():
    """
    Safety-net: quét tenant SME đã bật Accounting + Journal Posting,
    tìm sale completed chưa có active SALE journal và enqueue lại.
    """

    try:
        tenant_dbs = _collect_tenant_db_paths()
        if not tenant_dbs:
            return

        total_scanned = 0
        total_enqueued = 0
        total_skipped = 0
        total_errors = 0

        for db_path in tenant_dbs:
            try:
                profile = _load_tenant_accounting_profile(db_path)

                tenant_id = (
                    profile.get('tenant_id')
                    or os.path.splitext(os.path.basename(db_path))[0]
                )
                accounting_regime = profile.get('accounting_regime')
                features = profile.get('features') or {}
                regime_upper = str(accounting_regime or '').strip().upper()

                if not regime_upper.startswith('SME'):
                    logger.debug(
                        'accounting_reconcile [%s]: skip non-SME regime=%s',
                        tenant_id,
                        accounting_regime,
                    )
                    continue

                if not bool(features.get('accounting_enabled')):
                    logger.debug(
                        'accounting_reconcile [%s]: skip accounting_disabled',
                        tenant_id,
                    )
                    continue

                if not bool(features.get('journal_posting')):
                    logger.debug(
                        'accounting_reconcile [%s]: skip journal_posting_disabled',
                        tenant_id,
                    )
                    continue

                with open_sqlite(
                    db_path,
                    timeout=_QUEUE_PROCESS_TIMEOUT_SEC,
                ) as conn:
                    conn.row_factory = sqlite3.Row

                    from Services.accounting_queue import (
                        reconcile_missing_sale_accounting,
                    )

                    result = reconcile_missing_sale_accounting(
                        conn,
                        batch_size=_RECONCILE_BATCH_SIZE,
                        accounting_regime=accounting_regime,
                        features=features,
                        created_by='scheduler_reconcile',
                    )

                    # -------------------------------------------------
                    # Safety-net chứng từ bán hàng.
                    #
                    # Chạy độc lập với Accounting Journal:
                    # - 111: đảm bảo Phiếu Thu tiền mặt
                    # - 112: đảm bảo Phiếu Thu chuyển khoản
                    # - 131: đảm bảo Công nợ
                    #
                    # Không phụ thuộc việc sale đã có journal hay chưa.
                    # -------------------------------------------------
                    from Services.sme.sale_financial_documents import (
                        reconcile_completed_sale_documents,
                    )

                    doc_result = reconcile_completed_sale_documents(
                        conn,
                        batch_size=_RECONCILE_BATCH_SIZE,
                        created_by='scheduler_reconcile',
                    )

                    doc_scanned = int(
                        doc_result.get('scanned') or 0
                    )
                    doc_created = int(
                        doc_result.get('created') or 0
                    )
                    doc_existing = int(
                        doc_result.get('existing') or 0
                    )
                    doc_errors = int(
                        doc_result.get('errors') or 0
                    )

                    logger.info(
                        'sale_documents [%s]: '
                        'scanned=%d created=%d '
                        'existing=%d errors=%d',
                        tenant_id,
                        doc_scanned,
                        doc_created,
                        doc_existing,
                        doc_errors,
                    )

                    if doc_errors:
                        logger.warning(
                            'sale_documents [%s] details=%s',
                            tenant_id,
                            doc_result.get('details'),
                        )

                    scanned = int(result.get('scanned') or 0)
                    enqueued = int(result.get('enqueued') or 0)
                    skipped = int(result.get('skipped') or 0)
                    errors = int(result.get('errors') or 0)
                    reason = result.get('reason')

                    total_scanned += scanned
                    total_enqueued += enqueued
                    total_skipped += skipped
                    total_errors += errors

                    logger.info(
                        'accounting_reconcile [%s]: '
                        'regime=%s scanned=%d enqueued=%d '
                        'skipped=%d errors=%d reason=%s',
                        tenant_id,
                        accounting_regime,
                        scanned,
                        enqueued,
                        skipped,
                        errors,
                        reason,
                    )

            except OPERATIONAL_ERROR as exc:
                msg = str(exc).lower()

                if (
                    'locked' in msg
                    or 'busy' in msg
                    or 'timeout' in msg
                ):
                    logger.debug(
                        'accounting_reconcile skip busy %s',
                        os.path.basename(db_path),
                    )
                    continue

                logger.warning(
                    'accounting_reconcile [%s] DB error: %s',
                    os.path.basename(db_path),
                    exc,
                )

            except Exception as exc:
                logger.warning(
                    'accounting_reconcile [%s] skip: %s',
                    os.path.basename(db_path),
                    exc,
                    exc_info=True,
                )

            if _QUEUE_YIELD_SEC > 0:
                time.sleep(_QUEUE_YIELD_SEC)

        logger.info(
            'accounting_reconcile total: '
            'scanned=%d enqueued=%d skipped=%d errors=%d',
            total_scanned,
            total_enqueued,
            total_skipped,
            total_errors,
        )

    except Exception as exc:
        logger.error(
            'accounting_reconcile worker error: %s',
            exc,
            exc_info=True,
        )


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



def _reconcile_sale_revenue_account_integrity():
    """
    Safety-net cho SALE_REVENUE cũ có Có 511.

    Nếu COA hiện tại cho phép hạch toán trực tiếp 511 thì giữ nguyên.
    Nếu loại sản phẩm yêu cầu 5111/5112/5113/5117 đang postable,
    reverse journal cũ và rebuild từ products.product_code.

    Không tự đổi journal lịch sử 511x -> 511 khi người dùng đổi COA.
    """
    try:
        tenant_dbs = _collect_tenant_db_paths()
        if not tenant_dbs:
            return

        total_scanned = total_rebuilt = total_valid_511 = total_errors = 0

        for db_path in tenant_dbs:
            profile = _load_tenant_accounting_profile(db_path)
            tenant_id = (
                profile.get('tenant_id')
                or os.path.splitext(os.path.basename(db_path))[0]
            )
            accounting_regime = profile.get('accounting_regime')
            features = profile.get('features') or {}

            if not str(accounting_regime or '').strip().upper().startswith('SME'):
                continue
            if not bool(features.get('accounting_enabled')):
                continue
            if not bool(features.get('journal_posting')):
                continue

            try:
                with open_sqlite(db_path, timeout=_QUEUE_PROCESS_TIMEOUT_SEC) as conn:
                    conn.row_factory = sqlite3.Row

                    rows = conn.execute(
                        """
                        SELECT DISTINCT e.document_id AS sale_id, e.id AS entry_id
                        FROM sme_journal_entries e
                        JOIN sme_journal_lines l ON l.journal_entry_id = e.id
                        WHERE e.document_type = 'SALE_REVENUE'
                          AND e.status = 'posted'
                          AND e.reverses_id IS NULL
                          AND TRIM(COALESCE(l.account_code, '')) = '511'
                          AND COALESCE(l.credit, 0) > 0
                        ORDER BY e.id
                        LIMIT ?
                        """,
                        (_RECONCILE_BATCH_SIZE,),
                    ).fetchall()

                    from Services.sme.sale_journal import (
                        _build_revenue_lines,
                        sync_sale_journals,
                    )

                    for row in rows:
                        total_scanned += 1
                        sale_id = int(row['sale_id'])
                        entry_id = int(row['entry_id'])

                        try:
                            sale = conn.execute(
                                "SELECT * FROM sale WHERE id = ?",
                                (sale_id,),
                            ).fetchone()
                            if not sale or str(sale['status'] or '').lower() != 'completed':
                                continue

                            _, expected_lines = _build_revenue_lines(conn, sale)
                            expected_accounts = {
                                str(line.get('account_code') or '').strip()
                                for line in expected_lines
                                if float(line.get('credit') or 0) > 0
                                and str(line.get('account_code') or '').strip().startswith('511')
                            }

                            if expected_accounts == {'511'}:
                                total_valid_511 += 1
                                continue

                            result = sync_sale_journals(
                                conn,
                                sale_id,
                                accounting_regime=accounting_regime,
                                created_by='scheduler_integrity',
                                replace_existing=True,
                                features=features,
                            )
                            conn.commit()

                            if result.get('posted'):
                                total_rebuilt += 1
                                logger.warning(
                                    'accounting_integrity [%s]: rebuilt sale_id=%s '
                                    'old_entry=%s expected=%s',
                                    tenant_id, sale_id, entry_id,
                                    sorted(expected_accounts),
                                )

                        except Exception as exc:
                            conn.rollback()
                            total_errors += 1
                            logger.warning(
                                'accounting_integrity [%s]: sale_id=%s error=%s',
                                tenant_id, sale_id, exc, exc_info=True,
                            )

            except OPERATIONAL_ERROR as exc:
                msg = str(exc).lower()
                if 'locked' in msg or 'busy' in msg or 'timeout' in msg:
                    logger.debug('accounting_integrity [%s]: DB busy, skip', tenant_id)
                    continue
                total_errors += 1
                logger.warning(
                    'accounting_integrity [%s] DB error: %s',
                    tenant_id, exc, exc_info=True,
                )
            except Exception as exc:
                total_errors += 1
                logger.warning(
                    'accounting_integrity [%s] error: %s',
                    tenant_id, exc, exc_info=True,
                )

        logger.info(
            'accounting_integrity total: scanned=%d rebuilt=%d valid_511=%d errors=%d',
            total_scanned, total_rebuilt, total_valid_511, total_errors,
        )
    except Exception as exc:
        logger.error('accounting_integrity worker error: %s', exc, exc_info=True)



def _process_accounting_queue(app=None):
    """
    Background worker accounting queue:
    - chỉ xử lý DB có pending/retry;
    - đọc đúng tenant accounting profile từ registry;
    - repair job cũ thiếu regime/features;
    - giữ job nếu tenant chưa bật SME accounting;
    - nhường khóa khi DB bận.
    """

    _ = app  # giữ tương thích chữ ký cũ

    try:
        tenant_dbs = _collect_tenant_db_paths()
        if not tenant_dbs:
            return

        if len(tenant_dbs) > _QUEUE_MAX_DBS_PER_TICK:
            offset = (
                int(time.time() / max(5, _DEFAULT_QUEUE_SEC))
                % len(tenant_dbs)
            )
            rotated = tenant_dbs[offset:] + tenant_dbs[:offset]
        else:
            rotated = tenant_dbs

        processed_dbs = 0

        for db_path in rotated:
            if processed_dbs >= _QUEUE_MAX_DBS_PER_TICK:
                break

            pending = _db_has_pending_accounting_jobs(db_path)

            if pending is None:
                logger.debug(
                    'accounting_queue skip busy %s',
                    os.path.basename(db_path),
                )
                continue

            if not pending:
                continue

            try:
                profile = _load_tenant_accounting_profile(db_path)

                tenant_id = (
                    profile.get('tenant_id')
                    or os.path.splitext(os.path.basename(db_path))[0]
                )
                accounting_regime = profile.get('accounting_regime')
                features = profile.get('features') or {}
                regime_upper = str(accounting_regime or '').strip().upper()

                if not regime_upper.startswith('SME'):
                    logger.warning(
                        'accounting_queue [%s]: pending jobs nhưng '
                        'tenant không phải SME (regime=%s) — giữ job',
                        tenant_id,
                        accounting_regime,
                    )
                    continue

                if not bool(features.get('accounting_enabled')):
                    logger.warning(
                        'accounting_queue [%s]: pending jobs nhưng '
                        'accounting_enabled=false — giữ job',
                        tenant_id,
                    )
                    continue

                if not bool(features.get('journal_posting')):
                    logger.warning(
                        'accounting_queue [%s]: pending jobs nhưng '
                        'journal_posting=false — giữ job',
                        tenant_id,
                    )
                    continue

                with open_sqlite(
                    db_path,
                    timeout=_QUEUE_PROCESS_TIMEOUT_SEC,
                ) as conn:
                    conn.row_factory = sqlite3.Row

                    repaired = _repair_pending_accounting_job_profile(
                        conn,
                        accounting_regime=accounting_regime,
                        features=features,
                    )

                    if repaired:
                        logger.info(
                            'accounting_queue [%s]: '
                            'repaired_profile=%d regime=%s',
                            tenant_id,
                            repaired,
                            accounting_regime,
                        )

                    from Services.accounting_queue import (
                        process_accounting_jobs,
                    )

                    result = process_accounting_jobs(
                        conn,
                        batch_size=_QUEUE_BATCH_SIZE,
                    )

                    if (
                        result.get('processed')
                        or result.get('failed')
                        or result.get('skipped')
                    ):
                        logger.info(
                            'accounting_queue [%s]: '
                            'processed=%d failed=%d skipped=%d',
                            tenant_id,
                            int(result.get('processed') or 0),
                            int(result.get('failed') or 0),
                            int(result.get('skipped') or 0),
                        )

                processed_dbs += 1

            except OPERATIONAL_ERROR as exc:
                msg = str(exc).lower()

                if (
                    'locked' in msg
                    or 'busy' in msg
                    or 'timeout' in msg
                ):
                    logger.debug(
                        'accounting_queue skip locked/busy %s',
                        os.path.basename(db_path),
                    )
                else:
                    logger.warning(
                        'accounting_queue [%s] DB error: %s',
                        os.path.basename(db_path),
                        exc,
                    )

            except Exception as exc:
                logger.warning(
                    'accounting_queue [%s] error: %s',
                    os.path.basename(db_path),
                    exc,
                    exc_info=True,
                )

            if _QUEUE_YIELD_SEC > 0:
                time.sleep(_QUEUE_YIELD_SEC)

    except Exception as exc:
        logger.error(
            'accounting_queue worker error: %s',
            exc,
            exc_info=True,
        )


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


def _scheduled_sme_costing_auto_close():
    """Ngày 1 hàng tháng: chốt giá thành tháng trước (nếu tenant bật auto_close)."""
    try:
        from Services.sme.costing_period_close import run_costing_auto_close_for_all_tenants
        result = run_costing_auto_close_for_all_tenants()
        posted = sum(1 for r in result.get('results') or [] if r.get('posted'))
        print(
            f"[{datetime.now()}] SME costing auto-close {result.get('period')}/{result.get('fiscal_year')}: "
            f"{posted}/{result.get('tenants', 0)} tenants posted"
        )
    except Exception as e:
        print(f"[{datetime.now()}] SME costing auto-close failed: {e}")


def _scheduled_sme_period_close_catchup():
    """Hàng ngày: bù KCKQ các kỳ đã hết nhưng thiếu (server/lịch bỏ sót)."""
    try:
        from Services.sme.auto_posting import run_sme_period_close_catchup_for_all_tenants
        result = run_sme_period_close_catchup_for_all_tenants()
        posted = sum(int(r.get('posted_count') or 0) for r in result.get('results') or [])
        if posted:
            print(
                f"[{datetime.now()}] SME period-close catch-up: "
                f"{posted} KCKQ posted across {result.get('tenants', 0)} tenants"
            )
    except Exception as e:
        print(f"[{datetime.now()}] SME period-close catch-up failed: {e}")


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
