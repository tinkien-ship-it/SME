# -*- coding: utf-8 -*-
"""Lịch đồng bộ HĐ mua (portal Mắt Bảo) — quét từng tenant đã bật auto_sync_purchase."""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from db_utils import MAIN_DB_PATH, _normalize_db_path, get_main_db_connection, open_sqlite
from Services.invoice_schedule import claim_job_run, finish_job_run, now_vn

logger = logging.getLogger(__name__)

PURCHASE_SYNC_JOB_ID = 'purchase_invoice_sync'
SCHEDULE_HOURS = (0, 9, 15)  # 00:00, 09:00, 15:00 giờ VN
SCHEDULE_TZ = ZoneInfo('Asia/Ho_Chi_Minh')
INCLUDE_MAIN_DB = os.getenv('PURCHASE_SYNC_INCLUDE_MAIN', '0').strip().lower() in ('1', 'true', 'yes')

_PURCHASE_CONFIG_SQL = """
    SELECT * FROM invoice_settings
    WHERE COALESCE(is_active, 0) IN (1, '1')
      AND LOWER(TRIM(COALESCE(provider_name, ''))) = 'matbao'
      AND COALESCE(auto_sync_purchase, 1) IN (1, '1')
    ORDER BY updated_at DESC
    LIMIT 1
"""


def _months_to_sync(reference: datetime | None = None) -> list[str]:
    """Tháng hiện tại + tháng trước (MM/YYYY)."""
    ref = reference or now_vn()
    months = [ref.strftime('%m/%Y')]
    if ref.month == 1:
        prev = ref.replace(year=ref.year - 1, month=12, day=1)
    else:
        prev = ref.replace(month=ref.month - 1, day=1)
    months.append(prev.strftime('%m/%Y'))
    return months


def _config_for_db(db_path: str) -> dict | None:
    if not db_path or not os.path.exists(db_path):
        return None
    conn = open_sqlite(db_path)
    try:
        conn.row_factory = sqlite3.Row
        # Đảm bảo cột auto_sync_purchase tồn tại
        try:
            from db.init import ensure_invoice_settings_schema
            ensure_invoice_settings_schema(conn)
        except Exception:
            pass
        row = conn.execute(_PURCHASE_CONFIG_SQL).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        logger.warning('purchase sync config %s: %s', db_path, exc)
        return None
    finally:
        conn.close()


def iter_purchase_sync_targets() -> list[dict]:
    candidates: list[tuple[str, str]] = []
    names: dict[str, str] = {}

    conn = get_main_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT tenant_id, business_name, db_path FROM tenants "
            "WHERE COALESCE(is_active, 0) IN (1, '1')"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.error('iter_purchase_sync_targets: %s', exc)
        rows = []
    finally:
        conn.close()

    for row in rows:
        tenant_id = (row['tenant_id'] or '').strip()
        db_path = _normalize_db_path(row['db_path'])
        names[tenant_id] = row['business_name'] or tenant_id
        if tenant_id and db_path:
            candidates.append((tenant_id, db_path))

    if INCLUDE_MAIN_DB:
        candidates.append(('__main__', MAIN_DB_PATH))
        names['__main__'] = 'Main DB'

    targets = []
    seen = set()
    for tenant_id, db_path in candidates:
        if db_path in seen:
            continue
        seen.add(db_path)
        config = _config_for_db(db_path)
        if not config:
            continue
        api_key = (config.get('api_key') or '').strip()
        api_url = (config.get('purchase_api_url') or config.get('api_url') or '').strip()
        if not api_key or not api_url:
            continue
        targets.append({
            'tenant_id': tenant_id,
            'business_name': names.get(tenant_id, tenant_id),
            'db_path': db_path,
            'config': config,
        })
    return targets


def sync_tenant_db(db_path: str, config: dict, months: list[str] | None = None) -> dict:
    """Đồng bộ portal cho một DB tenant (không cần Flask request).

    Quan trọng: KHÔNG giữ connection SQLite trong lúc gọi HTTP Matbao (timeout 90s)
    — đó là nguyên nhân chính ``database is locked`` khi user lưu HĐĐT / seed roles.
    """
    from Services.purchase_invoice_sync import SOURCE_PORTAL, MatbaoPurchaseProvider, persist_invoices

    months = months or _months_to_sync()
    provider = MatbaoPurchaseProvider(config)
    totals = {'new_inserted': 0, 'duplicates_skipped': 0, 'total_received': 0, 'months': []}
    errors = []

    for month in months:
        # 1) HTTP ngoài — không mở DB
        result = provider.fetch_invoices(month, source=SOURCE_PORTAL)
        if not result.get('success'):
            errors.append(f'{month}: {result.get("error")}')
            continue
        invoices = result.get('invoices') or []
        # 2) Chỉ mở DB khi ghi — timeout ngắn, nhường user nếu đang bận
        try:
            conn = open_sqlite(db_path, timeout=5.0)
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower():
                errors.append(f'{month}: database is locked — thử lại sau')
                continue
            raise
        try:
            summary = persist_invoices(conn, invoices)
            totals['new_inserted'] += summary['new_inserted']
            totals['duplicates_skipped'] += summary['duplicates_skipped']
            totals['total_received'] += summary['total_received']
            totals['months'].append({'month': month, **summary})
        finally:
            conn.close()

    ok = totals['new_inserted'] > 0 or not errors or totals['total_received'] > 0
    if errors and totals['total_received'] == 0 and totals['new_inserted'] == 0:
        ok = False
    return {
        'success': ok,
        'summary': totals,
        'errors': errors,
    }


def run_purchase_sync_for_all_tenants() -> dict:
    """Entry point scheduler — chỉ portal (không captcha). Chạy 3 khung: 00 / 09 / 15."""
    now = now_vn()
    # Khóa theo ngày+giờ để 3 lượt/ngày không chặn nhau; nhiều worker vẫn không chạy trùng 1 slot
    run_key = now.strftime('%Y-%m-%d-%H')
    if not claim_job_run(PURCHASE_SYNC_JOB_ID, run_key):
        logger.info('purchase sync skipped — already claimed for %s', run_key)
        return {'skipped': True, 'reason': 'already_claimed', 'run_key': run_key}

    targets = iter_purchase_sync_targets()
    months = _months_to_sync(now)
    results = []
    for t in targets:
        try:
            out = sync_tenant_db(t['db_path'], t['config'], months=months)
            results.append({
                'tenant_id': t['tenant_id'],
                'business_name': t['business_name'],
                **out,
            })
            logger.info(
                'purchase sync [%s]: new=%s recv=%s errors=%s',
                t['tenant_id'],
                (out.get('summary') or {}).get('new_inserted'),
                (out.get('summary') or {}).get('total_received'),
                out.get('errors'),
            )
        except Exception as exc:
            logger.exception('purchase sync failed for %s', t['tenant_id'])
            results.append({
                'tenant_id': t['tenant_id'],
                'success': False,
                'error': str(exc),
            })

    summary = (
        f"slot={run_key} tenants={len(targets)} "
        f"ok={sum(1 for r in results if r.get('success'))} "
        f"new={sum((r.get('summary') or {}).get('new_inserted', 0) for r in results)}"
    )
    finish_job_run(summary, PURCHASE_SYNC_JOB_ID, run_key)
    return {'tenants': len(targets), 'months': months, 'results': results, 'summary': summary, 'run_key': run_key}
