"""Kết nối DB dùng chung — SQLite (mặc định) hoặc PostgreSQL (VPS production)."""
import logging
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager

from flask import g, has_request_context, session

from db.errors import OPERATIONAL_ERROR
from db.dialect import (
    db_backend,
    is_postgres,
    is_sqlite,
    is_locked_error as _dialect_locked_error,
    pg_schema_from_db_path,
    table_exists as _dialect_table_exists,
)
from db.schema_helpers import (  # noqa: F401 — re-export cho module legacy
    add_column_if_missing,
    column_exists,
    table_cols,
)

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MAIN_DB_PATH = os.path.join(BASE_DIR, "database.db")
# Alias tương thích code cũ (registry / master DB)
REGISTRY_PATH = MAIN_DB_PATH

# Gunicorn nhiều worker ghi cùng 1 file SQLite → cần chờ, nhưng KHÔNG vượt Nginx.
# Mặc định fail-fast (~ vài giây) để trả 503 thay vì treo worker → 504.
# Ghi đè: SME_SQLITE_TIMEOUT / SME_SQLITE_BUSY_TIMEOUT_MS / SME_SQLITE_WRITE_RETRIES
try:
    SQLITE_TIMEOUT_SEC = float(os.environ.get('SME_SQLITE_TIMEOUT', '8') or 8)
except ValueError:
    SQLITE_TIMEOUT_SEC = 8.0

try:
    SQLITE_WRITE_RETRIES = int(os.environ.get('SME_SQLITE_WRITE_RETRIES', '4') or 4)
except ValueError:
    SQLITE_WRITE_RETRIES = 4

# PRAGMA busy_timeout (ms) — mặc định 5s (trước đây 30s × retries → worker treo > Nginx).
try:
    SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get('SME_SQLITE_BUSY_TIMEOUT_MS', '5000') or 5000)
except ValueError:
    SQLITE_BUSY_TIMEOUT_MS = 5000

try:
    SQLITE_FILE_WRITE_LOCK_SEC = float(os.environ.get('SME_SQLITE_WRITE_LOCK_SEC', '3') or 3)
except ValueError:
    SQLITE_FILE_WRITE_LOCK_SEC = 3.0

# Single-writer trong cùng process (RLock — cho phép with_sqlite_write lồng nhau).
_WRITE_LOCKS_GUARD = threading.Lock()
_WRITE_LOCKS: dict[str, threading.RLock] = {}

# Đã bật WAL theo đường dẫn — tránh PRAGMA journal_mode lặp lại mỗi lần mở
_wal_ready_paths: set[str] = set()
# Schema/seed đã xong trong process này (key = đường dẫn file DB)
_process_ready: dict[str, set[str]] = {}


def _write_lock_for_db(path: str | None) -> threading.RLock:
    key = os.path.abspath(path) if path else '__anonymous__'
    with _WRITE_LOCKS_GUARD:
        if key not in _WRITE_LOCKS:
            _WRITE_LOCKS[key] = threading.RLock()
        return _WRITE_LOCKS[key]


@contextmanager
def sqlite_file_write_lock(conn_or_path, *, timeout: float | None = None):
    """Khóa ghi theo file DB trong process (single-writer pattern, thread-safe)."""
    if isinstance(conn_or_path, str):
        path = _normalize_db_path(conn_or_path) or conn_or_path
    else:
        path = sqlite_db_file(conn_or_path)
    lock = _write_lock_for_db(path)
    wait = SQLITE_FILE_WRITE_LOCK_SEC if timeout is None else timeout
    acquired = lock.acquire(timeout=max(0.1, wait))
    if not acquired:
        raise sqlite3.OperationalError('database is locked (write lock timeout)')
    try:
        yield
    finally:
        lock.release()


def _is_locked_error(exc: BaseException) -> bool:
    if _dialect_locked_error(exc):
        return True
    msg = str(exc).lower()
    return 'database is locked' in msg or 'database table is locked' in msg


def _raw_db_conn(conn):
    """Lấy connection thật từ proxy request-scoped / auto-close."""
    cur = conn
    for _ in range(6):
        if isinstance(cur, sqlite3.Connection):
            return cur
        if hasattr(cur, '_conn') and isinstance(getattr(cur, '_conn', None), sqlite3.Connection):
            return getattr(cur, '_conn')
        if isinstance(cur, _RequestScopedConnection):
            cur = object.__getattribute__(cur, '_conn')
            continue
        if isinstance(cur, _AutoCloseConnection):
            cur = cur._raw()
            continue
        inner = getattr(cur, '_inner', None)
        if inner is not None:
            cur = inner
            continue
        break
    return cur


def _raw_sqlite_conn(conn):
    """Alias tương thích — chỉ SQLite thật."""
    raw = _raw_db_conn(conn)
    if isinstance(raw, sqlite3.Connection):
        return raw
    raise TypeError('Expected sqlite3 connection')


def begin_immediate(conn, *, label: str = 'begin_immediate', retries: int | None = None) -> None:
    """Bắt đầu transaction ghi — SQLite: BEGIN IMMEDIATE; PostgreSQL: BEGIN.

    ``retries``: số lần thử khi locked. Gọi từ ``with_sqlite_write`` nên truyền 1
    để tránh retry lồng (busy_timeout × retries × retries → 504).
    """
    if is_postgres():
        def _pg():
            raw = _raw_db_conn(conn)
            if getattr(raw, 'in_transaction', False):
                return
            schema = getattr(raw, '_sme_pg_schema', 'public')
            from db.postgres_backend import pg_write_lock
            with pg_write_lock(schema):
                if not getattr(raw, 'in_transaction', False):
                    raw.execute('BEGIN')
        from db.postgres_backend import pg_write_retry
        pg_write_retry(_pg, label=label)
        return

    def _do():
        raw = _raw_sqlite_conn(conn)
        try:
            if getattr(raw, 'in_transaction', False):
                return
        except Exception:
            pass
        lock_key = sqlite_db_file(conn)
        with sqlite_file_write_lock(lock_key or conn):
            try:
                raw.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if 'within a transaction' in msg or 'transaction' in msg:
                    try:
                        raw.rollback()
                    except Exception:
                        pass
                    raw.execute('BEGIN IMMEDIATE')
                    return
                raise

    # with_sqlite_write truyền retries=1; gọi độc lập giữ retry bình thường
    attempt = SQLITE_WRITE_RETRIES if retries is None else retries
    sqlite_write_retry(_do, label=label, retries=attempt)


def rollback_quietly(conn) -> None:
    """Rollback an toàn (request-scoped / đã commit) — SQLite hoặc PostgreSQL."""
    try:
        raw = _raw_db_conn(conn)
        if hasattr(raw, 'rollback'):
            raw.rollback()
            return
    except Exception:
        pass
    try:
        conn.rollback()
    except Exception:
        pass


class _AutoCloseConnection:
    """Proxy: ``with open_sqlite(...)`` / ``close()`` luôn đóng file thật.

    ``sqlite3.Connection`` dùng làm context manager chỉ commit/rollback, **không**
    đóng connection — với Gunicorn nhiều worker sẽ giữ khóa → database is locked.
    """

    __slots__ = ('_conn', '_closed')

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_closed', False)

    def close(self):
        if object.__getattribute__(self, '_closed'):
            return
        object.__setattr__(self, '_closed', True)
        try:
            sqlite3.Connection.close(object.__getattribute__(self, '_conn'))
        except Exception:
            pass

    def _raw(self) -> sqlite3.Connection:
        return object.__getattribute__(self, '_conn')

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name, value):
        if name in ('_conn', '_closed'):
            object.__setattr__(self, name, value)
            return
        setattr(object.__getattribute__(self, '_conn'), name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                try:
                    self._raw().rollback()
                except Exception:
                    pass
        finally:
            self.close()
        return False

    def __iter__(self):
        return iter(self._raw())


class _RequestScopedConnection:
    """Proxy: ``close()`` no-op trong request; teardown mới đóng SQLite thật.

    Cần thiết trên Python 3.12+ vì ``sqlite3.Connection.close`` là read-only.
    """

    __slots__ = ('_conn',)

    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def close(self):
        return None

    def _real_close(self):
        inner = object.__getattribute__(self, '_conn')
        try:
            if isinstance(inner, _AutoCloseConnection):
                inner.close()
            else:
                sqlite3.Connection.close(inner)
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name, value):
        if name == '_conn':
            object.__setattr__(self, name, value)
            return
        setattr(object.__getattribute__(self, '_conn'), name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Không đóng — giữ connection cho các lần get_db_connection() sau trong cùng request
        return False

    def __iter__(self):
        return iter(object.__getattribute__(self, '_conn'))


def _normalize_db_path(db_path):
    """Chuẩn hóa đường dẫn file SQLite (tương đối → tuyệt đối trong BASE_DIR)."""
    if not db_path:
        return None
    text = str(db_path).strip()
    if not text:
        return None
    if not os.path.isabs(text):
        return os.path.join(BASE_DIR, text)
    return text


def ensure_sqlite_wal(conn: sqlite3.Connection, db_path: str | None = None) -> str | None:
    """Bật WAL trên connection đang mở (có retry khi locked)."""
    raw = _raw_sqlite_conn(conn)
    path = os.path.abspath(db_path) if db_path else sqlite_db_file(conn)

    def _set():
        mode = raw.execute('PRAGMA journal_mode=WAL').fetchone()
        if path and mode and str(mode[0]).lower() == 'wal':
            _wal_ready_paths.add(path)
            try:
                raw.execute('PRAGMA wal_autocheckpoint = 1000')
                raw.execute('PRAGMA journal_size_limit = 67108864')
            except sqlite3.Error:
                pass
        return str(mode[0]) if mode else None

    return sqlite_write_retry(_set, label='ensure_sqlite_wal')


def locked_user_message() -> str:
    return (
        'Hệ thống đang bận (database is locked). '
        'Vui lòng thử lại sau vài giây. Nếu lặp lại, Master chạy Kiểm tra & tự sửa (WAL).'
    )


def _configure_sqlite_connection(conn: sqlite3.Connection, db_path: str | None = None) -> sqlite3.Connection:
    """WAL + busy_timeout giúp đọc/ghi song song ổn định hơn trên SQLite file."""
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f'PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}')
    except sqlite3.Error:
        pass
    key = os.path.abspath(db_path) if db_path else None
    if key and key not in _wal_ready_paths:
        last_wal_exc = None
        for attempt in range(max(1, SQLITE_WRITE_RETRIES)):
            try:
                mode = conn.execute('PRAGMA journal_mode = WAL').fetchone()
                last_wal_exc = None
                if mode and str(mode[0]).lower() == 'wal':
                    _wal_ready_paths.add(key)
                    try:
                        conn.execute('PRAGMA wal_autocheckpoint = 1000')
                        conn.execute('PRAGMA journal_size_limit = 67108864')
                    except sqlite3.Error:
                        pass
                elif mode:
                    logger.warning(
                        'SQLite journal_mode=%s (WAL chưa bật — dễ database is locked với Gunicorn multi-worker)',
                        mode[0],
                    )
                break
            except sqlite3.OperationalError as exc:
                last_wal_exc = exc
                if not _is_locked_error(exc) or attempt >= SQLITE_WRITE_RETRIES - 1:
                    logger.warning('SQLite WAL chưa bật: %s', exc)
                    break
                time.sleep(min(0.05 * (2 ** attempt) + random.uniform(0, 0.04), 1.2))
        if last_wal_exc and not _is_locked_error(last_wal_exc):
            logger.warning('SQLite WAL chưa bật: %s', last_wal_exc)
    try:
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.execute('PRAGMA temp_store = MEMORY')
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA locking_mode = NORMAL')
    except sqlite3.Error:
        pass
    return conn


def db_path_available(db_path) -> bool:
    """True nếu có thể mở DB tenant (file SQLite tồn tại, hoặc backend Postgres)."""
    if not db_path:
        return False
    if is_postgres():
        return True
    path = _normalize_db_path(db_path) or db_path
    return os.path.isfile(path)


def open_sqlite(db_path, *, timeout: float | None = None):
    """Mở DB tenant — SQLite file hoặc Postgres schema (khi SME_DB_BACKEND=postgres).

    LUÔN dùng hàm này (hoặc get_db_connection / get_main_db_connection) thay vì
    ``sqlite3.connect`` thô — ``with sqlite3.connect(...)`` KHÔNG đóng file trên
    Python, dễ giữ khóa giữa các worker Gunicorn.
    """
    if is_postgres():
        from db.postgres_backend import ensure_pg_schema, open_pg
        from db.dialect import pg_schema_from_db_path
        schema = pg_schema_from_db_path(db_path)
        ensure_pg_schema(schema)
        return open_pg(schema=schema)

    path = _normalize_db_path(db_path) or db_path
    wait = SQLITE_TIMEOUT_SEC if timeout is None else timeout
    last_exc = None
    for attempt in range(max(1, SQLITE_WRITE_RETRIES)):
        try:
            raw = sqlite3.connect(
                path,
                timeout=wait,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
            )
            return _AutoCloseConnection(_configure_sqlite_connection(raw, path))
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not _is_locked_error(exc) or attempt >= SQLITE_WRITE_RETRIES - 1:
                raise
            sleep_s = min(0.08 * (2 ** attempt) + random.uniform(0, 0.08), 2.5)
            logger.warning('open_sqlite locked (lần %s/%s), chờ %.2fs: %s', attempt + 1, SQLITE_WRITE_RETRIES, sleep_s, exc)
            time.sleep(sleep_s)
    if last_exc:
        raise last_exc
    raise sqlite3.OperationalError('open_sqlite failed')


def sqlite_write_retry(fn, *, retries: int | None = None, label: str = 'sqlite_write'):
    """Chạy ``fn()``; nếu database is locked / PG deadlock thì chờ rồi thử lại."""
    total = SQLITE_WRITE_RETRIES if retries is None else retries
    last_exc = None
    for attempt in range(max(1, total)):
        try:
            return fn()
        except OPERATIONAL_ERROR as exc:
            last_exc = exc
            if not _is_locked_error(exc) or attempt >= total - 1:
                raise
            sleep_s = min(0.08 * (2 ** attempt) + random.uniform(0, 0.08), 2.5)
            log_fn = logger.warning if attempt >= total - 2 else logger.debug
            log_fn(
                '%s locked (lần %s/%s), chờ %.2fs: %s',
                label, attempt + 1, total, sleep_s, exc,
            )
            time.sleep(sleep_s)
    if last_exc:
        raise last_exc
    return None


def sqlite_db_file(conn) -> str | None:
    """Đường dẫn file SQLite hoặc schema Postgres (khóa ghi / cache)."""
    raw = _raw_db_conn(conn)
    if getattr(raw, '_sme_backend', None) == 'postgres':
        return getattr(raw, '_sme_pg_schema', None)
    try:
        if not isinstance(raw, sqlite3.Connection):
            return getattr(raw, '_sme_pg_schema', None)
        row = raw.execute('PRAGMA database_list').fetchone()
        if not row:
            return None
        path = row[2] if not isinstance(row, sqlite3.Row) else row['file']
        text = str(path or '').strip()
        return os.path.abspath(text) if text else None
    except sqlite3.Error:
        return None


def sqlite_is_ready(conn, flag: str) -> bool:
    key = sqlite_db_file(conn) or f'conn:{id(_raw_sqlite_conn(conn))}'
    return flag in _process_ready.get(key, set())


def sqlite_mark_ready(conn, flag: str) -> None:
    key = sqlite_db_file(conn) or f'conn:{id(_raw_sqlite_conn(conn))}'
    _process_ready.setdefault(key, set()).add(flag)


def sqlite_clear_ready(db_path: str | None = None) -> None:
    if not db_path:
        _process_ready.clear()
        return
    try:
        _process_ready.pop(os.path.abspath(db_path), None)
    except OSError:
        pass


def sqlite_table_exists(conn, name: str) -> bool:
    return _dialect_table_exists(conn, name)


def resolve_pg_schema() -> str:
    tenant_id = getattr(g, 'tenant_id', None) if has_request_context() else None
    db_path = resolve_db_path()
    if is_postgres() and paths_same_db(db_path, MAIN_DB_PATH):
        return pg_schema_from_db_path(MAIN_DB_PATH, tenant_id=None)
    return pg_schema_from_db_path(db_path, tenant_id=tenant_id)


def _open_db_for_path(db_path: str, *, request_scoped: bool = False):
    if is_postgres():
        from db.postgres_backend import open_pg, open_pg_request, ensure_pg_schema
        schema = pg_schema_from_db_path(
            db_path,
            tenant_id=getattr(g, 'tenant_id', None) if has_request_context() else None,
        )
        if schema != 'public':
            ensure_pg_schema(schema)
        if request_scoped:
            return open_pg_request(schema)
        return open_pg(schema=schema)
    inner = open_sqlite(db_path)
    if request_scoped:
        return _RequestScopedConnection(inner)
    return inner


def with_sqlite_write(conn, fn, *, commit: bool = True, label: str = 'sqlite_write'):
    """Chạy ``fn(target)`` có retry khi locked + khóa ghi theo file + BEGIN IMMEDIATE.

    ``commit=False`` (đọc sổ / cùng transaction nghiệp vụ): ghi DDL/seed trên
    **connection riêng** rồi commit ngay — không giữ khóa trên conn request.
    ``commit=True``: ghi trên đúng ``conn`` rồi commit.
    """
    def _run():
        own = None
        target = conn
        lock_key = sqlite_db_file(conn)
        try:
            if not commit:
                path = sqlite_db_file(conn)
                if path:
                    # Gỡ transaction đọc trên conn request — tránh 2 handle cùng file → locked
                    rollback_quietly(conn)
                    own = open_sqlite(path, timeout=SQLITE_TIMEOUT_SEC)
                    target = own
                    lock_key = path
            with sqlite_file_write_lock(lock_key or conn):
                # retries=1: with_sqlite_write đã có sqlite_write_retry bên ngoài
                begin_immediate(target, label=label, retries=1)
                fn(target)
                try:
                    target.commit()
                except Exception:
                    pass
        except Exception:
            try:
                rollback_quietly(target)
            except Exception:
                pass
            raise
        finally:
            if own is not None:
                try:
                    own.close()
                except Exception:
                    pass

    if is_postgres():
        from db.postgres_backend import pg_write_retry
        return pg_write_retry(_run, label=label)
    return sqlite_write_retry(_run, label=label)


def sqlite_run_write(conn, fn, *, label: str = 'sqlite_write'):
    """Chạy ``fn(conn)`` trong giao dịch ghi ngắn (file lock + BEGIN IMMEDIATE + commit + retry).

    Dùng khi route/service đã có ``conn`` từ ``get_db_connection()`` — tránh nhiều ``commit()`` rời rạc.
    """
    out: list = []

    def _wrapper(target):
        out.append(fn(target))

    with_sqlite_write(conn, _wrapper, commit=True, label=label)
    return out[0] if out else None


def sqlite_commit(conn, *, label: str = 'sqlite_commit') -> None:
    """Commit giao dịch hiện tại — file lock + retry (SQLite) hoặc schema lock (PostgreSQL)."""

    if is_postgres():
        def _pg():
            raw = _raw_db_conn(conn)
            schema = getattr(raw, '_sme_pg_schema', 'public')
            from db.postgres_backend import pg_write_lock, pg_write_retry
            with pg_write_lock(schema):
                if not getattr(raw, 'in_transaction', False):
                    raw.execute('BEGIN')
                raw.commit()
        from db.postgres_backend import pg_write_retry
        pg_write_retry(_pg, label=label)
        return

    def _do():
        raw = _raw_sqlite_conn(conn)
        in_txn = False
        try:
            in_txn = bool(getattr(raw, 'in_transaction', False))
        except Exception:
            pass
        lock_key = sqlite_db_file(conn)
        with sqlite_file_write_lock(lock_key or conn):
            if not in_txn:
                raw.execute('BEGIN IMMEDIATE')
            raw.commit()

    sqlite_write_retry(_do, label=label)


def paths_same_db(a, b) -> bool:
    """So sánh hai đường dẫn SQLite (chuẩn hóa abs)."""
    pa, pb = _normalize_db_path(a), _normalize_db_path(b)
    if not pa or not pb:
        return False
    try:
        return os.path.abspath(pa) == os.path.abspath(pb)
    except OSError:
        return pa == pb


def resolve_db_path():
    """
    Xác định database đang active:
    1. g.db_path (middleware tenant / session)
    2. session['db_path'] (dự phòng khi g chưa gán)
    3. MAIN_DB_PATH (hệ thống chính)
    """
    db_path = getattr(g, "db_path", None)
    if not db_path and has_request_context():
        try:
            db_path = session.get("db_path")
        except RuntimeError:
            db_path = None
    normalized = _normalize_db_path(db_path)
    if normalized:
        return normalized
    return MAIN_DB_PATH


def get_db_connection():
    """Kết nối DB của tenant hiện tại (hoặc main DB nếu chưa có tenant).

    Trong request Flask: tái sử dụng 1 connection trên ``g._sme_db``.
    ``conn.close()`` trong route là no-op — teardown mới đóng thật.
    """
    db_path = resolve_db_path()
    cache_key = resolve_pg_schema() if is_postgres() else db_path
    logger.debug(
        "DB: %s | Tenant: %s | Backend: %s",
        cache_key,
        getattr(g, "tenant_id", None) if has_request_context() else "NO_CTX",
        db_backend(),
    )
    if has_request_context():
        cached = getattr(g, '_sme_db', None)
        cached_path = getattr(g, '_sme_db_path', None)
        if cached is not None and cached_path == cache_key:
            return cached

        conn = _open_db_for_path(db_path, request_scoped=True)
        g._sme_db = conn
        g._sme_db_path = cache_key
        if is_postgres():
            g._sme_pg_schema = cache_key
        return conn
    return _open_db_for_path(db_path, request_scoped=False)


def close_request_db():
    """Đóng connection request-scoped (gọi từ teardown)."""
    if not has_request_context():
        return
    conn = getattr(g, '_sme_db', None)
    if conn is None:
        return
    g._sme_db = None
    g._sme_db_path = None
    try:
        # Gỡ transaction dở (lỗi FK không rollback) — tránh giữ khóa WAL
        rollback_quietly(conn)
    except Exception:
        pass
    try:
        if isinstance(conn, _RequestScopedConnection):
            conn._real_close()
        elif hasattr(conn, '_real_close'):
            conn._real_close()
        else:
            conn.close()
    except (sqlite3.Error, Exception):
        pass


def get_main_db_connection():
    """Kết nối main/registry database (tenants, mapping, login history)."""
    if is_postgres():
        from db.postgres_backend import open_pg
        return open_pg(schema=pg_schema_from_db_path(MAIN_DB_PATH))
    return open_sqlite(MAIN_DB_PATH)


def force_close_request_db_if_path(db_path: str | None) -> None:
    """Đóng ngay connection request-scoped nếu đang mở đúng ``db_path`` (trước khi xóa file)."""
    if not db_path or not has_request_context():
        return
    cached_path = getattr(g, '_sme_db_path', None)
    if not cached_path:
        return
    try:
        if os.path.abspath(cached_path) != os.path.abspath(db_path):
            return
    except OSError:
        return
    close_request_db()
    # WAL cache: cho phép process khác / lần mở sau cấu hình lại
    try:
        abs_path = os.path.abspath(db_path)
        _wal_ready_paths.discard(abs_path)
        sqlite_clear_ready(abs_path)
    except OSError:
        pass


def remove_sqlite_files(db_path: str | None, *, retries: int = 5, delay_sec: float = 0.15) -> dict:
    """Xóa file SQLite kèm ``-wal`` / ``-shm`` (Windows hay giữ lock ngắn)."""
    import time

    result = {'removed': [], 'errors': []}
    path = _normalize_db_path(db_path)
    if not path:
        return result
    force_close_request_db_if_path(path)
    candidates = [path, f'{path}-wal', f'{path}-shm', f'{path}-journal']
    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        last_err = None
        for _ in range(max(1, retries)):
            try:
                os.remove(candidate)
                result['removed'].append(candidate)
                last_err = None
                break
            except OSError as exc:
                last_err = exc
                time.sleep(delay_sec)
        if last_err is not None:
            result['errors'].append(f'{candidate}: {last_err}')
    return result


def get_tenant_db_connection(tenant_id):
    """Mở DB của một tenant cụ thể (dùng khi master truy vấn nhật ký)."""
    if not tenant_id:
        return None
    conn_main = get_main_db_connection()
    try:
        row = conn_main.execute(
            "SELECT db_path FROM tenants WHERE tenant_id = ?", (tenant_id.strip(),)
        ).fetchone()
    finally:
        conn_main.close()
    if not row or not row["db_path"]:
        return None
    db_path = _normalize_db_path(row["db_path"])
    if is_postgres():
        from db.postgres_backend import open_pg, ensure_pg_schema
        schema = pg_schema_from_db_path(db_path, tenant_id=tenant_id.strip())
        ensure_pg_schema(schema)
        return open_pg(schema=schema)
    if not db_path_available(db_path):
        return None
    return open_sqlite(db_path)


def open_db(db_path=None, *, request_scoped: bool = False):
    """Mở DB theo backend hiện tại (SQLite file hoặc Postgres schema)."""
    path = _normalize_db_path(db_path) if db_path else resolve_db_path()
    return _open_db_for_path(path, request_scoped=request_scoped)
