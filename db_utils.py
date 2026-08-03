"""Kết nối SQLite dùng chung — nguồn duy nhất cho tenant DB và main/registry DB."""
import logging
import os
import sqlite3

from flask import g, has_request_context, session

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MAIN_DB_PATH = os.path.join(BASE_DIR, "database.db")
# Alias tương thích code cũ (registry / master DB)
REGISTRY_PATH = MAIN_DB_PATH

# Chờ khi DB đang bị process/thread khác giữ khóa ghi.
# 5s đủ cho contention ngắn; 30s khiến trang "treo" rồi user hủy request (Network: đã hủy).
SQLITE_TIMEOUT_SEC = 5.0

# Đã bật WAL theo đường dẫn — tránh PRAGMA journal_mode lặp lại mỗi lần mở
_wal_ready_paths: set[str] = set()


class _RequestScopedConnection:
    """Proxy: ``close()`` no-op trong request; teardown mới đóng SQLite thật.

    Cần thiết trên Python 3.12+ vì ``sqlite3.Connection.close`` là read-only.
    """

    __slots__ = ('_conn',)

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, '_conn', conn)

    def close(self):
        return None

    def _real_close(self):
        sqlite3.Connection.close(self._conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == '_conn':
            object.__setattr__(self, name, value)
            return
        setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Không đóng — giữ connection cho các lần get_db_connection() sau trong cùng request
        return False

    def __iter__(self):
        return iter(self._conn)


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
    try:
        conn.execute(f'PRAGMA busy_timeout = {int(SQLITE_TIMEOUT_SEC * 1000)}')
    except sqlite3.Error:
        pass
    key = os.path.abspath(db_path) if db_path else None
    if key and key not in _wal_ready_paths:
        try:
            mode = conn.execute('PRAGMA journal_mode = WAL').fetchone()
            if mode and str(mode[0]).lower() == 'wal':
                _wal_ready_paths.add(key)
            elif mode:
                logger.debug(
                    'SQLite journal_mode=%s (WAL chưa bật — có thể DB đang bị process khác giữ)',
                    mode[0],
                )
        except sqlite3.Error as exc:
            logger.debug('SQLite WAL chưa bật: %s', exc)
    try:
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.execute('PRAGMA temp_store = MEMORY')
        conn.execute('PRAGMA foreign_keys = ON')
    except sqlite3.Error:
        pass
    return conn


def open_sqlite(db_path, *, timeout: float | None = None) -> sqlite3.Connection:
    """Mở file SQLite với timeout/WAL chuẩn dự án."""
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SEC if timeout is None else timeout,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    return _configure_sqlite_connection(conn, db_path)


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
