"""Kết nối SQLite dùng chung — nguồn duy nhất cho tenant DB và main/registry DB."""
import logging
import os
import random
import sqlite3
import time

from flask import g, has_request_context, session

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MAIN_DB_PATH = os.path.join(BASE_DIR, "database.db")
# Alias tương thích code cũ (registry / master DB)
REGISTRY_PATH = MAIN_DB_PATH

# Gunicorn nhiều worker ghi cùng 1 file SQLite → cần chờ lâu hơn khi locked.
# Có thể ghi đè: export SME_SQLITE_TIMEOUT=60
try:
    SQLITE_TIMEOUT_SEC = float(os.environ.get('SME_SQLITE_TIMEOUT', '60') or 60)
except ValueError:
    SQLITE_TIMEOUT_SEC = 60.0

try:
    SQLITE_WRITE_RETRIES = int(os.environ.get('SME_SQLITE_WRITE_RETRIES', '12') or 12)
except ValueError:
    SQLITE_WRITE_RETRIES = 12

# Đã bật WAL theo đường dẫn — tránh PRAGMA journal_mode lặp lại mỗi lần mở
_wal_ready_paths: set[str] = set()


def _is_locked_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return 'database is locked' in msg or 'database table is locked' in msg


def _raw_sqlite_conn(conn):
    """Lấy sqlite3.Connection thật từ proxy request-scoped / auto-close."""
    cur = conn
    for _ in range(4):
        if isinstance(cur, sqlite3.Connection):
            return cur
        if isinstance(cur, _RequestScopedConnection):
            cur = object.__getattribute__(cur, '_conn')
            continue
        if isinstance(cur, _AutoCloseConnection):
            cur = cur._raw()
            continue
        break
    return cur


def begin_immediate(conn, *, label: str = 'begin_immediate') -> None:
    """``BEGIN IMMEDIATE`` có retry khi database locked; bỏ qua nếu đã trong transaction.

    Sau lỗi FK / constraint trên cùng connection request-scoped, gọi ``rollback``
    rồi mới BEGIN lại để tránh giữ khóa và làm checkout bị ``database is locked``.
    """
    def _do():
        raw = _raw_sqlite_conn(conn)
        try:
            if getattr(raw, 'in_transaction', False):
                return
        except Exception:
            pass
        try:
            raw.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            # Transaction aborted / cannot start — gỡ rồi thử lại trong retry loop
            if 'within a transaction' in msg or 'transaction' in msg:
                try:
                    raw.rollback()
                except Exception:
                    pass
                raw.execute('BEGIN IMMEDIATE')
                return
            raise

    sqlite_write_retry(_do, label=label)


def rollback_quietly(conn) -> None:
    """Rollback an toàn (request-scoped / đã commit)."""
    try:
        raw = _raw_sqlite_conn(conn)
        raw.rollback()
    except Exception:
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


def _configure_sqlite_connection(conn: sqlite3.Connection, db_path: str | None = None) -> sqlite3.Connection:
    """WAL + busy_timeout giúp đọc/ghi song song ổn định hơn trên SQLite file."""
    conn.row_factory = sqlite3.Row
    busy_ms = int(max(SQLITE_TIMEOUT_SEC, 1) * 1000)
    try:
        conn.execute(f'PRAGMA busy_timeout = {busy_ms}')
    except sqlite3.Error:
        pass
    key = os.path.abspath(db_path) if db_path else None
    if key and key not in _wal_ready_paths:
        try:
            mode = conn.execute('PRAGMA journal_mode = WAL').fetchone()
            if mode and str(mode[0]).lower() == 'wal':
                _wal_ready_paths.add(key)
                try:
                    conn.execute('PRAGMA wal_autocheckpoint = 1000')
                except sqlite3.Error:
                    pass
            elif mode:
                logger.warning(
                    'SQLite journal_mode=%s (WAL chưa bật — dễ database is locked với Gunicorn multi-worker)',
                    mode[0],
                )
        except sqlite3.Error as exc:
            logger.warning('SQLite WAL chưa bật: %s', exc)
    try:
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.execute('PRAGMA temp_store = MEMORY')
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA locking_mode = NORMAL')
    except sqlite3.Error:
        pass
    return conn


def open_sqlite(db_path, *, timeout: float | None = None):
    """Mở file SQLite với timeout/WAL; trả proxy tự đóng khi dùng ``with`` / ``close()``.

    LUÔN dùng hàm này (hoặc get_db_connection / get_main_db_connection) thay vì
    ``sqlite3.connect`` thô — ``with sqlite3.connect(...)`` KHÔNG đóng file trên
    Python, dễ giữ khóa giữa các worker Gunicorn.
    """
    path = _normalize_db_path(db_path) or db_path
    raw = sqlite3.connect(
        path,
        timeout=SQLITE_TIMEOUT_SEC if timeout is None else timeout,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    return _AutoCloseConnection(_configure_sqlite_connection(raw, path))


def sqlite_write_retry(fn, *, retries: int | None = None, label: str = 'sqlite_write'):
    """Chạy ``fn()``; nếu database is locked thì chờ rồi thử lại (Gunicorn multi-worker)."""
    total = SQLITE_WRITE_RETRIES if retries is None else retries
    last_exc = None
    for attempt in range(max(1, total)):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not _is_locked_error(exc) or attempt >= total - 1:
                raise
            sleep_s = min(0.08 * (2 ** attempt) + random.uniform(0, 0.08), 2.5)
            logger.warning(
                '%s locked (lần %s/%s), chờ %.2fs: %s',
                label, attempt + 1, total, sleep_s, exc,
            )
            time.sleep(sleep_s)
    if last_exc:
        raise last_exc
    return None


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
    logger.debug(
        "DB: %s | Tenant: %s",
        db_path,
        getattr(g, "tenant_id", None) if has_request_context() else "NO_CTX",
    )
    if has_request_context():
        cached = getattr(g, '_sme_db', None)
        cached_path = getattr(g, '_sme_db_path', None)
        if cached is not None and cached_path == db_path:
            return cached

        conn = _RequestScopedConnection(open_sqlite(db_path))
        g._sme_db = conn
        g._sme_db_path = db_path
        return conn
    return open_sqlite(db_path)


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
        else:
            conn.close()
    except sqlite3.Error:
        pass


def get_main_db_connection():
    """Kết nối main/registry database (tenants, mapping, login history)."""
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
        _wal_ready_paths.discard(os.path.abspath(db_path))
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
    if not db_path or not os.path.exists(db_path):
        return None
    return open_sqlite(db_path)
