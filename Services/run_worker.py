"""
POS System - BACKGROUND WORKER PROCESS (Standalone)
--------------------------------------------------
File này chứa toàn bộ logic scheduler và background jobs (CQRS Accounting Queue,
Backup, Tenant Expiry...).

File này ĐƯỢC THIẾT KẾ ĐỂ CHẠY ĐƠN NHIỆM (Standalone Process) trên VPS
bằng Supervisord hoặc Systemd.

Lý do tách file: Tránh Gunicorn N workers cùng start N bộ scheduler
trên SQLite gây lỗi "database is locked".
"""
from __future__ import annotations

import atexit
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# Thêm thư mục gốc vào sys.path để import được các modules khác
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from db.errors import INTEGRITY_ERROR, OPERATIONAL_ERROR

# Thiết lập logging cơ bản cho file worker standalone
log_dir = os.path.join(BASE_DIR, 'logs')
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s (pid=%(process)d): %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, 'worker.log'),
            maxBytes=5 * 1024 * 1024, # 5MB
            backupCount=3,
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger('worker')

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Import các config từ db_utils gốc của bạn
from db_utils import (
    MAIN_DB_PATH,
    REGISTRY_PATH,
    _raw_sqlite_conn,
    get_main_db_connection,
    open_sqlite,
    sqlite_write_retry,
)

# --- KHỐI GIỮ NGUYÊN HOÀN TOÀN CÁC CẤU HÌNH CŨ CỦA BẠN ---
_leader_lock_fh = None
_schedulers_started = False
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

# --- KHỐI GIỮ NGUYÊN CƠ CHẾ FILE LOCK LEADERSHIP CŨ CỦA BẠN ---
# Windows: msvcrt.locking + PID stale check. Linux: fcntl.

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
            kernel32 = ctypes.windll.kernel32
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
    """Xóa lock scheduler nếu process ghi PID đã chết."""
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
    """Chỉ một process trên máy giữ quyền chạy background jobs."""
    global _leader_lock_fh
    if _leader_lock_fh is not None:
        return True

    cleanup_stale_scheduler_lock()
    path = _scheduler_lock_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, 'a+b')
    except OSError as exc:
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            fh = open(path, 'a+b')
        except OSError as exc2:
            logger.warning('Không mở được scheduler lock %s: %s', path, exc2 or exc)
            return False

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
        # THAY ĐỔI NHỎ ĐỂ PHÙ HỢP WORKER STANDALONE
        logger.error(
            'Worker bỏ qua process pid=%s — process khác đang giữ leadership (old_pid=%s). '
            'Chương trình sẽ tự thoát sau 5s.',
            os.getpid(),
            old_pid or '?',
        )
        time.sleep(5)
        sys.exit(0) # Tự thoát vì đã có leader khác chạy rồi

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
    logger.info('Worker leadership acquired (pid=%s, lock=%s)', os.getpid(), path)
    return True

# --- KHỐI GIỮ NGUYÊN CÁC JON NỀN CŨ CỦA BẠN (SỬA NHẸ LOG) ---

def _job_backup_database():
    backup_database(BACKUP_DIR)

def _job_accounting_queue():
    """Named job — chống chồng tick trong cùng process."""
    global _queue_tick_lock
    if _queue_tick_lock is None:
        _queue_tick_lock = threading.Lock()
    if not _queue_tick_lock.acquire(blocking=False):
        logger.debug('accounting_queue: tick trước chưa xong — bỏ qua')
        return
    try:
        _process_accounting_queue()
    finally:
        _queue_tick_lock.release()

def _job_accounting_reconcile():
    """
    Named reconciliation job.

    Không cho 2 tick reconcile chạy chồng nhau trong cùng worker.
    """
    global _reconcile_tick_lock

    if _reconcile_tick_lock is None:
        _reconcile_tick_lock = threading.Lock()

    if not _reconcile_tick_lock.acquire(blocking=False):
        logger.debug(
            'accounting_reconcile: tick trước chưa xong — bỏ qua'
        )
        return

    try:
        _reconcile_missing_sale_accounting()
    finally:
        _reconcile_tick_lock.release()

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
    logger.info(f"--- Đã kiểm tra và khóa các Tenant hết hạn ngày {today} ---")

def backup_database(backup_root):
    """Quét Registry và backup cho tất cả Tenant + Main DB."""
    try:
        from db.dialect import is_postgres
        if is_postgres():
            # Tối ưu: Chỉ chạy 1 lần pg_dump (đã có schema separation)
            try:
                out = Path(backup_root) / 'pg'
                # Dùng subprocess gọi script pg_dump chuyên dụng của bạn
                rc = subprocess.call([
                    sys.executable,
                    str(Path(BASE_DIR) / 'scripts' / 'pg_dump_backup.py'),
                    '--out', str(out),
                ])
                if rc == 0:
                    logger.info(f"Backup PostgreSQL OK -> {out}")
                else:
                    logger.error(
                        f"Backup PostgreSQL thất bại (rc={rc}) "
                        "— kiểm tra pg_dump / DATABASE_URL"
                    )
            except Exception as exc:
                logger.error(f"Backup PostgreSQL lỗi: {exc}")
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
            logger.error(f"Lỗi truy cập Registry: {db_err}")
            return

        for t_id, t_path in tenants:
            if not t_id or not t_path: continue
            t_id = str(t_id).strip()
            abs_path = t_path if os.isabs(t_path) else os.path.join(BASE_DIR, t_path)
            tasks.append((t_id, abs_path))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for tenant_id, db_path in tasks:
            try:
                if not os.path.exists(db_path):
                    logger.warning(f"Bỏ qua {tenant_id}: File không tồn tại tại {db_path}")
                    continue

                tenant_backup_dir = os.path.join(backup_root, tenant_id)
                os.makedirs(tenant_backup_dir, exist_ok=True)

                filename = f"{tenant_id}_auto_{timestamp}.db"
                dest = os.path.join(tenant_backup_dir, filename)
                
                # Timeout ngắn: đang bận phục vụ user thì bỏ qua tenant này
                try:
                    with open_sqlite(db_path, timeout=2.0) as src:
                        with open_sqlite(dest, timeout=5.0) as dst:
                            _raw_sqlite_conn(src).backup(_raw_sqlite_conn(dst))
                except OPERATIONAL_ERROR as e:
                    if 'locked' in str(e).lower():
                        logger.warning(f"Bỏ qua backup {tenant_id}: database đang bận")
                        continue
                    raise

                cutoff = (datetime.now() - timedelta(days=10)).timestamp()
                for f in os.listdir(tenant_backup_dir):
                    f_path = os.path.join(tenant_backup_dir, f)
                    if os.path.isfile(f_path) and f.endswith('.db'):
                        if os.path.getctime(f_path) < cutoff:
                            os.remove(f_path)
                logger.info(f"Backup OK: {tenant_id}")
            except Exception as e:
                logger.error(f"Lỗi khi backup tenant {tenant_id}: {e}")
                continue
    except Exception as e:
        logger.error(f"Lỗi hệ thống Backup (Tổng quát): {e}")

# --- KHỐI GIỮ NGUYÊN ACCOUNTING QUEUE LOGIC CŨ CỦA BẠN (SỬA NHẸ LOG) ---

def _collect_tenant_db_paths() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def _add(path: str) -> None:
        if not path: return
        full = os.path.abspath(path if os.path.isabs(path) else os.path.join(BASE_DIR, path))
        if full in seen or not os.path.isfile(full): return
        seen.add(full)
        out.append(full)

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
                _add(row['db_path'] if isinstance(row, sqlite3.Row) else row[0])
        except Exception: pass
    return out

def _reconcile_missing_sale_accounting():
    """
    Safety-net kế toán.

    Quét từng tenant DB để tìm:
        sale.status = completed
        nhưng chưa có active SALE journal.

    Chỉ enqueue; accounting queue worker sẽ ghi sổ.

    Nếu DB đang bận thì bỏ qua tenant ở tick này.
    Tick 5 phút sau sẽ thử lại.
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
                        created_by='pos_worker_reconcile',
                    )

                    scanned = int(
                        result.get('scanned') or 0
                    )
                    enqueued = int(
                        result.get('enqueued') or 0
                    )
                    skipped = int(
                        result.get('skipped') or 0
                    )
                    errors = int(
                        result.get('errors') or 0
                    )

                    total_scanned += scanned
                    total_enqueued += enqueued
                    total_skipped += skipped
                    total_errors += errors

                    if enqueued or errors:
                        logger.info(
                            'accounting_reconcile [%s]: '
                            'scanned=%d enqueued=%d '
                            'skipped=%d errors=%d',
                            os.path.basename(db_path),
                            scanned,
                            enqueued,
                            skipped,
                            errors,
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

        if total_enqueued or total_errors:
            logger.info(
                'accounting_reconcile total: '
                'scanned=%d enqueued=%d '
                'skipped=%d errors=%d',
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
    try:
        with open_sqlite(db_path, timeout=_QUEUE_PROBE_TIMEOUT_SEC) as conn:
            conn.row_factory = sqlite3.Row
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounting_jobs'"
            ).fetchone()
            if not has_table: return False
            pending = conn.execute(
                "SELECT 1 FROM accounting_jobs WHERE status IN ('pending','retry') LIMIT 1"
            ).fetchone()
            return bool(pending)
    except OPERATIONAL_ERROR as e:
        if 'locked' in str(e).lower(): return None
        return False
    except Exception as e:
        msg = str(e).lower()
        if 'lock' in msg or 'deadlock' in msg or 'timeout' in msg: return None
        return False

def _process_accounting_queue():
    """Background worker: chỉ xử lý DB có job pending; nhường khóa nếu đang bận."""
    try:
        tenant_dbs = _collect_tenant_db_paths()
        if not tenant_dbs: return

        if len(tenant_dbs) > _QUEUE_MAX_DBS_PER_TICK:
            offset = int(time.time() / max(5, _DEFAULT_QUEUE_SEC)) % len(tenant_dbs)
            rotated = tenant_dbs[offset:] + tenant_dbs[:offset]
        else:
            rotated = tenant_dbs

        processed_dbs = 0
        for db_path in rotated:
            if processed_dbs >= _QUEUE_MAX_DBS_PER_TICK: break

            pending = _db_has_pending_accounting_jobs(db_path)
            if pending is None:
                logger.debug('accounting_queue skip busy %s', os.path.basename(db_path))
                continue
            if not pending: continue

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

# --- KHỐI MAIN CHẠY WORKER STANDALONE ---

def start_worker():
    """Hàm main khởi chạy Scheduler và giữ process sống."""
    global _schedulers_started

    if _schedulers_started: return
    
    # 1. GATEKEEP: Cố gắng bầu Leader (ghi file lock logs/scheduler.leader.lock)
    if not try_acquire_scheduler_leadership():
        # Lệnh sys.exit(0) đã được gọi trong try_acquire nếu thất bại.
        return

    _schedulers_started = True

    # Giảm spam log "Running job ..." của APScheduler
    logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)
    logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)

    # 2. Khởi tạo Scheduler chính
    scheduler = BackgroundScheduler(
        daemon=True,
        job_defaults={
            'coalesce': True,
            'max_instances': 1,
            'misfire_grace_time': 300,
        },
    )

    # 3. ĐĂNG KÝ CÁC JOB CŨ CỦA BẠN VÀO SCHEDULER

    # 00:01 hàng ngày: Khóa tenant hết hạn
    scheduler.add_job(
        func=check_tenant_expirations,
        trigger=CronTrigger(hour=0, minute=1),
        id='do_check_expiry',
        name='do_check_expiry',
        replace_existing=True,
    )

    # 20:00 hàng ngày: Backup DB
    scheduler.add_job(
        func=_job_backup_database,
        trigger=CronTrigger(hour=20, minute=0),
        id='backup_database_daily',
        name='backup_database_daily',
        replace_existing=True,
    )

    # Interval (mặc định 30s): Xử lý Queue kế toán
    queue_interval = max(5, _DEFAULT_QUEUE_SEC)
    scheduler.add_job(
        func=_job_accounting_queue,
        trigger=IntervalTrigger(seconds=queue_interval),
        id='accounting_queue_worker',
        name='accounting_queue_worker',
        replace_existing=True,
    )

    # Interval mặc định 5 phút:
    # tìm completed sale bị thiếu accounting journal.
    reconcile_interval = max(
        60,
        _RECONCILE_INTERVAL_SEC,
    )

    scheduler.add_job(
        func=_job_accounting_reconcile,
        trigger=IntervalTrigger(
            seconds=reconcile_interval,
        ),
        id='accounting_reconcile_worker',
        name='accounting_reconcile_worker',
        replace_existing=True,
    )

    # 4. Bắt đầu Scheduler
    scheduler.start()

    # Chạy reconcile ngay một lần khi worker khởi động.
    # Không cần chờ interval 5 phút đầu tiên.
    try:
        _job_accounting_reconcile()
    except Exception as exc:
        logger.warning(
            'Initial accounting reconcile failed: %s',
            exc,
            exc_info=True,
        )

    logger.info(
        'Schedulers started '
        '(pid=%s, accounting_queue=%ss, '
        'accounting_reconcile=%ss, reconcile_batch=%s)',
        os.getpid(),
        queue_interval,
        reconcile_interval,
        _RECONCILE_BATCH_SIZE,
    )

    # 5. GIỮ PROCESS SỐNG MÃI MÃI (CHỜ SCHEDULER CHẠY)
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Chương trình dừng theo yêu cầu.")
        if scheduler.running:
            scheduler.shutdown(wait=False)

if __name__ == '__main__':
    # Định nghĩa các thư mục cần thiết
    BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
    
    start_worker()