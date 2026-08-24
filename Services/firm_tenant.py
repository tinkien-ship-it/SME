# -*- coding: utf-8 -*-
"""Tenant đơn vị dịch vụ kế toán (DVKT) — nhiều DN thuê, không tài khoản login cho DN thuê."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from flask import session, url_for
from flask_bcrypt import check_password_hash, generate_password_hash

from db_utils import (
    BASE_DIR,
    MAIN_DB_PATH,
    force_close_request_db_if_path,
    get_db_connection,
    get_main_db_connection,
    has_request_context,
    open_sqlite,
    paths_same_db,
    sqlite_write_retry,
    _is_locked_error,
    _normalize_db_path,
    sqlite_commit,
)

logger = logging.getLogger(__name__)

_FIRM_SCHEMA_READY = False

TENANT_TYPE_STANDALONE = 'standalone'
TENANT_TYPE_FIRM = 'firm'

FIRM_ROLES = frozenset({'owner', 'chief_accountant', 'accountant', 'viewer'})
FIRM_ROLE_LABELS = {
    'owner': 'Chủ đơn vị',
    'chief_accountant': 'Kế Toán Trưởng',
    'accountant': 'Kế Toán Viên',
    'viewer': 'Người chỉ được phép xem',
}
CLIENT_ACCESS_ROLES = frozenset({'full', 'accounting', 'tax_only', 'view'})
CLIENT_ACCESS_ROLE_LABELS = {
    'full': 'Toàn quyền',
    'accounting': 'Kế toán phụ trách',
    'tax_only': 'Chỉ phụ trách thuế',
    'view': 'Chỉ được xem',
}

# Gói số DN thuê — max_clients = 0 → không giới hạn
FIRM_CLIENT_PACKAGES: tuple[dict[str, Any], ...] = (
    {'id': 'firm_10', 'max_clients': 10, 'label': '10 doanh nghiệp', 'sort': 10},
    {'id': 'firm_20', 'max_clients': 20, 'label': '20 doanh nghiệp', 'sort': 20},
    {'id': 'firm_30', 'max_clients': 30, 'label': '30 doanh nghiệp', 'sort': 30},
    {'id': 'firm_40', 'max_clients': 40, 'label': '40 doanh nghiệp', 'sort': 40},
    {'id': 'firm_50', 'max_clients': 50, 'label': '50 doanh nghiệp', 'sort': 50},
    {'id': 'firm_100', 'max_clients': 100, 'label': '100 doanh nghiệp', 'sort': 100},
    {'id': 'firm_200', 'max_clients': 200, 'label': '200 doanh nghiệp', 'sort': 200},
    {'id': 'firm_300', 'max_clients': 300, 'label': '300 doanh nghiệp', 'sort': 300},
    {'id': 'firm_400', 'max_clients': 400, 'label': '400 doanh nghiệp', 'sort': 400},
    {'id': 'firm_500', 'max_clients': 500, 'label': '500 doanh nghiệp', 'sort': 500},
    {'id': 'firm_600', 'max_clients': 600, 'label': '600 doanh nghiệp', 'sort': 600},
    {'id': 'firm_unlimited', 'max_clients': 0, 'label': 'Không giới hạn', 'sort': 999},
)
FIRM_PACKAGE_IDS = frozenset(p['id'] for p in FIRM_CLIENT_PACKAGES)
FIRM_LIMITED_MAX_VALUES = frozenset(p['max_clients'] for p in FIRM_CLIENT_PACKAGES if p['max_clients'] > 0)


def list_firm_client_packages() -> list[dict[str, Any]]:
    return [dict(p) for p in FIRM_CLIENT_PACKAGES]


def is_firm_unlimited(max_clients: int | None) -> bool:
    return max_clients is None or int(max_clients or 0) <= 0


def firm_max_clients_label(max_clients: int | None) -> str:
    if is_firm_unlimited(max_clients):
        return 'Không giới hạn'
    return f'{int(max_clients)} doanh nghiệp'


def resolve_firm_package(
    package_id: str | None = None,
    max_clients: int | None = None,
) -> dict[str, Any]:
    """Chuẩn hóa gói DVKT từ id hoặc số lượng DN."""
    if package_id:
        pid = str(package_id).strip()
        for p in FIRM_CLIENT_PACKAGES:
            if p['id'] == pid:
                return dict(p)
    if max_clients is not None:
        try:
            mc = int(max_clients)
        except (TypeError, ValueError):
            mc = 50
        if mc <= 0:
            for p in FIRM_CLIENT_PACKAGES:
                if p['max_clients'] == 0:
                    return dict(p)
        for p in FIRM_CLIENT_PACKAGES:
            if p['max_clients'] == mc:
                return dict(p)
        return {
            'id': f'firm_{mc}',
            'max_clients': mc,
            'label': f'{mc} doanh nghiệp',
            'sort': mc,
        }
    for p in FIRM_CLIENT_PACKAGES:
        if p['max_clients'] == 50:
            return dict(p)
    return dict(FIRM_CLIENT_PACKAGES[-2])


def normalize_firm_max_clients(value: int | str | None, default: int = 50) -> int:
    try:
        mc = int(value if value is not None else default)
    except (TypeError, ValueError):
        mc = default
    return 0 if mc < 0 else mc


def count_firm_active_clients(firm_tenant_id: str, conn: sqlite3.Connection | None = None) -> int:
    ensure_firm_schema(conn)
    own = conn is None
    conn = conn or get_main_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM firm_clients WHERE firm_tenant_id = ? AND status = 'active'",
            (firm_tenant_id.strip(),),
        ).fetchone()
        return int(row['c'] if row else 0)
    finally:
        if own:
            conn.close()


def firm_usage_summary(
    firm_tenant_id: str,
    max_clients: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Số DN đang phục vụ / giới hạn gói."""
    ensure_firm_schema(conn)
    own = conn is None
    conn = conn or get_main_db_connection()
    try:
        active = count_firm_active_clients(firm_tenant_id, conn=conn)
        if max_clients is None:
            row = conn.execute(
                "SELECT COALESCE(max_clients, 50) AS max_clients FROM tenants WHERE tenant_id = ?",
                (firm_tenant_id.strip(),),
            ).fetchone()
            max_clients = int(row['max_clients']) if row else 50
    finally:
        if own:
            conn.close()
    unlimited = is_firm_unlimited(max_clients)
    limit = None if unlimited else int(max_clients)
    at_capacity = (not unlimited) and active >= limit
    return {
        'active_clients': active,
        'max_clients': 0 if unlimited else limit,
        'unlimited': unlimited,
        'at_capacity': at_capacity,
        'usage_label': f'{active} / ∞' if unlimited else f'{active} / {limit}',
        'package_label': firm_max_clients_label(max_clients),
    }


def firm_can_add_client(
    firm_tenant_id: str,
    max_clients: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[bool, str]:
    usage = firm_usage_summary(firm_tenant_id, max_clients, conn=conn)
    if usage['at_capacity']:
        return False, f"Đã đạt giới hạn gói ({usage['usage_label']} doanh nghiệp)"
    return True, ''


def ensure_firm_schema(conn: sqlite3.Connection | None = None) -> None:
    global _FIRM_SCHEMA_READY
    own = conn is None
    if own and _FIRM_SCHEMA_READY:
        return
    conn = conn or get_main_db_connection()
    try:
        from db.schema_helpers import add_column_if_missing, execute_ddl, table_cols

        cols = table_cols(conn, 'tenants')
        add_column_if_missing(conn, 'tenants', 'tenant_type', "TEXT NOT NULL DEFAULT 'standalone'")
        add_column_if_missing(conn, 'tenants', 'max_clients', 'INTEGER DEFAULT 50')

        execute_ddl(conn, """
            CREATE TABLE IF NOT EXISTS firm_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                firm_tenant_id TEXT NOT NULL,
                login_email TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT NOT NULL DEFAULT '',
                firm_role TEXT NOT NULL DEFAULT 'accountant',
                is_active INTEGER NOT NULL DEFAULT 1,
                last_session_id TEXT,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(firm_tenant_id, login_email)
            )
        """)
        execute_ddl(conn, """
            CREATE INDEX IF NOT EXISTS idx_firm_users_email
            ON firm_users(LOWER(login_email))
        """)
        execute_ddl(conn, """
            CREATE TABLE IF NOT EXISTS firm_clients (
                client_id TEXT NOT NULL,
                firm_tenant_id TEXT NOT NULL,
                client_name TEXT NOT NULL,
                tax_code TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                representative_name TEXT NOT NULL DEFAULT '',
                accounting_regime TEXT NOT NULL DEFAULT 'SME_TT99',
                db_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (firm_tenant_id, client_id)
            )
        """)
        execute_ddl(conn, """
            CREATE INDEX IF NOT EXISTS idx_firm_clients_firm
            ON firm_clients(firm_tenant_id, status)
        """)
        execute_ddl(conn, """
            CREATE TABLE IF NOT EXISTS firm_user_client_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                firm_user_id INTEGER NOT NULL,
                firm_tenant_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                access_role TEXT NOT NULL DEFAULT 'accounting',
                is_active INTEGER NOT NULL DEFAULT 1,
                granted_at TEXT,
                UNIQUE(firm_user_id, client_id)
            )
        """)
        execute_ddl(conn, """
            CREATE TABLE IF NOT EXISTS firm_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                firm_tenant_id TEXT NOT NULL,
                firm_user_id INTEGER,
                client_id TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                ip_address TEXT,
                created_at TEXT
            )
        """)
        sqlite_commit(conn, label='firm_tenant')
        if own:
            _FIRM_SCHEMA_READY = True
    finally:
        if own:
            conn.close()


def _read_tenant_business_row(db_path: str) -> dict[str, Any]:
    """Đọc business_info — tái sử dụng connection request nếu cùng file DB."""
    abs_path = _normalize_db_path(db_path)
    if not abs_path or not os.path.isfile(abs_path):
        return {}

    def _query(target) -> dict[str, Any]:
        row = target.execute(
            'SELECT representative_name, business_name FROM business_info LIMIT 1',
        ).fetchone()
        return dict(row) if row else {}

    if has_request_context():
        from flask import g
        cached_path = getattr(g, '_sme_db_path', None)
        if cached_path and paths_same_db(cached_path, abs_path):
            try:
                return _query(get_db_connection())
            except Exception:
                pass
    with open_sqlite(abs_path) as conn:
        return _query(conn)


def _exclusive_tenant_db_write(abs_path: str, label: str, fn) -> None:
    """Ghi DB tenant khi có thể request đang giữ connection — tránh database is locked."""
    force_close_request_db_if_path(abs_path)

    def _run():
        with open_sqlite(abs_path) as conn:
            fn(conn)
            sqlite_commit(conn, label='firm_tenant')

    sqlite_write_retry(_run, label=label)

FIRM_CLIENT_REGIMES_DEFAULT = ('SME_MICRO_TT58', 'SME_TT99')
FIRM_CLIENT_REGIME_LABELS = {
    'SME_MICRO_TT58': 'Thông Tư 58/2026/TT-BTC (DNSN)',
    'SME_TT99': 'Thông Tư 99/2025/TT-BTC',
}


def firm_client_regime_label(code: str | None) -> str:
    key = (code or 'SME_TT99').strip().upper()
    return FIRM_CLIENT_REGIME_LABELS.get(key, key)


def get_firm_allowed_client_regimes(firm_tenant_id: str) -> tuple[str, ...]:
    """Chế độ kế toán DN thuê mà đơn vị được phép cung cấp dịch vụ."""
    from Services.subscription_service import get_tenant_record, parse_tenant_settings
    rec = get_tenant_record(firm_tenant_id, include_inactive=True)
    if not rec:
        return FIRM_CLIENT_REGIMES_DEFAULT
    settings = parse_tenant_settings(rec.get('settings'))
    raw = settings.get('firm_client_regimes') or FIRM_CLIENT_REGIMES_DEFAULT
    allowed = tuple(r for r in raw if r in FIRM_CLIENT_REGIMES_DEFAULT)
    return allowed or FIRM_CLIENT_REGIMES_DEFAULT


def get_tenant_type(tenant_id: str | None) -> str:
    if not tenant_id:
        return TENANT_TYPE_STANDALONE
    ensure_firm_schema()
    with get_main_db_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(tenant_type, 'standalone') AS tenant_type FROM tenants WHERE tenant_id = ?",
            (tenant_id.strip(),),
        ).fetchone()
    if not row:
        return TENANT_TYPE_STANDALONE
    t = (row['tenant_type'] or TENANT_TYPE_STANDALONE).strip().lower()
    return t if t in (TENANT_TYPE_STANDALONE, TENANT_TYPE_FIRM) else TENANT_TYPE_STANDALONE


def is_firm_tenant(tenant_id: str | None) -> bool:
    return get_tenant_type(tenant_id) == TENANT_TYPE_FIRM


def purge_firm_tenant_registry(firm_tenant_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Xóa metadata DVKT trên registry (firm_users, firm_clients, …)."""
    firm_tenant_id = (firm_tenant_id or '').strip()
    if not firm_tenant_id:
        return {'success': False, 'error': 'Thiếu firm_tenant_id'}
    own = conn is None
    conn = conn or get_main_db_connection()
    try:
        ensure_firm_schema(conn)
        conn.execute(
            "DELETE FROM firm_user_client_access WHERE firm_tenant_id = ?",
            (firm_tenant_id,),
        )
        conn.execute("DELETE FROM firm_users WHERE firm_tenant_id = ?", (firm_tenant_id,))
        conn.execute("DELETE FROM firm_clients WHERE firm_tenant_id = ?", (firm_tenant_id,))
        conn.execute("DELETE FROM firm_audit_log WHERE firm_tenant_id = ?", (firm_tenant_id,))
        if own:
            sqlite_commit(conn, label='firm_tenant')
        return {'success': True, 'firm_tenant_id': firm_tenant_id}
    except Exception as exc:
        if own:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.exception('purge_firm_tenant_registry')
        return {'success': False, 'error': str(exc)}
    finally:
        if own:
            conn.close()


def remove_firm_tenant_files(firm_tenant_id: str) -> dict[str, Any]:
    """Xóa file DB meta firm và thư mục sổ DN thuê."""
    from db_utils import remove_sqlite_files

    firm_tenant_id = (firm_tenant_id or '').strip()
    errors: list[str] = []
    removed_files: list[str] = []

    meta_path = _firm_meta_db_path(firm_tenant_id)
    if os.path.isfile(meta_path):
        r = remove_sqlite_files(meta_path)
        if r.get('errors'):
            errors.extend(r['errors'])
        elif r.get('removed'):
            removed_files.append(meta_path)

    clients_dir = os.path.join(BASE_DIR, 'tenants', 'firms', firm_tenant_id)
    if os.path.isdir(clients_dir):
        try:
            shutil.rmtree(clients_dir)
            removed_files.append(clients_dir)
        except OSError as exc:
            errors.append(f'{clients_dir}: {exc}')

    firms_parent = os.path.join(BASE_DIR, 'tenants', 'firms')
    try:
        if os.path.isdir(firms_parent) and not os.listdir(firms_parent):
            os.rmdir(firms_parent)
    except OSError:
        pass

    return {'success': not errors, 'removed': removed_files, 'errors': errors}


def cleanup_orphan_firm_registry(firm_tenant_id: str) -> bool:
    """Dọn firm_users/firm_clients còn sót khi tenants đã xóa (tránh UNIQUE khi tạo lại)."""
    firm_tenant_id = (firm_tenant_id or '').strip().lower()
    if not firm_tenant_id:
        return False
    ensure_firm_schema()
    with get_main_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM tenants WHERE tenant_id = ?",
            (firm_tenant_id,),
        ).fetchone()
        if row:
            return False
        orphan = conn.execute(
            "SELECT 1 FROM firm_users WHERE firm_tenant_id = ? LIMIT 1",
            (firm_tenant_id,),
        ).fetchone()
        if not orphan:
            orphan = conn.execute(
                "SELECT 1 FROM firm_clients WHERE firm_tenant_id = ? LIMIT 1",
                (firm_tenant_id,),
            ).fetchone()
        if not orphan:
            return False
        purge_firm_tenant_registry(firm_tenant_id, conn=conn)
        sqlite_commit(conn, label='firm_tenant')
        logger.info('cleanup_orphan_firm_registry: %s', firm_tenant_id)
        return True


def _norm_email(v: str) -> str:
    return (v or '').strip().lower()


def get_firm_user_by_login(login: str) -> dict | None:
    login = (login or '').strip()
    if not login:
        return None
    ensure_firm_schema()
    with get_main_db_connection() as conn:
        row = conn.execute(
            """
            SELECT fu.*, t.business_name AS firm_name, t.db_path AS firm_db_path,
                   t.is_active AS firm_active, t.expiry_date, t.settings
            FROM firm_users fu
            JOIN tenants t ON t.tenant_id = fu.firm_tenant_id
            WHERE (LOWER(fu.login_email) = LOWER(?) OR fu.login_email = ?)
              AND fu.is_active = 1
              AND COALESCE(t.tenant_type, 'standalone') = 'firm'
            LIMIT 1
            """,
            (login, login),
        ).fetchone()
    return dict(row) if row else None


def verify_firm_password(firm_user: dict, password: str) -> bool:
    h = firm_user.get('password_hash') or ''
    if isinstance(h, bytes):
        h = h.decode('utf-8')
    try:
        return check_password_hash(h, password or '')
    except Exception:
        return False


def _firm_meta_db_path(firm_tenant_id: str) -> str:
    return os.path.join(BASE_DIR, 'tenants', f'{firm_tenant_id}.db')


def _client_db_rel(firm_tenant_id: str, client_id: str) -> str:
    safe = re.sub(r'[^\w\-]', '_', client_id.strip())
    return os.path.join('tenants', 'firms', firm_tenant_id, 'clients', f'{safe}.db')


def _map_sme_role(accounting_regime: str, access_role: str, firm_role: str) -> str:
    from Services.tenant_profile import is_sme_regime
    sme = is_sme_regime(accounting_regime)
    if not sme:
        return 'accountant'
    reg = (accounting_regime or '').upper()
    suffix = '58' if 'TT58' in reg or 'MICRO' in reg else '99'
    if firm_role == 'owner' or access_role == 'full':
        return f'managerSME{suffix}'
    if access_role == 'view':
        return f'accountantSME{suffix}'
    return f'accountantSME{suffix}'


def init_firm_meta_database(firm_tenant_id: str, business_name: str, phone: str, **kwargs) -> str:
    """DB meta firm (billing/settings) — không tạo user đăng nhập DN."""
    from tenant_middleware import ensure_tenants_dir
    ensure_tenants_dir()
    db_path = _firm_meta_db_path(firm_tenant_id)
    template = os.path.join(BASE_DIR, 'database.db')
    if not os.path.exists(template):
        raise FileNotFoundError('Không tìm thấy database.db mẫu')
    shutil.copy2(template, db_path)

    contact_email = (kwargs.get('contact_email') or kwargs.get('email') or '').strip()
    with open_sqlite(db_path) as conn:
        cur = conn.cursor()
        for table in ('user_tenant_mapping', 'user_trusted_devices', 'tenants'):
            cur.execute(f'DROP TABLE IF EXISTS {table}')
        cur.execute('DELETE FROM users')
        try:
            cur.execute("DELETE FROM sqlite_sequence WHERE name='users'")
        except Exception:
            pass
        cur.execute('DELETE FROM business_info')
        from Services.tenant_profile import ensure_business_info_profile_columns, sync_business_info_profile, build_profile_from_registry
        ensure_business_info_profile_columns(cur)
        settings_json = kwargs.get('settings_json') or {}
        cur.execute("""
            INSERT INTO business_info (
                business_name, representative_name, address, phone, email, tax_code,
                accounting_regime, filing_period
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            business_name,
            kwargs.get('representative_name') or business_name,
            kwargs.get('address') or '',
            phone,
            contact_email,
            kwargs.get('tax_code') or '',
            settings_json.get('accounting_regime', 'SME_TT99'),
            settings_json.get('filing_period', 'monthly'),
        ))
        profile = build_profile_from_registry({
            'tenant_id': firm_tenant_id,
            'settings': settings_json,
            'business_type': kwargs.get('business_line') or 'accounting_firm',
        })
        sync_business_info_profile(cur, profile)
        from db.init import ensure_tenant_db_schema
        ensure_tenant_db_schema(conn)
        from Services.tenant_db_bootstrap import clear_trial_business_data
        from Services.audit_log import ensure_audit_table
        from db.schema_helpers import set_foreign_keys
        set_foreign_keys(conn, False)
        clear_trial_business_data(conn)
        ensure_audit_table(conn)
        set_foreign_keys(conn, True)
        sqlite_commit(conn, label='firm_tenant')
    return db_path


def _mark_firm_own_books_ready(firm_tenant_id: str) -> None:
    from Services.subscription_service import parse_tenant_settings

    def _write():
        with get_main_db_connection() as conn:
            row = conn.execute(
                "SELECT settings FROM tenants WHERE tenant_id = ?",
                (firm_tenant_id.strip(),),
            ).fetchone()
            if not row:
                return
            settings = parse_tenant_settings(row['settings'])
            if settings.get('firm_own_books_ready'):
                return
            settings['firm_own_books_ready'] = True
            conn.execute(
                "UPDATE tenants SET settings = ? WHERE tenant_id = ?",
                (json.dumps(settings, ensure_ascii=False), firm_tenant_id.strip()),
            )
            sqlite_commit(conn, label='firm_tenant')

    sqlite_write_retry(_write, label='mark_firm_own_books_ready')


def ensure_firm_own_books_ready(firm_tenant_id: str) -> str:
    """Chuẩn bị sổ Kế toán SME nội bộ của DVKT (DB meta firm). Trả đường dẫn tuyệt đối."""
    from Services.subscription_service import get_tenant_record, parse_tenant_settings
    from Services.tenant_db_bootstrap import clear_trial_business_data
    from db.init import ensure_tenant_db_schema
    from Services.audit_log import ensure_audit_table

    firm_tenant_id = firm_tenant_id.strip()
    rec = get_tenant_record(firm_tenant_id, include_inactive=True)
    if not rec:
        raise FileNotFoundError('Không tìm thấy tenant DVKT')
    settings = parse_tenant_settings(rec.get('settings'))
    abs_path = client_db_abs(rec.get('db_path') or os.path.join('tenants', f'{firm_tenant_id}.db'))
    if not os.path.isfile(abs_path):
        raise FileNotFoundError('File sổ kế toán DVKT không tồn tại')

    if not settings.get('firm_own_books_ready'):
        def _bootstrap(conn):
            from db.schema_helpers import set_foreign_keys
            set_foreign_keys(conn, False)
            clear_trial_business_data(conn)
            ensure_tenant_db_schema(conn)
            ensure_audit_table(conn)
            set_foreign_keys(conn, True)

        _exclusive_tenant_db_write(abs_path, 'ensure_firm_own_books_bootstrap', _bootstrap)
        _mark_firm_own_books_ready(firm_tenant_id)
    else:
        def _migrate(conn):
            ensure_tenant_db_schema(conn)

        _exclusive_tenant_db_write(abs_path, 'ensure_firm_own_books_migrate', _migrate)
    return abs_path


def _firm_own_books_access_role(firm_role: str) -> str:
    role = (firm_role or '').strip().lower()
    if role in ('owner', 'chief_accountant'):
        return 'full'
    if role == 'viewer':
        return 'view'
    return 'accounting'

def init_client_book_database(
    firm_tenant_id: str,
    client_id: str,
    *,
    client_name: str,
    tax_code: str = '',
    address: str = '',
    phone: str = '',
    email: str = '',
    representative_name: str = '',
    accounting_regime: str = 'SME_TT99',
    settings_json: dict | None = None,
) -> str:
    """Sổ DN thuê — không users, không user_tenant_mapping."""
    rel = _client_db_rel(firm_tenant_id, client_id)
    abs_path = os.path.join(BASE_DIR, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    template = os.path.join(BASE_DIR, 'database.db')
    if not os.path.exists(template):
        raise FileNotFoundError('Không tìm thấy database.db mẫu')
    shutil.copy2(template, abs_path)

    settings_json = dict(settings_json or {})
    settings_json.setdefault('accounting_regime', accounting_regime)
    settings_json['onboarding_completed'] = True
    settings_json['firm_client'] = True
    settings_json['firm_tenant_id'] = firm_tenant_id
    settings_json['client_id'] = client_id

    from Services.tenant_profile import ensure_business_info_profile_columns, sync_business_info_profile, build_profile_from_registry
    from Services.tenant_db_bootstrap import clear_trial_business_data
    from db.init import ensure_tenant_db_schema

    def _init(conn):
        from db.schema_helpers import set_foreign_keys
        set_foreign_keys(conn, False)
        cur = conn.cursor()
        for table in ('user_tenant_mapping', 'user_trusted_devices', 'tenants'):
            cur.execute(f'DROP TABLE IF EXISTS {table}')
        cur.execute('DELETE FROM users')
        clear_trial_business_data(conn)
        cur.execute('DELETE FROM business_info')
        ensure_business_info_profile_columns(cur)
        cur.execute("""
            INSERT INTO business_info (
                business_name, representative_name, address, phone, email, tax_code,
                accounting_regime, filing_period
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            client_name,
            representative_name or client_name,
            address,
            phone,
            email,
            tax_code,
            accounting_regime,
            settings_json.get('filing_period', 'monthly'),
        ))
        profile = build_profile_from_registry({
            'tenant_id': f'{firm_tenant_id}:{client_id}',
            'settings': settings_json,
            'business_type': 'firm_client',
        })
        sync_business_info_profile(cur, profile)
        ensure_tenant_db_schema(conn)
        from Services.audit_log import ensure_audit_table
        ensure_audit_table(conn)
        set_foreign_keys(conn, True)

    _exclusive_tenant_db_write(abs_path, 'init_client_book_database', _init)
    return rel


def send_firm_account_emails(
    *,
    firm_tenant_id: str,
    firm_name: str,
    phone: str,
    owner_email: str,
    owner_password: str,
    owner_name: str = '',
    expiry_date: str | None = None,
    package_label: str = '',
) -> dict[str, Any]:
    """Gửi email thông tin đăng nhập owner DVKT + thông báo nội bộ."""
    from Services.email_service import send_email, smtp_configured
    from Services.login_service import public_page_url
    from Services.subscription_service import SUPPORT_NOTIFY_EMAIL

    owner_email = _norm_email(owner_email)
    if not owner_email:
        return {'success': False, 'error': 'Không có email owner'}
    if not smtp_configured():
        return {'success': False, 'error': 'SMTP chưa cấu hình — kiểm tra SENDER_EMAIL / APP_PASSWORD'}

    login_url = public_page_url('/login')
    portal_url = public_page_url('/firm')
    greet = (owner_name or firm_name or '').strip() or owner_email
    pkg_line = f"Gói dịch vụ: {package_label}\n" if package_label else ''
    expiry_line = f"Hết hạn hợp đồng: {expiry_date}\n" if expiry_date else ''

    subject = '[KETO] Tài khoản Đơn Vị Dịch Vụ Kế Toán đã sẵn sàng'
    body = f"""Kính gửi {greet},

Đơn Vị Dịch Vụ Kế Toán «{firm_name}» đã được khởi tạo trên KETO ALL IN ONE.

Đường dẫn đăng nhập: {login_url}
Email đăng nhập: {owner_email}
Mật khẩu: {owner_password}

Sau đăng nhập, truy cập cổng quản lý doanh nghiệp thuê: {portal_url}
Mã tenant: {firm_tenant_id}
{pkg_line}{expiry_line}
Vui lòng đổi mật khẩu sau lần đăng nhập đầu tiên.

Trân trọng,
KETO ALL IN ONE"""

    ok, err = send_email(owner_email, subject, body)
    if not ok:
        logger.warning('send_firm_account_emails → %s: %s', owner_email, err)
        return {'success': False, 'error': err or 'Gửi email thất bại'}

    support_subject = f'[KETO DVKT] Tenant mới {firm_tenant_id} — {firm_name}'
    support_body = f"""Đơn vị DVKT mới:

Tenant ID: {firm_tenant_id}
Tên: {firm_name}
SĐT: {phone}
Owner email: {owner_email}
Mật khẩu owner: {owner_password}
{pkg_line}{expiry_line}
Đăng nhập: {login_url}
Cổng DVKT: {portal_url}
"""
    send_email(SUPPORT_NOTIFY_EMAIL, support_subject, support_body)
    return {'success': True, 'sent_to': owner_email}


def provision_firm_tenant(
    firm_tenant_id: str,
    firm_name: str,
    phone: str,
    *,
    owner_email: str,
    owner_password: str,
    owner_name: str = '',
    address: str = '',
    tax_code: str = '',
    expiry_date: str | None = None,
    max_clients: int = 50,
    accounting_regime: str = 'SME_TT99',
    extra_settings: dict | None = None,
    send_emails: bool = True,
) -> dict[str, Any]:
    from Services.subscription_service import get_tenant_record
    from Services.tenant_profile import build_tenant_settings

    firm_tenant_id = (firm_tenant_id or '').strip().lower()
    owner_email = _norm_email(owner_email)
    if not firm_tenant_id or not phone or not owner_email or not owner_password:
        return {'success': False, 'error': 'Cần mã firm, SĐT, email & mật khẩu owner'}

    if get_tenant_record(firm_tenant_id, include_inactive=True):
        return {'success': False, 'error': f"Tenant '{firm_tenant_id}' đã tồn tại"}

    cleanup_orphan_firm_registry(firm_tenant_id)

    ensure_firm_schema()
    pkg = resolve_firm_package(
        (extra_settings or {}).get('firm_package_id'),
        max_clients,
    )
    max_clients = pkg['max_clients']
    settings = build_tenant_settings(
        business_line='accounting_firm',
        accounting_regime=accounting_regime,
        subscription_plan='firm',
        onboarding_completed=True,
        extra={
            'tenant_type': TENANT_TYPE_FIRM,
            'max_clients': max_clients,
            'firm_package_id': pkg['id'],
            'firm_package_label': pkg['label'],
            'firm_own_books_ready': True,
            **(extra_settings or {}),
        },
    )
    if not expiry_date:
        from dateutil.relativedelta import relativedelta
        expiry_date = (datetime.now() + relativedelta(months=12)).strftime('%Y-%m-%d')

    rel_db = os.path.join('tenants', f'{firm_tenant_id}.db')
    try:
        init_firm_meta_database(
            firm_tenant_id,
            firm_name,
            phone,
            email=owner_email,
            address=address,
            tax_code=tax_code,
            representative_name=owner_name or firm_name,
            settings_json=settings,
            business_line='accounting_firm',
        )
    except Exception as exc:
        logger.exception('init_firm_meta_database')
        return {'success': False, 'error': str(exc)}

    pw_hash = generate_password_hash(owner_password).decode('utf-8')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _write():
        with get_main_db_connection() as conn:
            conn.execute("""
                INSERT INTO tenants
                (tenant_id, db_path, business_name, phone, address, email, expiry_date,
                 created_at, is_active, settings, business_type, tenant_type, max_clients)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'accounting_firm', 'firm', ?)
            """, (
                firm_tenant_id, rel_db, firm_name, phone, address, owner_email,
                expiry_date, now, json.dumps(settings, ensure_ascii=False), max_clients,
            ))
            conn.execute("""
                INSERT INTO firm_users
                (firm_tenant_id, login_email, password_hash, full_name, firm_role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'owner', 1, ?, ?)
            """, (firm_tenant_id, owner_email, pw_hash, owner_name or firm_name, now, now))
            sqlite_commit(conn, label='firm_tenant')

    try:
        sqlite_write_retry(_write, label='provision_firm_tenant')
    except Exception as exc:
        meta = _firm_meta_db_path(firm_tenant_id)
        if os.path.exists(meta):
            os.remove(meta)
        return {'success': False, 'error': str(exc)}

    email_result: dict[str, Any] | None = None
    if send_emails:
        email_result = send_firm_account_emails(
            firm_tenant_id=firm_tenant_id,
            firm_name=firm_name,
            phone=phone,
            owner_email=owner_email,
            owner_password=owner_password,
            owner_name=owner_name or firm_name,
            expiry_date=expiry_date,
            package_label=pkg['label'],
        )

    return {
        'success': True,
        'tenant_id': firm_tenant_id,
        'tenant_type': TENANT_TYPE_FIRM,
        'owner_email': owner_email,
        'owner_password': owner_password,
        'expiry_date': expiry_date,
        'max_clients': max_clients,
        'firm_package_id': pkg['id'],
        'firm_package_label': pkg['label'],
        'email_sent': bool(email_result and email_result.get('success')),
        'email_error': (email_result or {}).get('error'),
    }


def add_firm_client(
    firm_tenant_id: str,
    *,
    client_name: str,
    tax_code: str = '',
    address: str = '',
    phone: str = '',
    email: str = '',
    representative_name: str = '',
    accounting_regime: str = 'SME_TT99',
    client_id: str | None = None,
    grant_user_ids: list[int] | None = None,
    notes: str = '',
) -> dict[str, Any]:
    ensure_firm_schema()
    firm_tenant_id = firm_tenant_id.strip()
    client_name = (client_name or '').strip()
    if not client_name:
        return {'success': False, 'error': 'Tên doanh nghiệp thuê không được trống'}

    accounting_regime = (accounting_regime or 'SME_TT99').strip().upper()
    allowed = get_firm_allowed_client_regimes(firm_tenant_id)
    if accounting_regime not in allowed:
        labels = ', '.join(allowed)
        return {'success': False, 'error': f'Chế độ kế toán không được phép. Đơn vị chỉ hỗ trợ: {labels}'}

    with get_main_db_connection() as conn:
        firm = conn.execute(
            """
            SELECT tenant_id, COALESCE(max_clients, 50) AS max_clients
            FROM tenants
            WHERE tenant_id = ?
              AND COALESCE(tenant_type, 'standalone') = 'firm'
            """,
            (firm_tenant_id,),
        ).fetchone()
        if not firm:
            return {'success': False, 'error': 'Không tìm thấy tenant DVKT'}
        ok, cap_msg = firm_can_add_client(firm_tenant_id, firm['max_clients'], conn=conn)
        if not ok:
            return {'success': False, 'error': cap_msg}

        if not client_id:
            base = re.sub(r'\D', '', tax_code) or re.sub(r'\W+', '_', client_name.lower())[:20]
            client_id = f'C{base}' if base else f'C{datetime.now().strftime("%H%M%S")}'
        client_id = re.sub(r'[^\w\-]', '_', client_id.strip())[:40]

        exists = conn.execute(
            "SELECT 1 FROM firm_clients WHERE firm_tenant_id = ? AND client_id = ?",
            (firm_tenant_id, client_id),
        ).fetchone()
        if exists:
            return {'success': False, 'error': f'Mã client {client_id} đã tồn tại'}

    try:
        db_rel = init_client_book_database(
            firm_tenant_id,
            client_id,
            client_name=client_name,
            tax_code=tax_code,
            address=address,
            phone=phone,
            email=email,
            representative_name=representative_name,
            accounting_regime=accounting_regime,
        )
    except Exception as exc:
        logger.exception('init_client_book_database')
        rel = _client_db_rel(firm_tenant_id, client_id)
        abs_path = os.path.join(BASE_DIR, rel)
        if os.path.isfile(abs_path):
            try:
                os.remove(abs_path)
            except OSError:
                pass
        return {'success': False, 'error': str(exc)}

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _write():
        with get_main_db_connection() as conn:
            conn.execute("""
                INSERT INTO firm_clients
                (client_id, firm_tenant_id, client_name, tax_code, address, phone, email,
                 representative_name, accounting_regime, db_path, status, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """, (
                client_id, firm_tenant_id, client_name, tax_code, address, phone, email,
                representative_name, accounting_regime, db_rel, notes, now, now,
            ))
            uids = grant_user_ids
            if uids is None:
                uids = [
                    r['id'] for r in conn.execute(
                        """
                        SELECT id FROM firm_users
                        WHERE firm_tenant_id = ? AND is_active = 1
                          AND firm_role IN ('owner', 'chief_accountant')
                        """,
                        (firm_tenant_id,),
                    ).fetchall()
                ]
            for uid in uids:
                conn.execute("""
                    INSERT OR REPLACE INTO firm_user_client_access
                    (firm_user_id, firm_tenant_id, client_id, access_role, is_active, granted_at)
                    VALUES (?, ?, ?, 'accounting', 1, ?)
                """, (uid, firm_tenant_id, client_id, now))
            sqlite_commit(conn, label='firm_tenant')

    sqlite_write_retry(_write, label='add_firm_client')
    return {'success': True, 'client_id': client_id, 'db_path': db_rel}


def get_firm_representative_name(firm_tenant_id: str) -> str:
    """Người đại diện DVKT — lấy từ business_info sổ meta firm."""
    from Services.subscription_service import get_tenant_record
    tid = (firm_tenant_id or '').strip()
    rec = get_tenant_record(tid, include_inactive=True)
    if not rec:
        return ''
    biz = _read_tenant_business_row(rec.get('db_path') or os.path.join('tenants', f'{tid}.db'))
    name = (biz.get('representative_name') or '').strip()
    if name:
        return name
    return (biz.get('business_name') or rec.get('business_name') or '').strip()


def _batch_client_assigned_accountants(firm_tenant_id: str, client_ids: list[str]) -> dict[str, list[str]]:
    """Kế Toán Viên được phân quyền «Kế toán phụ trách» theo từng DN."""
    if not client_ids:
        return {}
    ensure_firm_schema()
    placeholders = ','.join('?' * len(client_ids))
    params: list[Any] = [firm_tenant_id, firm_tenant_id, *client_ids]
    with get_main_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.client_id,
                   COALESCE(NULLIF(TRIM(fu.full_name), ''), fu.login_email) AS display_name
            FROM firm_user_client_access a
            JOIN firm_users fu ON fu.id = a.firm_user_id
            WHERE a.firm_tenant_id = ? AND a.is_active = 1
              AND a.access_role = 'accounting'
              AND fu.firm_tenant_id = ? AND fu.is_active = 1
              AND fu.firm_role = 'accountant'
              AND a.client_id IN ({placeholders})
            ORDER BY a.client_id, display_name
            """,
            params,
        ).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        cid = row['client_id']
        out.setdefault(cid, []).append(row['display_name'])
    return out


def enrich_clients_charge_staff(firm_tenant_id: str, clients: list[dict]) -> list[dict]:
    """Gắn tên KTV phụ trách; mặc định là Người đại diện DVKT nếu chưa phân quyền."""
    if not clients:
        return clients
    firm_rep = get_firm_representative_name(firm_tenant_id)
    client_ids = [c['client_id'] for c in clients if c.get('client_id')]
    assigned_map = _batch_client_assigned_accountants(firm_tenant_id, client_ids)
    for client in clients:
        cid = client.get('client_id') or ''
        client['accounting_regime_label'] = firm_client_regime_label(client.get('accounting_regime'))
        names = assigned_map.get(cid) or []
        if names:
            client['charge_staff_names'] = names
            client['charge_staff_display'] = ', '.join(names)
            client['charge_staff_is_default'] = False
        else:
            client['charge_staff_names'] = []
            client['charge_staff_display'] = firm_rep or '—'
            client['charge_staff_is_default'] = True
    return clients


def list_clients_for_firm_user(firm_tenant_id: str, firm_user_id: int) -> list[dict]:
    ensure_firm_schema()
    with get_main_db_connection() as conn:
        fu = conn.execute(
            "SELECT firm_role FROM firm_users WHERE id = ? AND firm_tenant_id = ? AND is_active = 1",
            (firm_user_id, firm_tenant_id),
        ).fetchone()
        if not fu:
            return []
        if fu['firm_role'] in ('owner', 'chief_accountant'):
            rows = conn.execute("""
                SELECT c.* FROM firm_clients c
                WHERE c.firm_tenant_id = ? AND c.status = 'active'
                ORDER BY c.client_name
            """, (firm_tenant_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT c.* FROM firm_clients c
                JOIN firm_user_client_access a
                  ON a.client_id = c.client_id AND a.firm_tenant_id = c.firm_tenant_id
                WHERE c.firm_tenant_id = ? AND c.status = 'active'
                  AND a.firm_user_id = ? AND a.is_active = 1
                ORDER BY c.client_name
            """, (firm_tenant_id, firm_user_id)).fetchall()
    clients = [dict(r) for r in rows]
    return enrich_clients_charge_staff(firm_tenant_id, clients)


def get_client_record(firm_tenant_id: str, client_id: str) -> dict | None:
    ensure_firm_schema()
    with get_main_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM firm_clients WHERE firm_tenant_id = ? AND client_id = ? AND status = 'active'",
            (firm_tenant_id, client_id),
        ).fetchone()
    return dict(row) if row else None


def client_has_active_einvoice(db_rel: str) -> bool:
    """Kiểm tra sổ DN thuê đã có cấu hình HĐĐT active chưa."""
    abs_db = client_db_abs(db_rel or '')
    if not abs_db or not os.path.exists(abs_db):
        return False
    try:
        from db_utils import open_sqlite

        with open_sqlite(abs_db) as conn:
            from db.init import ensure_invoice_settings_schema
            ensure_invoice_settings_schema(conn)
            row = conn.execute("""
                SELECT 1 FROM invoice_settings
                WHERE is_active = 1
                  AND provider_name IS NOT NULL
                  AND TRIM(provider_name) != ''
                LIMIT 1
            """).fetchone()
            return bool(row)
    except Exception:
        return False


def list_clients_for_master(firm_tenant_id: str) -> list[dict]:
    """Danh sách doanh nghiệp thuê DVKT — dùng cho Master cấu hình HĐĐT."""
    ensure_firm_schema()
    firm_tenant_id = firm_tenant_id.strip()
    with get_main_db_connection() as conn:
        rows = conn.execute("""
            SELECT client_id, client_name, tax_code, accounting_regime, db_path, created_at
            FROM firm_clients
            WHERE firm_tenant_id = ? AND status = 'active'
            ORDER BY client_name COLLATE NOCASE
        """, (firm_tenant_id,)).fetchall()
    clients = [dict(r) for r in rows]
    for c in clients:
        c['einvoice_configured'] = client_has_active_einvoice(c.get('db_path'))
    return clients


def user_can_manage_firm_clients(firm_tenant_id: str, firm_user_id: int) -> bool:
    ensure_firm_schema()
    with get_main_db_connection() as conn:
        row = conn.execute(
            "SELECT firm_role FROM firm_users WHERE id = ? AND firm_tenant_id = ? AND is_active = 1",
            (firm_user_id, firm_tenant_id),
        ).fetchone()
    return bool(row and row['firm_role'] in ('owner', 'chief_accountant'))


def list_firm_users(firm_tenant_id: str, *, active_only: bool = True) -> list[dict]:
    ensure_firm_schema()
    q = """
        SELECT id, firm_tenant_id, login_email, full_name, firm_role, is_active, created_at
        FROM firm_users
        WHERE firm_tenant_id = ?
    """
    params: list[Any] = [firm_tenant_id.strip()]
    if active_only:
        q += ' AND is_active = 1'
    q += ' ORDER BY firm_role, full_name, login_email'
    with get_main_db_connection() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def list_firm_users_portal(firm_tenant_id: str) -> list[dict]:
    """Danh sách nhân viên DVKT cho cổng quản trị — kèm vai trò và số DN phụ trách."""
    ensure_firm_schema()
    firm_tenant_id = firm_tenant_id.strip()
    with get_main_db_connection() as conn:
        users = [
            dict(r) for r in conn.execute(
                """
                SELECT id, firm_tenant_id, login_email, full_name, firm_role, is_active, created_at
                FROM firm_users
                WHERE firm_tenant_id = ? AND is_active = 1
                ORDER BY firm_role, full_name, login_email
                """,
                (firm_tenant_id,),
            ).fetchall()
        ]
        if not users:
            return []
        count_rows = conn.execute(
            """
            SELECT firm_user_id, COUNT(*) AS cnt
            FROM firm_user_client_access
            WHERE firm_tenant_id = ? AND is_active = 1
            GROUP BY firm_user_id
            """,
            (firm_tenant_id,),
        ).fetchall()
        total_clients = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM firm_clients
            WHERE firm_tenant_id = ? AND status = 'active'
            """,
            (firm_tenant_id,),
        ).fetchone()['cnt']
    count_map = {int(r['firm_user_id']): int(r['cnt']) for r in count_rows}
    role_order = {'owner': 0, 'chief_accountant': 1, 'accountant': 2, 'viewer': 3}
    out: list[dict] = []
    for user in users:
        role = user.get('firm_role') or ''
        uid = int(user['id'])
        if role in ('owner', 'chief_accountant'):
            assigned_count = int(total_clients or 0)
            assigned_label = f'Toàn bộ ({assigned_count} DN)' if assigned_count else 'Toàn bộ'
        else:
            assigned_count = count_map.get(uid, 0)
            assigned_label = f'{assigned_count} DN thuê' if assigned_count else 'Chưa phân công'
        out.append({
            **user,
            'firm_role_label': FIRM_ROLE_LABELS.get(role, role),
            'assigned_client_count': assigned_count,
            'assigned_clients_label': assigned_label,
        })
    out.sort(key=lambda u: (role_order.get(u.get('firm_role') or '', 9), (u.get('full_name') or u.get('login_email') or '').lower()))
    return out


def list_firm_users_for_settings(firm_tenant_id: str) -> list[dict]:
    """Danh sách tài khoản DVKT cho tab Nhân viên trên /settings (Master cấu hình)."""
    out: list[dict] = []
    for u in list_firm_users_portal(firm_tenant_id):
        role = u.get('firm_role') or ''
        out.append({
            'id': int(u['id']),
            'username': u.get('login_email') or '',
            'full_name': u.get('full_name') or '',
            'email': u.get('login_email') or '',
            'phone': '',
            'role': role,
            'role_label': u.get('firm_role_label') or FIRM_ROLE_LABELS.get(role, role),
            'branch_names': u.get('assigned_clients_label') or '',
            'is_firm_user': True,
            'is_active': int(u.get('is_active') or 1),
        })
    return out


def get_client_staff_access(firm_tenant_id: str, client_id: str) -> list[dict]:
    """Danh sách nhân viên + trạng thái phân quyền theo DN thuê."""
    ensure_firm_schema()
    with get_main_db_connection() as conn:
        staff = [
            dict(r) for r in conn.execute(
                """
                SELECT id, firm_tenant_id, login_email, full_name, firm_role, is_active, created_at
                FROM firm_users
                WHERE firm_tenant_id = ? AND is_active = 1
                ORDER BY firm_role, full_name, login_email
                """,
                (firm_tenant_id.strip(),),
            ).fetchall()
        ]
        access_rows = conn.execute(
            """
            SELECT firm_user_id, access_role, is_active
            FROM firm_user_client_access
            WHERE firm_tenant_id = ? AND client_id = ? AND is_active = 1
            """,
            (firm_tenant_id, client_id),
        ).fetchall()
    access_map = {int(r['firm_user_id']): r['access_role'] for r in access_rows}
    result = []
    for u in staff:
        role = u.get('firm_role') or ''
        implicit_full = role in ('owner', 'chief_accountant')
        uid = int(u['id'])
        assigned = implicit_full or uid in access_map
        result.append({
            **u,
            'firm_role_label': FIRM_ROLE_LABELS.get(role, role),
            'assigned': assigned,
            'implicit_access': implicit_full,
            'access_role': 'full' if implicit_full else access_map.get(uid, 'accounting'),
            'access_role_label': CLIENT_ACCESS_ROLE_LABELS.get(
                'full' if implicit_full else access_map.get(uid, 'accounting'),
                '',
            ),
        })
    return result


def get_client_manage_detail(firm_tenant_id: str, client_id: str) -> dict | None:
    client = get_client_record(firm_tenant_id, client_id)
    if not client:
        return None
    return {
        'client': client,
        'staff_access': get_client_staff_access(firm_tenant_id, client_id),
    }


def _sync_client_business_info(client: dict, **fields) -> None:
    abs_path = client_db_abs(client.get('db_path') or '')
    if not abs_path or not os.path.isfile(abs_path):
        return
    vals = {
        'client_name': fields.get('client_name', client.get('client_name')),
        'representative_name': fields.get('representative_name', client.get('representative_name')),
        'address': fields.get('address', client.get('address')),
        'phone': fields.get('phone', client.get('phone')),
        'email': fields.get('email', client.get('email')),
        'tax_code': fields.get('tax_code', client.get('tax_code')),
        'accounting_regime': fields.get('accounting_regime', client.get('accounting_regime')),
    }

    def _write(conn):
        row = conn.execute('SELECT id FROM business_info LIMIT 1').fetchone()
        if row:
            conn.execute(
                """
                UPDATE business_info SET
                    business_name = ?, representative_name = ?, address = ?,
                    phone = ?, email = ?, tax_code = ?, accounting_regime = ?
                WHERE id = ?
                """,
                (
                    vals['client_name'], vals['representative_name'], vals['address'],
                    vals['phone'], vals['email'], vals['tax_code'], vals['accounting_regime'],
                    row[0],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO business_info (
                    business_name, representative_name, address, phone, email, tax_code,
                    accounting_regime, filing_period
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'monthly')
                """,
                (
                    vals['client_name'], vals['representative_name'], vals['address'],
                    vals['phone'], vals['email'], vals['tax_code'], vals['accounting_regime'],
                ),
            )

    if has_request_context():
        from flask import g
        cached_path = getattr(g, '_sme_db_path', None)
        if cached_path and paths_same_db(cached_path, abs_path):
            try:
                conn = get_db_connection()
                _write(conn)
                from db_utils import sqlite_commit
                sqlite_commit(conn, label='sync_client_business_info')
                return
            except Exception as exc:
                if not _is_locked_error(exc):
                    raise
    _exclusive_tenant_db_write(abs_path, 'sync_client_business_info', _write)


def update_firm_client(
    firm_tenant_id: str,
    client_id: str,
    caller_user_id: int,
    **fields,
) -> dict[str, Any]:
    if not user_can_manage_firm_clients(firm_tenant_id, caller_user_id):
        return {'success': False, 'error': 'Chỉ Chủ đơn vị / Kế Toán Trưởng được sửa DN thuê'}
    client = get_client_record(firm_tenant_id, client_id)
    if not client:
        return {'success': False, 'error': 'Không tìm thấy doanh nghiệp thuê'}

    client_name = (fields.get('client_name') or client.get('client_name') or '').strip()
    if not client_name:
        return {'success': False, 'error': 'Tên doanh nghiệp không được trống'}

    accounting_regime = (fields.get('accounting_regime') or client.get('accounting_regime') or 'SME_TT99').strip().upper()
    allowed = get_firm_allowed_client_regimes(firm_tenant_id)
    if accounting_regime not in allowed:
        return {'success': False, 'error': f'Chế độ kế toán không được phép: {", ".join(allowed)}'}

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    tax_code = (fields.get('tax_code') if 'tax_code' in fields else client.get('tax_code') or '').strip()
    address = (fields.get('address') if 'address' in fields else client.get('address') or '').strip()
    phone = (fields.get('phone') if 'phone' in fields else client.get('phone') or '').strip()
    email = (fields.get('email') if 'email' in fields else client.get('email') or '').strip()
    representative_name = (
        fields.get('representative_name') if 'representative_name' in fields
        else client.get('representative_name') or ''
    ).strip()
    notes = (fields.get('notes') if 'notes' in fields else client.get('notes') or '').strip()

    def _write():
        with get_main_db_connection() as conn:
            conn.execute(
                """
                UPDATE firm_clients SET
                    client_name = ?, tax_code = ?, address = ?, phone = ?, email = ?,
                    representative_name = ?, accounting_regime = ?, notes = ?, updated_at = ?
                WHERE firm_tenant_id = ? AND client_id = ? AND status = 'active'
                """,
                (
                    client_name, tax_code, address, phone, email,
                    representative_name, accounting_regime, notes, now,
                    firm_tenant_id, client_id,
                ),
            )
            sqlite_commit(conn, label='firm_tenant')

    sqlite_write_retry(_write, label='update_firm_client')
    updated = {**client, 'client_name': client_name, 'tax_code': tax_code, 'address': address,
               'phone': phone, 'email': email, 'representative_name': representative_name,
               'accounting_regime': accounting_regime, 'notes': notes}
    try:
        _sync_client_business_info(updated)
    except Exception as exc:
        logger.warning('sync client business_info: %s', exc)
    return {'success': True, 'client_id': client_id}


def delete_firm_client(firm_tenant_id: str, client_id: str, caller_user_id: int) -> dict[str, Any]:
    if not user_can_manage_firm_clients(firm_tenant_id, caller_user_id):
        return {'success': False, 'error': 'Chỉ Chủ đơn vị / Kế Toán Trưởng được xóa DN thuê'}
    client = get_client_record(firm_tenant_id, client_id)
    if not client:
        return {'success': False, 'error': 'Không tìm thấy doanh nghiệp thuê'}

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _write():
        with get_main_db_connection() as conn:
            conn.execute(
                """
                UPDATE firm_clients SET status = 'deleted', updated_at = ?
                WHERE firm_tenant_id = ? AND client_id = ?
                """,
                (now, firm_tenant_id, client_id),
            )
            conn.execute(
                """
                DELETE FROM firm_user_client_access
                WHERE firm_tenant_id = ? AND client_id = ?
                """,
                (firm_tenant_id, client_id),
            )
            sqlite_commit(conn, label='firm_tenant')

    sqlite_write_retry(_write, label='delete_firm_client')
    return {'success': True, 'client_id': client_id}


def set_client_staff_access(
    firm_tenant_id: str,
    client_id: str,
    caller_user_id: int,
    assignments: list[dict],
) -> dict[str, Any]:
    if not user_can_manage_firm_clients(firm_tenant_id, caller_user_id):
        return {'success': False, 'error': 'Chỉ Chủ đơn vị / Kế Toán Trưởng được phân quyền'}
    if not get_client_record(firm_tenant_id, client_id):
        return {'success': False, 'error': 'Không tìm thấy doanh nghiệp thuê'}

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _write():
        with get_main_db_connection() as conn:
            for item in assignments or []:
                try:
                    uid = int(item.get('firm_user_id'))
                except (TypeError, ValueError):
                    continue
                fu = conn.execute(
                    "SELECT id, firm_role FROM firm_users WHERE id = ? AND firm_tenant_id = ? AND is_active = 1",
                    (uid, firm_tenant_id),
                ).fetchone()
                if not fu:
                    continue
                if fu['firm_role'] in ('owner', 'chief_accountant'):
                    continue
                assigned = bool(item.get('assigned'))
                if assigned:
                    role = (item.get('access_role') or 'accounting').strip().lower()
                    if role not in ('accounting', 'view'):
                        role = 'accounting'
                    conn.execute(
                        """
                        INSERT INTO firm_user_client_access
                        (firm_user_id, firm_tenant_id, client_id, access_role, is_active, granted_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                        ON CONFLICT(firm_user_id, client_id) DO UPDATE SET
                            access_role = excluded.access_role,
                            is_active = 1,
                            granted_at = excluded.granted_at
                        """,
                        (uid, firm_tenant_id, client_id, role, now),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM firm_user_client_access
                        WHERE firm_user_id = ? AND firm_tenant_id = ? AND client_id = ?
                        """,
                        (uid, firm_tenant_id, client_id),
                    )
            sqlite_commit(conn, label='firm_tenant')

    sqlite_write_retry(_write, label='set_client_staff_access')
    return {'success': True, 'staff_access': get_client_staff_access(firm_tenant_id, client_id)}


def user_can_access_client(firm_tenant_id: str, firm_user_id: int, client_id: str) -> dict | None:
    ensure_firm_schema()
    with get_main_db_connection() as conn:
        fu = conn.execute(
            "SELECT * FROM firm_users WHERE id = ? AND firm_tenant_id = ? AND is_active = 1",
            (firm_user_id, firm_tenant_id),
        ).fetchone()
        if not fu:
            return None
        fu = dict(fu)
        if fu['firm_role'] in ('owner', 'chief_accountant'):
            access_role = 'full'
        else:
            acc = conn.execute("""
                SELECT access_role FROM firm_user_client_access
                WHERE firm_user_id = ? AND firm_tenant_id = ? AND client_id = ? AND is_active = 1
            """, (firm_user_id, firm_tenant_id, client_id)).fetchone()
            if not acc:
                return None
            access_role = acc['access_role']
    client = get_client_record(firm_tenant_id, client_id)
    if not client:
        return None
    sme_role = _map_sme_role(client.get('accounting_regime') or 'SME_TT99', access_role, fu['firm_role'])
    return {'client': client, 'firm_user': fu, 'access_role': access_role, 'sme_role': sme_role}


def client_db_abs(db_rel: str) -> str:
    if not db_rel:
        return ''
    return db_rel if os.path.isabs(db_rel) else os.path.join(BASE_DIR, db_rel)


def finalize_firm_login(firm_user: dict) -> str:
    """Thiết lập session firm (chưa chọn client). Trả session_token."""
    token = str(uuid.uuid4())
    firm_tenant_id = firm_user['firm_tenant_id']
    db_path = firm_user.get('firm_db_path') or os.path.join('tenants', f'{firm_tenant_id}.db')
    if db_path and not os.path.isabs(db_path):
        abs_db = os.path.join(BASE_DIR, db_path)
    else:
        abs_db = db_path

    def _write():
        with get_main_db_connection() as conn:
            conn.execute(
                "UPDATE firm_users SET last_session_id = ?, updated_at = ? WHERE id = ?",
                (token, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), firm_user['id']),
            )
            sqlite_commit(conn, label='firm_tenant')

    sqlite_write_retry(_write, label='firm_login_session')

    sme_role = 'managerSME99'
    session.clear()
    session['user'] = {
        'id': firm_user['id'],
        'username': firm_user['login_email'],
        'role': sme_role,
        'full_name': firm_user.get('full_name') or firm_user['login_email'],
        'permissions': 'view_audit_log',
        'is_firm_user': True,
    }
    session['role'] = sme_role
    session['session_token'] = token
    session['db_path'] = abs_db
    session['last_tenant_id'] = firm_tenant_id
    session['firm_tenant_id'] = firm_tenant_id
    session['firm_user_id'] = firm_user['id']
    session['firm_role'] = firm_user.get('firm_role') or 'accountant'
    session['firm_name'] = firm_user.get('firm_name') or firm_tenant_id
    session.pop('firm_active_client_id', None)
    session.pop('firm_viewing_client', None)
    session.pop('firm_viewing_own_books', None)
    session.pop('firm_client_name', None)
    session['user_id'] = int(firm_user['id'])
    session.modified = True
    return token


def enter_client_context(firm_tenant_id: str, firm_user_id: int, client_id: str) -> dict[str, Any]:
    ctx = user_can_access_client(firm_tenant_id, firm_user_id, client_id)
    if not ctx:
        return {'success': False, 'error': 'Không có quyền truy cập doanh nghiệp này'}
    client = ctx['client']
    fu = ctx['firm_user']
    abs_db = client_db_abs(client['db_path'])
    if not os.path.exists(abs_db):
        return {'success': False, 'error': 'File sổ kế toán không tồn tại'}

    sme_role = ctx['sme_role']
    perms = 'view_audit_log' if ctx['access_role'] == 'view' else 'view_audit_log'
    session['user'] = {
        'id': fu['id'],
        'username': fu['login_email'],
        'role': sme_role,
        'full_name': fu.get('full_name') or fu['login_email'],
        'permissions': perms,
        'is_firm_user': True,
    }
    session['role'] = sme_role
    session['db_path'] = abs_db
    session['last_tenant_id'] = f'{firm_tenant_id}:{client_id}'
    session['firm_tenant_id'] = firm_tenant_id
    session['firm_user_id'] = int(fu['id'])
    session['firm_active_client_id'] = client_id
    session['firm_viewing_client'] = True
    session.pop('firm_viewing_own_books', None)
    session['firm_client_name'] = client.get('client_name') or client_id
    session['firm_client_tax_code'] = client.get('tax_code') or ''
    session['firm_access_role'] = ctx['access_role']
    session['user_id'] = int(fu['id'])
    session.modified = True

    from Services.tenant_profile import is_sme_regime
    regime = client.get('accounting_regime') or 'SME_TT99'
    redirect_to = url_for('SME_dashboard') if is_sme_regime(regime) else url_for('HKD_dashboard')
    return {
        'success': True,
        'client_id': client_id,
        'client_name': client.get('client_name'),
        'redirect': redirect_to,
    }


def enter_firm_own_books_context(firm_tenant_id: str, firm_user_id: int) -> dict[str, Any]:
    """Vào sổ Kế toán SME nội bộ của đơn vị DVKT."""
    from Services.subscription_service import get_tenant_business_info, get_tenant_record, parse_tenant_settings
    from Services.tenant_profile import is_sme_regime

    firm_tenant_id = firm_tenant_id.strip()
    with get_main_db_connection() as conn:
        fu = conn.execute(
            "SELECT * FROM firm_users WHERE id = ? AND firm_tenant_id = ? AND is_active = 1",
            (firm_user_id, firm_tenant_id),
        ).fetchone()
    if not fu:
        return {'success': False, 'error': 'User firm không hợp lệ'}
    fu = dict(fu)

    rec = get_tenant_record(firm_tenant_id, include_inactive=True)
    if not rec:
        return {'success': False, 'error': 'Không tìm thấy tenant DVKT'}
    settings = parse_tenant_settings(rec.get('settings'))
    regime = settings.get('accounting_regime') or 'SME_TT99'
    if not is_sme_regime(regime):
        return {'success': False, 'error': 'Đơn vị DVKT chưa được cấu hình chế độ Kế toán SME'}

    try:
        abs_db = ensure_firm_own_books_ready(firm_tenant_id)
    except FileNotFoundError as exc:
        return {'success': False, 'error': str(exc)}
    except Exception as exc:
        logger.exception('ensure_firm_own_books_ready')
        return {'success': False, 'error': f'Không chuẩn bị được sổ DVKT: {exc}'}

    access_role = _firm_own_books_access_role(fu.get('firm_role') or '')
    sme_role = _map_sme_role(regime, access_role, fu.get('firm_role') or '')
    perms = 'view_audit_log' if access_role == 'view' else 'view_audit_log'
    firm_label = rec.get('business_name') or session.get('firm_name') or firm_tenant_id
    biz = get_tenant_business_info(firm_tenant_id)

    session['user'] = {
        'id': fu['id'],
        'username': fu['login_email'],
        'role': sme_role,
        'full_name': fu.get('full_name') or fu['login_email'],
        'permissions': perms,
        'is_firm_user': True,
    }
    session['role'] = sme_role
    session['db_path'] = abs_db
    session['last_tenant_id'] = firm_tenant_id
    session['firm_tenant_id'] = firm_tenant_id
    session['firm_user_id'] = int(fu['id'])
    session.pop('firm_active_client_id', None)
    session.pop('firm_viewing_client', None)
    session['firm_viewing_own_books'] = True
    session['firm_client_name'] = firm_label
    session['firm_client_tax_code'] = biz.get('tax_code') or ''
    session['firm_access_role'] = access_role
    session['user_id'] = int(fu['id'])
    session.modified = True

    redirect_to = url_for('SME_dashboard') if is_sme_regime(regime) else url_for('HKD_dashboard')
    return {
        'success': True,
        'redirect': redirect_to,
        'firm_name': firm_label,
    }


def firm_user_can_tenant_settings(firm_role: str | None) -> bool:
    """DVKT không tự cấu hình tenant — chỉ Master từ /master/settings."""
    return False


def enter_firm_settings_context(firm_tenant_id: str, firm_user_id: int) -> dict[str, Any]:
    return {
        'success': False,
        'error': 'Cài đặt DVKT do Master thiết lập tại Thiết lập tổng quản trị',
    }


def leave_client_context() -> dict[str, Any]:
    firm_tenant_id = session.get('firm_tenant_id')
    if not firm_tenant_id:
        return {'success': False, 'error': 'Không phải phiên firm'}
    firm_user_id = session.get('firm_user_id')
    with get_main_db_connection() as conn:
        fu = conn.execute(
            """
            SELECT fu.*, t.db_path AS firm_db_path
            FROM firm_users fu
            JOIN tenants t ON t.tenant_id = fu.firm_tenant_id
            WHERE fu.id = ? AND fu.firm_tenant_id = ?
            """,
            (firm_user_id, firm_tenant_id),
        ).fetchone()
    if not fu:
        return {'success': False, 'error': 'User firm không hợp lệ'}
    fu = dict(fu)
    db_rel = fu.get('firm_db_path') or os.path.join('tenants', f'{firm_tenant_id}.db')
    abs_db = client_db_abs(db_rel) if not os.path.isabs(db_rel) else db_rel
    session.pop('firm_active_client_id', None)
    session.pop('firm_viewing_client', None)
    session.pop('firm_viewing_own_books', None)
    session.pop('firm_client_name', None)
    session.pop('firm_client_tax_code', None)
    session.pop('firm_access_role', None)
    session['db_path'] = abs_db
    session['last_tenant_id'] = firm_tenant_id
    session['user'] = {
        'id': fu['id'],
        'username': fu['login_email'],
        'role': 'managerSME99',
        'full_name': fu.get('full_name') or fu['login_email'],
        'permissions': 'view_audit_log',
        'is_firm_user': True,
    }
    session['role'] = 'managerSME99'
    session.modified = True
    return {'success': True, 'redirect': url_for('firm_portal')}


def is_firm_session() -> bool:
    return bool(session.get('firm_tenant_id') and session.get('firm_user_id'))


def is_firm_viewing_client() -> bool:
    return bool(is_firm_session() and session.get('firm_viewing_client') and session.get('firm_active_client_id'))


def is_firm_viewing_own_books() -> bool:
    return bool(session.get('firm_viewing_own_books') and session.get('firm_tenant_id'))


def is_firm_using_accounting() -> bool:
    if session.get('firm_viewing_own_books') and session.get('firm_tenant_id'):
        return True
    return is_firm_viewing_client()


def add_firm_staff_user(
    firm_tenant_id: str,
    caller_user_id: int,
    *,
    login_email: str,
    password: str,
    full_name: str = '',
    firm_role: str = 'accountant',
) -> dict[str, Any]:
    """Owner / KTT thêm nhân viên firm."""
    ensure_firm_schema()
    firm_tenant_id = firm_tenant_id.strip()
    login_email = _norm_email(login_email)
    firm_role = (firm_role or 'accountant').strip().lower()
    if firm_role not in FIRM_ROLES:
        return {'success': False, 'error': 'Vai trò firm không hợp lệ'}
    if not login_email or not password:
        return {'success': False, 'error': 'Cần email đăng nhập và mật khẩu'}

    with get_main_db_connection() as conn:
        caller = conn.execute(
            "SELECT firm_role FROM firm_users WHERE id = ? AND firm_tenant_id = ? AND is_active = 1",
            (caller_user_id, firm_tenant_id),
        ).fetchone()
        if not caller or caller['firm_role'] not in ('owner', 'chief_accountant'):
            return {'success': False, 'error': 'Chỉ owner / KTT được thêm user'}
        dup = conn.execute(
            "SELECT 1 FROM firm_users WHERE firm_tenant_id = ? AND LOWER(login_email) = LOWER(?)",
            (firm_tenant_id, login_email),
        ).fetchone()
        if dup:
            return {'success': False, 'error': 'Email đăng nhập đã tồn tại trong firm'}

    pw_hash = generate_password_hash(password).decode('utf-8')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _write():
        with get_main_db_connection() as conn:
            conn.execute("""
                INSERT INTO firm_users
                (firm_tenant_id, login_email, password_hash, full_name, firm_role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (firm_tenant_id, login_email, pw_hash, full_name or login_email, firm_role, now, now))
            sqlite_commit(conn, label='firm_tenant')

    sqlite_write_retry(_write, label='add_firm_staff_user')
    return {'success': True, 'login_email': login_email, 'firm_role': firm_role}
