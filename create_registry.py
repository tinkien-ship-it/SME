# create_registry.py
import sqlite3
import os

# Tạo thư mục tenants nếu chưa có
os.makedirs('tenants', exist_ok=True)

conn = sqlite3.connect('tenants/registry.db')
c = conn.cursor()

c.execute('''
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT UNIQUE NOT NULL,           -- Đổi từ subdomain thành tenant_id cho phù hợp Path-based
    db_path TEXT NOT NULL,
    business_name TEXT,
    phone TEXT,
    address TEXT,
    email TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    settings JSON DEFAULT '{}',
    master_settings JSON DEFAULT '{}'
)
''')

# Tạo index để tìm nhanh
c.execute("CREATE INDEX IF NOT EXISTS idx_tenant_id ON tenants(tenant_id)")

conn.commit()
conn.close()

print("✅ Đã tạo registry.db thành công!")
print("Cột 'tenant_id' được dùng cho Path-based (ví dụ: /cuahang1/)")
print("Tenant chính vẫn là file database.db (không cần đăng ký trong registry)")