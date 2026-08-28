# -*- coding: utf-8 -*-
"""
Cấu hình Gunicorn — production VPS.

Chạy:
  gunicorn -c gunicorn.conf.py app:app

Biến môi trường (.env):
  GUNICORN_BIND=127.0.0.1:8000
  GUNICORN_WORKERS=4          # Postgres: 2*CPU+1; SQLite: 2–4
  GUNICORN_THREADS=1
  GUNICORN_TIMEOUT=120
  SME_DB_BACKEND=postgres     # khuyến nghị production + ESS nhiều NV
"""
from __future__ import annotations

import multiprocessing
import os

bind = os.getenv('GUNICORN_BIND', '127.0.0.1:8000')

_backend = (os.getenv('SME_DB_BACKEND') or os.getenv('DATABASE_BACKEND') or 'sqlite').strip().lower()
_default_workers = multiprocessing.cpu_count() * 2 + 1
if _backend in ('postgres', 'postgresql', 'pg'):
    _default_workers = max(4, _default_workers)
else:
    # SQLite: ít worker hơn — giảm tranh khóa ghi (ESS check-in, HRM)
    _default_workers = min(4, max(2, multiprocessing.cpu_count()))

workers = int(os.getenv('GUNICORN_WORKERS', str(_default_workers)))
threads = int(os.getenv('GUNICORN_THREADS', '1'))
worker_class = os.getenv('GUNICORN_WORKER_CLASS', 'sync')

timeout = int(os.getenv('GUNICORN_TIMEOUT', '120'))
keepalive = int(os.getenv('GUNICORN_KEEPALIVE', '5'))
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT', '30'))

max_requests = int(os.getenv('GUNICORN_MAX_REQUESTS', '2000'))
max_requests_jitter = int(os.getenv('GUNICORN_MAX_REQUESTS_JITTER', '200'))

# False: tránh scheduler/APScheduler chạy N lần (app.py đã gate leader)
preload_app = os.getenv('GUNICORN_PRELOAD', '0').strip().lower() in ('1', 'true', 'yes')

accesslog = os.getenv('GUNICORN_ACCESS_LOG', '-')
errorlog = os.getenv('GUNICORN_ERROR_LOG', '-')
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')

forwarded_allow_ips = os.getenv('GUNICORN_FORWARDED_ALLOW_IPS', '127.0.0.1')
