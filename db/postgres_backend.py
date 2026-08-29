"""PostgreSQL backend — schema-per-tenant, API tương thích sqlite3.Connection.execute()."""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from contextlib import contextmanager
from typing import Any

from db.dialect import (
    BACKEND_POSTGRES,
    is_locked_error,
    pg_schema_from_db_path,
    sanitize_pg_schema,
)
from db.sql_compat import compat_row_factory, rewrite_sql_for_postgres

logger = logging.getLogger(__name__)

try:
    import psycopg
    from psycopg import sql as pg_sql
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore
    pg_sql = None  # type: ignore
    ConnectionPool = None  # type: ignore

_POOL: ConnectionPool | None = None
_POOL_GUARD = threading.Lock()
_SCHEMA_LOCKS_GUARD = threading.Lock()
_SCHEMA_LOCKS: dict[str, threading.RLock] = {}
_SCHEMA_READY: set[str] = set()

_INSERT_RE = re.compile(
    r'^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\b',
    re.IGNORECASE,
)
# Câu ghi — dùng SAVEPOINT; SELECT không dùng (tránh mất result set sau RELEASE)
_WRITE_SQL_RE = re.compile(
    r'^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|VACUUM|ANALYZE)\b',
    re.IGNORECASE,
)


def database_url() -> str:
    for key in ('SME_PG_URL', 'DATABASE_URL'):
        url = (os.environ.get(key) or '').strip()
        if not url:
            continue
        if url.startswith('postgres://'):
            url = 'postgresql://' + url[len('postgres://'):]
        if url.startswith('postgresql+psycopg://'):
            url = 'postgresql://' + url[len('postgresql+psycopg://'):]
        if url.startswith('postgresql://'):
            return url
    raise RuntimeError(
        'PostgreSQL: thiếu SME_PG_URL hoặc DATABASE_URL dạng postgresql://...'
    )


def _require_psycopg() -> None:
    if psycopg is None or ConnectionPool is None:
        raise RuntimeError(
            'PostgreSQL backend cần psycopg: pip install "psycopg[binary]" psycopg-pool'
        )


def get_pool() -> ConnectionPool:
    global _POOL
    _require_psycopg()
    with _POOL_GUARD:
        if _POOL is None:
            # Gunicorn 4 worker + scheduler + CRM song song — cần pool đủ lớn
            min_size = int(os.environ.get('SME_PG_POOL_MIN', '4') or 4)
            max_size = int(os.environ.get('SME_PG_POOL_MAX', '50') or 50)
            timeout = float(os.environ.get('SME_PG_POOL_TIMEOUT', '15') or 15)
            kwargs = {
                'conninfo': database_url(),
                'min_size': min_size,
                'max_size': max_size,
                'timeout': timeout,
                'kwargs': {
                    'row_factory': compat_row_factory,
                    'autocommit': False,
                    'connect_timeout': 10,
                },
                'open': True,
            }
            # Kiểm tra connection khi lấy từ pool (psycopg_pool ≥ 3.1)
            check_fn = getattr(ConnectionPool, 'check_connection', None)
            if check_fn is not None:
                kwargs['check'] = check_fn
            _POOL = ConnectionPool(**kwargs)
        return _POOL


def reset_pg_pool() -> None:
    """Đóng pool (sau migrate hang / leak) rồi tạo lại lần get tiếp theo."""
    close_pg_pool()


def schema_lock(schema: str) -> threading.RLock:
    key = sanitize_pg_schema(schema)
    with _SCHEMA_LOCKS_GUARD:
        if key not in _SCHEMA_LOCKS:
            _SCHEMA_LOCKS[key] = threading.RLock()
        return _SCHEMA_LOCKS[key]


@contextmanager
def pg_write_lock(schema: str, *, timeout: float | None = None):
    wait = float(os.environ.get('SME_PG_WRITE_LOCK_SEC', '30') or 30)
    if timeout is not None:
        wait = timeout
    lock = schema_lock(schema)
    acquired = lock.acquire(timeout=max(0.1, wait))
    if not acquired:
        raise psycopg.OperationalError('schema write lock timeout')  # type: ignore
    try:
        yield
    finally:
        lock.release()


def _is_insert(sql: str) -> bool:
    return bool(_INSERT_RE.match(sql or ''))


def _read_lastval(executor) -> int:
    """Đọc sequence vừa dùng sau INSERT (tương đương sqlite lastrowid)."""
    try:
        row = executor.execute('SELECT lastval()').fetchone()
        if row is None:
            return 0
        return int(row[0] if not isinstance(row, dict) else list(row.values())[0])
    except Exception:
        return 0


class PgCursor:
    """Cursor psycopg — rewrite SQL SQLite → PostgreSQL + lastrowid."""

    __slots__ = ('_cur', '_schema', 'lastrowid', 'rowcount', 'description')

    def __init__(self, cur, schema: str):
        self._cur = cur
        self._schema = schema
        self.lastrowid = 0
        self.rowcount = -1
        self.description = None

    def execute(self, query: str, params: Any = None):
        sql = rewrite_sql_for_postgres(query, schema=self._schema)
        want_id = _is_insert(query) and 'RETURNING' not in sql.upper()
        is_write = want_id or bool(_WRITE_SQL_RE.match(query or ''))

        # SELECT / đọc: không bọc SAVEPOINT — RELEASE sẽ nuốt result → fetchone lỗi
        # "command status: RELEASE" và worker treo / Nginx 504.
        if not is_write:
            try:
                if params is None:
                    self._cur.execute(sql)
                else:
                    self._cur.execute(sql, params)
                self.description = getattr(self._cur, 'description', None)
                self.rowcount = getattr(self._cur, 'rowcount', -1)
                return self
            except Exception:
                # Postgres: lỗi 1 câu → abort cả transaction; phải rollback
                # nếu không request sau báo "current transaction is aborted"
                try:
                    self._cur.connection.rollback()
                except Exception:
                    pass
                raise

        try:
            self._cur.execute('SAVEPOINT sme_stmt')
            used_returning = False
            if want_id:
                try:
                    sql_ret = sql.rstrip().rstrip(';') + ' RETURNING id'
                    if params is None:
                        self._cur.execute(sql_ret)
                    else:
                        self._cur.execute(sql_ret, params)
                    used_returning = True
                except Exception:
                    self._cur.execute('ROLLBACK TO SAVEPOINT sme_stmt')
                    self._cur.execute('SAVEPOINT sme_stmt')
                    used_returning = False
            if not used_returning:
                if params is None:
                    self._cur.execute(sql)
                else:
                    self._cur.execute(sql, params)
            if _is_insert(query):
                if used_returning:
                    row = self._cur.fetchone()
                    if row is None:
                        self.lastrowid = 0
                    else:
                        self.lastrowid = int(
                            row[0] if not isinstance(row, dict) else list(row.values())[0]
                        )
                else:
                    self.lastrowid = _read_lastval(self._cur)
            self._cur.execute('RELEASE SAVEPOINT sme_stmt')
            self.description = getattr(self._cur, 'description', None)
            self.rowcount = getattr(self._cur, 'rowcount', -1)
            return self
        except Exception:
            try:
                self._cur.execute('ROLLBACK TO SAVEPOINT sme_stmt')
            except Exception:
                try:
                    self._cur.connection.rollback()
                except Exception:
                    pass
            raise

    def executemany(self, query: str, params_seq):
        sql = rewrite_sql_for_postgres(query, schema=self._schema)
        self._cur.executemany(sql, params_seq)
        self.rowcount = getattr(self._cur, 'rowcount', -1)
        return self

    def executescript(self, script: str):
        for stmt in str(script or '').split(';'):
            sql = stmt.strip()
            if not sql:
                continue
            self.execute(sql)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, size=None):
        if size is None:
            return self._cur.fetchmany()
        return self._cur.fetchmany(size)

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class PgConnection:
    """Wrapper psycopg với ``execute()`` kiểu SQLite và search_path theo tenant."""

    __slots__ = (
        '_conn', '_schema', '_sme_backend', '_sme_pg_schema', '_closed', '_from_pool',
        'lastrowid', 'row_factory',
    )

    def __init__(self, conn, schema: str, *, from_pool: bool = True):
        self._conn = conn
        self._schema = sanitize_pg_schema(schema)
        self._sme_backend = BACKEND_POSTGRES
        self._sme_pg_schema = self._schema
        self._closed = False
        self._from_pool = from_pool
        self.lastrowid = 0
        self.row_factory = None
        self._set_search_path()

    def _set_search_path(self) -> None:
        reg = (os.environ.get('SME_PG_REGISTRY_SCHEMA') or 'public').strip() or 'public'
        paths = [self._schema]
        if self._schema != reg:
            paths.append(reg)
        paths.append('public')
        ordered = []
        seen = set()
        for p in paths:
            if p not in seen:
                ordered.append(p)
                seen.add(p)
        self._conn.execute(
            pg_sql.SQL('SET search_path TO {}').format(
                pg_sql.SQL(', ').join(pg_sql.Identifier(p) for p in ordered)
            )
        )

    def execute(self, query: str, params: Any = None):
        """Tương thích sqlite3: trả cursor; hỗ trợ lastrowid sau INSERT."""
        cur = self.cursor()
        cur.execute(query, params)
        self.lastrowid = cur.lastrowid
        return cur

    def executescript(self, script: str):
        for stmt in str(script or '').split(';'):
            sql = stmt.strip()
            if not sql:
                continue
            self.execute(sql)
        return self

    def cursor(self):
        return PgCursor(self._conn.cursor(), self._schema)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    @property
    def autocommit(self):
        return self._conn.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self._conn.autocommit = value

    @property
    def in_transaction(self) -> bool:
        try:
            status = self._conn.info.transaction_status
            return status not in (psycopg.pq.TransactionStatus.IDLE,)  # type: ignore
        except Exception:
            return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        conn = self._conn
        if self._from_pool:
            # Luôn rollback trước khi trả pool — tránh connection "aborted" làm hỏng request sau
            try:
                if not getattr(conn, 'closed', False):
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    get_pool().putconn(conn)
                else:
                    try:
                        get_pool().putconn(conn, close=True)
                    except TypeError:
                        get_pool().putconn(conn)
            except Exception as exc:
                logger.warning('putconn failed: %s', exc)
                try:
                    conn.close()
                except Exception:
                    pass
            return
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self.rollback()
        finally:
            self.close()
        return False


class _PgRequestScoped:
    """Proxy: close() no-op trong request; teardown mới trả connection về pool."""

    __slots__ = ('_inner',)

    def __init__(self, inner: PgConnection):
        self._inner = inner

    def close(self):
        return None

    def _real_close(self):
        self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        if name == '_inner':
            object.__setattr__(self, name, value)
            return
        # Cho phép gán row_factory = sqlite3.Row (no-op trên PG)
        if name == 'row_factory':
            setattr(self._inner, name, value)
            return
        setattr(self._inner, name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open_pg_request(schema: str):
    """Connection Postgres tái sử dụng trong request Flask."""
    return _PgRequestScoped(open_pg(schema=schema))


def open_pg(schema: str | None = None, *, db_path: str | None = None, tenant_id: str | None = None):
    """Mở connection Postgres với search_path = schema tenant."""
    _require_psycopg()
    sch = schema or pg_schema_from_db_path(db_path, tenant_id=tenant_id)
    pool = get_pool()
    raw = pool.getconn()
    try:
        return PgConnection(raw, sch, from_pool=True)
    except Exception:
        pool.putconn(raw)
        raise


def pg_write_retry(fn, *, retries: int | None = None, label: str = 'pg_write'):
    total = int(os.environ.get('SME_PG_WRITE_RETRIES', '8') or 8)
    if retries is not None:
        total = retries
    last_exc = None
    for attempt in range(max(1, total)):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not is_locked_error(exc) or attempt >= total - 1:
                raise
            sleep_s = min(0.08 * (2 ** attempt) + random.uniform(0, 0.08), 2.5)
            logger.warning('%s retry %s/%s: %s', label, attempt + 1, total, exc)
            time.sleep(sleep_s)
    if last_exc:
        raise last_exc
    return None


def ensure_pg_schema(schema: str) -> None:
    """Tạo schema tenant nếu chưa có."""
    sch = sanitize_pg_schema(schema)
    if sch in _SCHEMA_READY:
        return
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(pg_sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(pg_sql.Identifier(sch)))
        conn.commit()
    _SCHEMA_READY.add(sch)


def close_pg_pool() -> None:
    global _POOL
    with _POOL_GUARD:
        if _POOL is not None:
            try:
                _POOL.close()
            except Exception:
                pass
            _POOL = None
