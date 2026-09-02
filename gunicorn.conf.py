# -*- coding: utf-8 -*-
"""
Cấu hình Gunicorn — production VPS (Nginx → 127.0.0.1:8000).

Chạy:
  gunicorn -c gunicorn.conf.py app:app

Biến môi trường (.env):
  GUNICORN_BIND=127.0.0.1:8000
  GUNICORN_WORKERS=2          # SQLite: 2; Postgres: 2*CPU+1
  GUNICORN_THREADS=3          # gthread: I/O chờ SQLite
  GUNICORN_WORKER_CLASS=gthread
  GUNICORN_TIMEOUT=90         # < proxy_read_timeout Nginx (khuyến nghị 120s)
  SME_SQLITE_BUSY_TIMEOUT_MS=5000
  SME_SQLITE_WRITE_RETRIES=4
  SME_SKIP_RUNTIME_MIGRATE=1
  SME_DB_BACKEND=sqlite|postgres
  SME_CRM_ANALYTICS_BUDGET_SEC=8
"""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

bind = os.getenv('GUNICORN_BIND', '127.0.0.1:8000')


def _resolve_db_backend() -> str:
    """Khớp logic db/dialect.py — tránh chỉ set DATABASE_URL mà Gunicorn vẫn dùng profile SQLite."""
    raw = (os.getenv('SME_DB_BACKEND') or os.getenv('DATABASE_BACKEND') or '').strip().lower()
    if raw in ('postgres', 'postgresql', 'pg'):
        return 'postgres'
    if raw == 'sqlite':
        return 'sqlite'
    for env_key in ('DATABASE_URL', 'SME_PG_URL'):
        url = (os.getenv(env_key) or '').strip().lower()
        if url.startswith(('postgres://', 'postgresql://', 'postgresql+psycopg://')):
            return 'postgres'
    return 'sqlite'


_backend = _resolve_db_backend()
_cpu = max(1, multiprocessing.cpu_count())
if _backend == 'postgres':
    # PG: pool trong app — sync worker, nhiều process hơn SQLite file lock
    _default_workers = max(4, min(_cpu * 2 + 1, 12))
    _default_threads = 1
    _default_class = 'sync'
else:
    # SQLite: ít worker hơn → giảm database is locked / 504
    _default_workers = min(2, max(2, _cpu))
    _default_threads = 3
    _default_class = 'gthread'

workers = int(os.getenv('GUNICORN_WORKERS', str(_default_workers)))
threads = int(os.getenv('GUNICORN_THREADS', str(_default_threads)))
worker_class = os.getenv('GUNICORN_WORKER_CLASS', _default_class)

# Timeout worker < Nginx proxy_read_timeout để Gunicorn kill treo trước khi Nginx 504
timeout = int(os.getenv('GUNICORN_TIMEOUT', '90'))
keepalive = int(os.getenv('GUNICORN_KEEPALIVE', '5'))
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT', '30'))

# Recycle worker — PG: cao hơn để giữ cache bootstrap/schema; SQLite: thấp hơn tránh leak
_default_max_requests = '3000' if _backend == 'postgres' else '500'
max_requests = int(os.getenv('GUNICORN_MAX_REQUESTS', _default_max_requests))
max_requests_jitter = int(os.getenv('GUNICORN_MAX_REQUESTS_JITTER', '200' if _backend == 'postgres' else '50'))

# PG: preload giảm cold-start bootstrap; SQLite: tắt (scheduler leader gate trong app.py)
_default_preload = '1' if _backend == 'postgres' else '0'
preload_app = os.getenv('GUNICORN_PRELOAD', _default_preload).strip().lower() in ('1', 'true', 'yes')

accesslog = os.getenv('GUNICORN_ACCESS_LOG', '-')
errorlog = os.getenv('GUNICORN_ERROR_LOG', '-')
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')

forwarded_allow_ips = os.getenv('GUNICORN_FORWARDED_ALLOW_IPS', '127.0.0.1')

# Heartbeat worker trên RAM — tránh /tmp đầy làm worker treo
_shm = Path('/dev/shm')
if _shm.is_dir() and os.access(_shm, os.W_OK):
    worker_tmp_dir = os.getenv('GUNICORN_WORKER_TMP_DIR', '/dev/shm')
else:
    worker_tmp_dir = os.getenv('GUNICORN_WORKER_TMP_DIR', None)
