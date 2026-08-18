# tenant.py - Path-based Multi-Tenant cho POS System
from flask import g, request, current_app
import os
import shutil
from datetime import datetime

from db_utils import open_sqlite

# Đường dẫn đến registry database
REGISTRY_PATH = os.path.join(os.path.dirname(__file__), 'tenants', 'registry.db')

def ensure_tenants_dir():
    """Đảm bảo thư mục tenants tồn tại"""
    os.makedirs('tenants', exist_ok=True)

def init_tenant_database(tenant_id: str, business_name: str = "Cửa Hàng Mới"):
    """Tạo database mới cho tenant bằng cách copy từ database.db"""
    ensure_tenants_dir()
    tenant_db_path = os.path.join('tenants', f"{tenant_id}.db")

    if os.path.exists(tenant_db_path):
        return tenant_db_path

    # Copy toàn bộ cấu trúc + dữ liệu từ database chính
    if os.path.exists('database.db'):
        shutil.copy2('database.db', tenant_db_path)
        print(f"✅ Đã tạo database cho tenant '{tenant_id}' từ database.db")
    else:
        # Nếu chưa có database chính, tạo file rỗng
        open(tenant_db_path, 'w').close()
        print(f"⚠️  Tạo file database rỗng cho tenant '{tenant_id}'")

    # Đăng ký vào registry
    conn = open_sqlite(REGISTRY_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO tenants 
        (tenant_id, db_path, business_name, created_at, is_active)
        VALUES (?, ?, ?, ?, 1)
    """, (tenant_id, tenant_db_path, business_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    return tenant_db_path

def get_tenant_db_path(tenant_id: str):
    """Lấy thông tin database của tenant từ registry"""
    if not tenant_id or tenant_id.lower() in ['main', '']:
        return None  # Tenant chính dùng database.db

    conn = open_sqlite(REGISTRY_PATH)
    try:
        c = conn.cursor()
        c.execute("""
            SELECT db_path, business_name, phone 
            FROM tenants 
            WHERE tenant_id = ? AND is_active = 1
        """, (tenant_id,))
        row = c.fetchone()
        if row:
            return {
                'db_path': row['db_path'],
                'business_name': row['business_name'],
                'phone': row['phone'] or ''
            }
        return None
    finally:
        conn.close()

def get_tenant_from_path():
    """Lấy tenant_id từ URL path (ví dụ: /cuahang1/sale → tenant_id = 'cuahang1')"""
    path = request.path.strip('/')
    if not path:
        return None
    
    parts = path.split('/')
    first_part = parts[0]

    # Bỏ qua các đường dẫn hệ thống
    if first_part in ['', 'static', 'api', 'login', 'logout', 'master', 'favicon.ico']:
        return None
    
    return first_part

def load_tenant():
    """Load thông tin tenant từ URL path"""
    tenant_id = get_tenant_from_path()

    # Tenant chính (không có tenant_id)
    if not tenant_id:
        g.tenant_id = None
        g.db_path = 'database.db'
        g.is_main_tenant = True
        g.tenant_info = None
        return

    # Tenant con
    tenant_data = get_tenant_db_path(tenant_id)
    
    if tenant_data:
        g.tenant_id = tenant_id
        g.db_path = tenant_data['db_path']
        g.is_main_tenant = False
        g.tenant_info = {
            'business_name': tenant_data['business_name'],
            'phone': tenant_data['phone']
        }
    else:
        # Tenant không tồn tại
        g.tenant_id = None
        g.db_path = None
        g.is_main_tenant = True
        g.tenant_info = None

def get_db_connection():
    """Re-export — dùng db_utils.get_db_connection."""
    from db_utils import get_db_connection as _connect
    return _connect()

def init_tenant(app):
    """Khởi tạo middleware cho multi-tenant"""
    
    @app.before_request
    def before_request():
        load_tenant()

    @app.teardown_appcontext
    def teardown(exception):
        db = getattr(g, 'db', None)
        if db is not None:
            db.close()

    # Truyền thông tin tenant vào tất cả template
    @app.context_processor
    def inject_tenant_info():
        return {
            'current_tenant': getattr(g, 'tenant_id', None),
            'is_main_tenant': getattr(g, 'is_main_tenant', True),
            'info': getattr(g, 'tenant_info', {})
        }

    print("✅ Tenant Middleware (Path-based) đã được khởi tạo.")