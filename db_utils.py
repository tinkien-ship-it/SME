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
    """Kết nối DB của tenant hiện tại (hoặc main DB nếu chưa có tenant)."""
    db_path = resolve_db_path()
    logger.debug(
        "DB: %s | Tenant: %s",
        db_path,
        getattr(g, "tenant_id", None) if has_request_context() else "NO_CTX",
    )
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def get_main_db_connection():
    """Kết nối main/registry database (tenants, mapping, login history)."""
    conn = sqlite3.connect(MAIN_DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn
