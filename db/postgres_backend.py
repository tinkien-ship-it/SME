"""PostgreSQL backend — schema-per-tenant, API tương thích sqlite3.Connection.execute()."""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
import weakref
from contextlib import contextmanager
# FIX: Đã thêm Optional vào import
from typing import Any, Optional

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
            # Mỗi Gunicorn worker là một process và có một pool riêng.
            # Pool 4..50 với 5 workers có thể giữ tối thiểu 20 và lý thuyết
            # mở tới 250 connection. Mặc định mới ưu tiên pool nhỏ, co giãn.
            min_size = int(os.environ.get('SME_PG_POOL_MIN', '1') or 1)
            max_size = int(os.environ.get('SME_PG_POOL_MAX', '8') or 8)
            timeout = float(os.environ.get('SME_PG_POOL_TIMEOUT', '5') or 5)
            max_idle = float(os.environ.get('SME_PG_POOL_MAX_IDLE', '60') or 60)
            max_lifetime = float(
                os.environ.get('SME_PG_POOL_MAX_LIFETIME', '1800') or 1800
            )

            min_size = max(0, min_size)
            max_size = max(1, max_size)
            if min_size > max_size:
                min_size = max_size

            kwargs = {
                'conninfo': database_url(),
                'min_size': min_size,
                'max_size': max_size,
                'timeout': max(0.5, timeout),
                'kwargs': {
                    'row_factory': compat_row_factory,
                    'autocommit': False,
                    'connect_timeout': 10,
                },
                'open': True,
                'max_idle': max(1.0, max_idle),
                'max_lifetime': max(60.0, max_lifetime),
            }

            check_fn = getattr(ConnectionPool, 'check_connection', None)
            if check_fn is not None:
                kwargs['check'] = check_fn

            try:
                _POOL = ConnectionPool(**kwargs)
            except TypeError:
                # Tương thích psycopg_pool cũ.
                kwargs.pop('max_idle', None)
                kwargs.pop('max_lifetime', None)
                _POOL = ConnectionPool(**kwargs)

            logger.info(
                'PostgreSQL pool initialized pid=%s min=%s max=%s timeout=%ss',
                os.getpid(), min_size, max_size, max(0.5, timeout),
            )
        return _POOL



def _pool_stats(pool=None) -> dict:
    """Lấy thống kê pool an toàn để chẩn đoán PoolTimeout."""
    target = pool or _POOL
    if target is None:
        return {}
    try:
        getter = getattr(target, 'get_stats', None)
        return dict(getter() or {}) if getter else {}
    except Exception:
        return {}


def _log_pool_state(level: int, message: str, *, pool=None, schema: str | None = None) -> None:
    logger.log(
        level,
        '%s (pid=%s schema=%s stats=%s)',
        message,
        os.getpid(),
        schema or '?',
        _pool_stats(pool),
    )


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


def _row_first_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    try:
        return row[0]
    except Exception:
        return None


def _coerce_lastrowid(val: Any) -> int:
    """Ép id sau INSERT — None/invalid → 0 (tránh int(None) crash)."""
    if val is None:
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _read_lastval(executor) -> int:
    """
    FIX: Hàm này gốc có rủi ro làm hỏng transaction chính.
    Đã được bọc trong một helper 'safe' trong class chính.
    """
    try:
        # SQLite tương thích không dùng savepoint ở đây để tránh lồng nhau
        row = executor.execute('SELECT lastval()').fetchone()
        return _coerce_lastrowid(_row_first_value(row))
    except Exception:
        return 0

def _sanitize_params(params):
    """Chuyển đổi các chuỗi rỗng '' thành None để PostgreSQL nhận diện là NULL."""
    if params is None:
        return None
    if isinstance(params, (list, tuple)):
        return type(params)(None if p == "" else p for p in params)
    if isinstance(params, dict):
        return {k: (None if v == "" else v) for k, v in params.items()}
    return params

class PgCursor:
    """Cursor psycopg — rewrite SQL SQLite → PostgreSQL + lastrowid."""

    __slots__ = ('_cur', '_schema', 'lastrowid', 'rowcount', 'description')

    def __init__(self, cur, schema: str):
        self._cur = cur
        self._schema = schema
        self.lastrowid: int = 0
        self.rowcount: int = -1
        # FIX: Sửa type hint description sau khi import Optional
        self.description: Optional[Any] = None

    def execute(self, query: str, params: Any = None):
        from db_utils import recover_pg_transaction
        recover_pg_transaction(self._cur.connection)

        sql = rewrite_sql_for_postgres(query, schema=self._schema)
        # Fix sanitize params trước khi execute
        params = _sanitize_params(params)
        
        want_id = _is_insert(query) and 'RETURNING' not in sql.upper()
        is_write = want_id or bool(_WRITE_SQL_RE.match(query or ''))

        def _full_rollback():
            try:
                self._cur.connection.rollback()
            except Exception:
                pass

        def _read_lastval_safe(cur) -> int:
            """FIX: Đọc lastval() an toàn, không làm hỏng transaction nếu bảng không có sequence."""
            try:
                cur.execute("SAVEPOINT sme_lastval")
                cur.execute("SELECT lastval()")
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sme_lastval")
                return _coerce_lastrowid(_row_first_value(row))
            except Exception:
                # Nếu không có sequence, hoặc sequence chưa dùng, lastval() sẽ lỗi.
                # Rollback savepoint để xóa trạng thái InFailedSqlTransaction.
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sme_lastval")
                except Exception:
                    pass
                return 0

        # ===== Case 1: SELECT / Read-only (Không bọc SAVEPOINT) =====
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
                _full_rollback()
                raise

        # ===== Case 2: WRITE Operations (Bọc trong SAVEPOINT) =====
        try:
            self._cur.execute('SAVEPOINT sme_stmt')
        except Exception:
            _full_rollback()
            raise

        executed_successfully = False
        
        # FIX: Tách exception gốc khỏi exception của việc rollback
        original_exception = None

        # Thử chèn 'RETURNING id' nếu là câu INSERT chưa có RETURNING
        if want_id:
            try:
                sql_ret = sql.rstrip().rstrip(';') + ' RETURNING id'
                if params is None:
                    self._cur.execute(sql_ret)
                else:
                    self._cur.execute(sql_ret, params)
                
                # FIX: Lấy id vừa tạo ngay lập tức để tránh cursor bị kẹt dữ liệu cũ
                row = self._cur.fetchone()
                rid = _coerce_lastrowid(_row_first_value(row))
                
                # FIX: Nếu không có cột id, hoặc RETURNING fail không mong muốn, fallback về lastval()
                if rid == 0:
                    # Rủi ro cao nhưng fallback cuối cùng
                    rid = _read_lastval_safe(self._cur)
                self.lastrowid = rid

                executed_successfully = True
            except Exception as e:
                # Lưu exception gốc
                original_exception = e
                # Nếu câu có RETURNING id thất bại (VD: bảng không có cột 'id')
                # Rollback về Savepoint để xóa trạng thái InFailedSqlTransaction
                try:
                    self._cur.execute('ROLLBACK TO SAVEPOINT sme_stmt')
                    # Tạo lại Savepoint để chuẩn bị cho câu fallback gốc
                    self._cur.execute('SAVEPOINT sme_stmt')
                except Exception:
                    # Nếu rollback thất bại, ném exception gốc, transaction đã chết
                    _full_rollback()
                    raise original_exception
                executed_successfully = False

        # Thực thi câu lệnh gốc (nếu không dùng được RETURNING id hoặc không phải INSERT lấy ID)
        if not executed_successfully:
            try:
                if params is None:
                    self._cur.execute(sql)
                else:
                    self._cur.execute(sql, params)

                if _is_insert(query):
                    self.lastrowid = _read_lastval_safe(self._cur)
            except Exception as fallback_exc:
                # Nếu câu gốc cũng fail, rollback và ném lỗi câu gốc
                try:
                    self._cur.execute('ROLLBACK TO SAVEPOINT sme_stmt')
                except Exception:
                    pass
                _full_rollback()
                raise fallback_exc

        # Release Savepoint sau khi thực thi thành công
        try:
            self._cur.execute('RELEASE SAVEPOINT sme_stmt')
        except Exception:
            _full_rollback()
            raise

        self.description = getattr(self._cur, 'description', None)
        self.rowcount = getattr(self._cur, 'rowcount', -1)
        return self

    def executemany(self, query: str, params_seq):
        from db_utils import recover_pg_transaction
        recover_pg_transaction(self._cur.connection)
        sql = rewrite_sql_for_postgres(query, schema=self._schema)
        try:
            self._cur.executemany(sql, params_seq)
            self.rowcount = getattr(self._cur, 'rowcount', -1)
            return self
        except Exception:
            try:
                self._cur.connection.rollback()
            except Exception:
                pass
            raise

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


def _pool_putconn_finalizer(pool_obj, conn_obj) -> None:
    """Safety net: trả connection về ĐÚNG pool đã checkout khi wrapper bị GC."""
    try:
        if conn_obj is None:
            return
        if getattr(conn_obj, 'closed', False):
            return
        try:
            conn_obj.rollback()
        except Exception:
            pass
        if pool_obj is not None:
            try:
                pool_obj.putconn(conn_obj)
                return
            except Exception:
                pass
        conn_obj.close()
    except Exception:
        try:
            conn_obj.close()
        except Exception:
            pass


class PgConnection:
    """Wrapper psycopg với ``execute()`` kiểu SQLite và search_path theo tenant."""

    __slots__ = (
        '__weakref__',
        '_conn', '_schema', '_sme_backend', '_sme_pg_schema', '_closed', '_from_pool',
        '_pool', '_finalizer', 'lastrowid', 'row_factory',
    )

    def __init__(self, conn, schema: str, *, from_pool: bool = True, pool=None):
        self._conn = conn
        self._schema = sanitize_pg_schema(schema)
        self._sme_backend = BACKEND_POSTGRES
        self._sme_pg_schema = self._schema
        self._closed = False
        self._from_pool = from_pool
        self._pool = pool
        self._finalizer = None
        self.lastrowid = 0
        self.row_factory = None
        self._set_search_path()

        # SET search_path với autocommit=False mở transaction. Commit ngay vì
        # ở đây chưa có nghiệp vụ; tránh checkout xong đã "idle in transaction".
        try:
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise

        if from_pool:
            try:
                self._finalizer = weakref.finalize(
                    self,
                    _pool_putconn_finalizer,
                    pool,
                    conn,
                )
            except TypeError:
                # Class không hỗ trợ weakref — bỏ safety net, vẫn dùng được
                self._finalizer = None

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
        from db_utils import recover_pg_transaction
        recover_pg_transaction(self)
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
        if self._finalizer is not None:
            try:
                self._finalizer.detach()
            except Exception:
                pass
            self._finalizer = None
        conn = self._conn
        if self._from_pool:
            # Rollback trước khi trả pool để không giữ transaction/request cũ.
            # Luôn trả về CHÍNH pool đã checkout connection này.
            pool = self._pool
            try:
                if not getattr(conn, 'closed', False):
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if pool is not None:
                        pool.putconn(conn)
                    else:
                        conn.close()
                elif pool is not None:
                    try:
                        pool.putconn(conn, close=True)
                    except TypeError:
                        pool.putconn(conn)
            except Exception as exc:
                logger.warning(
                    'putconn failed pid=%s schema=%s: %s',
                    os.getpid(),
                    self._schema,
                    exc,
                )
                try:
                    conn.close()
                except Exception:
                    pass
            finally:
                self._pool = None
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
    """Mở connection Postgres với search_path = schema tenant.

    PoolTimeout không reset pool đang phục vụ request khác. Reset pool khi còn
    connection checked-out có thể làm ownership rối và gây lỗi dây chuyền.
    """
    _require_psycopg()
    from psycopg_pool import PoolTimeout

    sch = sanitize_pg_schema(
        schema or pg_schema_from_db_path(db_path, tenant_id=tenant_id)
    )
    pool = get_pool()

    try:
        raw = pool.getconn()
    except PoolTimeout:
        _log_pool_state(
            logging.ERROR,
            'PoolTimeout khi checkout PostgreSQL; giữ nguyên pool và retry 1 lần',
            pool=pool,
            schema=sch,
        )

        try:
            check_pool = getattr(pool, 'check', None)
            if callable(check_pool):
                check_pool()
        except Exception as exc:
            logger.warning('PostgreSQL pool.check failed: %s', exc)

        retry_delay = float(
            os.environ.get('SME_PG_POOL_RETRY_DELAY', '0.25') or 0.25
        )
        time.sleep(max(0.05, min(retry_delay, 2.0)))

        try:
            raw = pool.getconn()
        except PoolTimeout:
            _log_pool_state(
                logging.ERROR,
                'PoolTimeout lần 2; không reset pool để tránh ảnh hưởng request khác',
                pool=pool,
                schema=sch,
            )
            raise

    try:
        return PgConnection(raw, sch, from_pool=True, pool=pool)
    except Exception:
        try:
            if not getattr(raw, 'closed', False):
                try:
                    raw.rollback()
                except Exception:
                    pass
                pool.putconn(raw)
            else:
                try:
                    pool.putconn(raw, close=True)
                except TypeError:
                    pool.putconn(raw)
        except Exception:
            try:
                raw.close()
            except Exception:
                pass
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
    """Đóng pool hiện tại khi shutdown / thao tác quản trị chủ động."""
    global _POOL
    with _POOL_GUARD:
        pool = _POOL
        if pool is None:
            return

        _log_pool_state(
            logging.INFO,
            'Closing PostgreSQL pool',
            pool=pool,
        )

        # Tách global trước khi close để checkout mới không lấy pool đang đóng.
        _POOL = None
        try:
            pool.close()
        except Exception as exc:
            logger.warning('close PostgreSQL pool failed: %s', exc)
