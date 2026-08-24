"""Nhật ký truy vết — đăng nhập, sửa/xóa dữ liệu, cài đặt."""
import json
import logging
import sqlite3
from datetime import datetime

from flask import g, has_request_context, request, session

from db_utils import get_db_connection, get_main_db_connection, get_tenant_db_connection, sqlite_commit

logger = logging.getLogger(__name__)

AUDIT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    tenant_id TEXT,
    user_id INTEGER,
    username TEXT,
    user_role TEXT,
    action TEXT NOT NULL,
    module TEXT,
    entity_type TEXT,
    entity_id TEXT,
    entity_label TEXT,
    summary TEXT,
    old_data TEXT,
    new_data TEXT,
    ip_address TEXT,
    request_path TEXT,
    user_agent TEXT,
    status TEXT DEFAULT 'success'
)
"""

ACTION_LABELS = {
    'login': 'Đăng nhập',
    'logout': 'Đăng xuất',
    'create': 'Tạo mới',
    'update': 'Cập nhật',
    'delete': 'Xóa',
    'export': 'Xuất dữ liệu',
    'import': 'Nhập dữ liệu',
    'settings': 'Thay đổi cài đặt',
    'restore': 'Khôi phục',
    'other': 'Khác',
}

MODULE_LABELS = {
    'auth': 'Xác thực',
    'users': 'Người dùng',
    'products': 'Sản phẩm',
    'sale': 'Bán hàng',
    'inventory': 'Kho hàng',
    'import': 'Nhập kho',
    'settings': 'Cài đặt',
    'tax': 'Thuế',
    'invoice': 'Hóa đơn',
    'tenant': 'Tenant',
    'payment': 'Thanh toán',
    'fb': 'F&B',
    'rental': 'Thuê phòng',
    'system': 'Hệ thống',
}


def ensure_audit_table(conn):
    conn.execute(AUDIT_TABLE_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_module ON audit_log(module)")


def _json_safe(data, max_len=12000):
    if data is None:
        return None
    try:
        if isinstance(data, (dict, list)):
            text = json.dumps(data, ensure_ascii=False, default=str)
        else:
            text = str(data)
    except Exception:
        text = str(data)
    if len(text) > max_len:
        return text[:max_len] + '…'
    return text


def _request_meta():
    if not has_request_context():
        return '', '', ''
    ip = request.remote_addr or ''
    if request.headers.getlist('X-Forwarded-For'):
        ip = request.headers.getlist('X-Forwarded-For')[0].split(',')[0].strip()
    path = (request.path or '')[:500]
    ua = (request.headers.get('User-Agent') or '')[:500]
    return ip, path, ua


def _open_audit_connection(*, use_main=False, tenant_id=None):
    if use_main:
        return get_main_db_connection()
    if tenant_id:
        conn = get_tenant_db_connection(tenant_id)
        if conn:
            return conn
    return get_db_connection()


def get_current_actor():
    user = session.get('user') or {}
    return {
        'user_id': user.get('id') or session.get('user_id'),
        'username': user.get('username') or session.get('username') or 'system',
        'role': user.get('role') or session.get('role') or '',
        'tenant_id': getattr(g, 'tenant_id', None) or session.get('last_tenant_id'),
    }


def write_audit(
    action,
    module,
    summary,
    *,
    entity_type=None,
    entity_id=None,
    entity_label=None,
    old_data=None,
    new_data=None,
    status='success',
    tenant_id=None,
    username=None,
    user_id=None,
    user_role=None,
    use_main=False,
):
    """Ghi một dòng nhật ký. use_main=True → database.db (master)."""
    actor = get_current_actor()
    ip, path, ua = _request_meta()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    row = (
        now,
        tenant_id if tenant_id is not None else actor.get('tenant_id'),
        user_id if user_id is not None else actor.get('user_id'),
        username if username is not None else actor.get('username'),
        user_role if user_role is not None else actor.get('role'),
        (action or 'other').strip().lower(),
        (module or 'system').strip().lower(),
        entity_type,
        str(entity_id) if entity_id is not None else None,
        (entity_label or '')[:500] or None,
        (summary or '')[:1000] or None,
        _json_safe(old_data),
        _json_safe(new_data),
        ip,
        path,
        ua,
        status,
    )

    try:
        from db_utils import sqlite_write_retry

        def _write():
            conn = get_main_db_connection() if use_main else get_db_connection()
            try:
                ensure_audit_table(conn)
                conn.execute(
                    """
                    INSERT INTO audit_log (
                        created_at, tenant_id, user_id, username, user_role,
                        action, module, entity_type, entity_id, entity_label,
                        summary, old_data, new_data, ip_address, request_path,
                        user_agent, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                sqlite_commit(conn, label='audit_log')
            finally:
                conn.close()

        sqlite_write_retry(_write, label='write_audit')
        return True
    except Exception as exc:
        logger.warning('write_audit failed: %s', exc)
        return False


def query_audit_logs(
    *,
    tenant_id=None,
    action=None,
    module=None,
    username=None,
    keyword=None,
    start_date=None,
    end_date=None,
    limit=300,
    use_main=False,
    tenant_id_for_db=None,
):
    conn = _open_audit_connection(use_main=use_main, tenant_id=tenant_id_for_db)
    conn.row_factory = sqlite3.Row
    try:
        ensure_audit_table(conn)
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params = []

        if tenant_id:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if action:
            sql += " AND action = ?"
            params.append(action)
        if module:
            sql += " AND module = ?"
            params.append(module)
        if username:
            sql += " AND username LIKE ?"
            params.append(f'%{username}%')
        if keyword:
            sql += " AND (summary LIKE ? OR entity_label LIKE ? OR old_data LIKE ? OR new_data LIKE ?)"
            kw = f'%{keyword}%'
            params.extend([kw, kw, kw, kw])
        if start_date:
            sql += " AND DATE(created_at) >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND DATE(created_at) <= ?"
            params.append(end_date)

        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [_format_row(dict(r)) for r in rows]
    finally:
        conn.close()


def get_audit_log_by_id(log_id, use_main=False, tenant_id_for_db=None):
    conn = _open_audit_connection(use_main=use_main, tenant_id=tenant_id_for_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (log_id,)).fetchone()
        return _format_row(dict(row), include_raw=True) if row else None
    finally:
        conn.close()


def query_login_history(tenant_id=None, start_date=None, end_date=None, limit=300):
    """Đọc login_history trên main DB (đã có sẵn)."""
    conn = get_main_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM login_history WHERE 1=1"
        params = []
        if tenant_id:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        if start_date:
            sql += " AND DATE(login_at) >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND DATE(login_at) <= ?"
            params.append(end_date)
        sql += " ORDER BY login_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append({
                'id': f"login-{d.get('id')}",
                'created_at': d.get('login_at'),
                'tenant_id': d.get('tenant_id'),
                'user_id': d.get('user_id'),
                'username': d.get('username'),
                'user_role': '',
                'action': 'login',
                'action_label': ACTION_LABELS['login'],
                'module': 'auth',
                'module_label': MODULE_LABELS['auth'],
                'entity_type': None,
                'entity_id': None,
                'entity_label': None,
                'summary': d.get('status') or 'Đăng nhập',
                'ip_address': d.get('ip_address'),
                'location': d.get('location'),
                'device_info': d.get('device_info'),
                'status': d.get('status'),
                'source': 'login_history',
            })
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _format_row(row, include_raw=False):
    action = row.get('action') or 'other'
    module = row.get('module') or 'system'
    item = {
        'id': row.get('id'),
        'created_at': row.get('created_at'),
        'tenant_id': row.get('tenant_id'),
        'user_id': row.get('user_id'),
        'username': row.get('username'),
        'user_role': row.get('user_role'),
        'action': action,
        'action_label': ACTION_LABELS.get(action, action),
        'module': module,
        'module_label': MODULE_LABELS.get(module, module),
        'entity_type': row.get('entity_type'),
        'entity_id': row.get('entity_id'),
        'entity_label': row.get('entity_label'),
        'summary': row.get('summary'),
        'ip_address': row.get('ip_address'),
        'request_path': row.get('request_path'),
        'status': row.get('status'),
        'source': 'audit_log',
    }
    if include_raw:
        item['old_data'] = row.get('old_data')
        item['new_data'] = row.get('new_data')
        item['user_agent'] = row.get('user_agent')
    return item
