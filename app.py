# app.py - POS SYSTEM - UPDATED (dotenv + stock_moves + sale complete)
import atexit
import calendar
import glob
import hashlib
import json
import logging
import os
import pyotp
import random
import re
import schedule
import secrets
import shutil
import smtplib
import sqlite3
import string
import tempfile
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from email.message import EmailMessage
from functools import wraps
from io import BytesIO
from sqlite3 import Row
from typing import Optional
from unicodedata import normalize
from xml.dom import minidom
from zoneinfo import ZoneInfo  # Python 3.9+

import pandas as pd
import pdfkit  # IN PDF
import pymysql
import requests  # GỌI API HÓA ĐƠN ĐIỆN TỬ
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_bcrypt import Bcrypt  # BẢO MẬT MẬT KHẨU
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_mail import Mail, Message
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from num2words import num2words
from requests.auth import HTTPBasicAuth  # Hóa đơn điện tử Viettel
from sqlalchemy import func
from urllib3.exceptions import InsecureRequestWarning
from wtforms import DateField, SelectField, StringField
from wtforms.validators import DataRequired, Length, Regexp
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db_utils import BASE_DIR, MAIN_DB_PATH, get_db_connection
from helpers import (
    allowed_file,
    format_date,
    format_date_for_frontend,
    format_number,
    format_price,
    get_next_voucher_no,
    get_product_stock,
    get_setting,
    parse_date,
    register_jinja_filters,
    register_sqlite_converters,
    so_thanh_chu,
    thuần_thục_tên_file,
    validate_json,
    vnd,
)
from db.init import init_db, init_db_columns, migrate_database
from scheduler import init_schedulers
from tenant_middleware import (
    add_user_to_mapping,
    get_tenant_by_username,
    init_tenant,
    init_tenant_middleware,
    update_user_email_in_mapping,
)

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

load_dotenv()
# ÉP FLASK TÌM ĐÚNG THƯ MỤC templates – FIX LỖI VĨNH VIỄN!
template_dir = os.path.abspath('templates')
app = Flask(__name__, template_folder=template_dir)

register_sqlite_converters()

# ==============================================================================
# --- KHỐI CẤU HÌNH ĐƯỜNG DẪN ĐỒNG BỘ CHUẨN TUYỆT ĐỐI (CHỈ KHAI BÁO 1 LẦN) ---
# BASE_DIR, MAIN_DB_PATH, get_db_connection → db_utils.py
# ==============================================================================
STATIC_DIR    = os.path.join(BASE_DIR, 'static')
BACKUP_DIR    = os.path.join(BASE_DIR, 'backups')
UPLOAD_FOLDER = os.path.join(STATIC_DIR, 'img')      # Ép lưu chuẩn vào POS/static/img/

# Đăng ký thư mục upload chuẩn vào cấu hình của Flask
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

from Services.login_service import (
    get_auth_settings,
    get_google_client_id,
    repair_swapped_google_credentials,
)
repair_swapped_google_credentials()
_google_cfg = get_auth_settings()

# Khởi tạo OAuth (Google) — đọc từ DB Master Settings hoặc .env
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=get_google_client_id() or os.getenv('GOOGLE_CLIENT_ID', ''),
    client_secret=_google_cfg.get('google_client_secret') or os.getenv('GOOGLE_CLIENT_SECRET', ''),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

# Khởi tạo Multi-Tenant trước, rồi middleware session — session db_path ghi đè cuối cùng
init_tenant(app)
init_tenant_middleware(app, get_db_connection)

# Tự động thêm tenant_id vào tất cả url_for
from flask import url_for as original_url_for

def url_for(endpoint, **values):
    tenant_id = getattr(g, 'tenant_id', None)
    if tenant_id and endpoint != 'static' and 'tenant_id' not in values:
        values.setdefault('tenant_id', tenant_id)
    return original_url_for(endpoint, **values)

app.add_template_global(url_for, 'url_for')
def get_business_config():
    conn = get_db_connection()
    # Lấy thông tin hộ kinh doanh đang hoạt động mới nhất
    config = conn.execute('SELECT * FROM business_info WHERE is_active = 1 ORDER BY id DESC').fetchone()
    conn.close()
    return dict(config) if config else {}

@app.context_processor
def inject_business_info():
    """
    Cung cấp thông tin doanh nghiệp cho tất cả Template Frontend.
    Ưu tiên lấy dữ liệu từ bảng 'business_info' của database hiện tại (g.db_path).
    """
    # 1. Khởi tạo giá trị mặc định từ tenant_info (nếu có)
    # Điều này giúp hiển thị tên shop cơ bản ngay cả khi bảng business_info trống
    business_data = dict(g.tenant_info) if g.get('tenant_info') else {}

    try:
        # 2. Kết nối tới DB hiện tại (Hàm này đã được fix dùng g.db_path ở các bước trước)
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
        conn.close()

        if row:
            # Chuyển row thành dict và cập nhật/ghi đè vào business_data
            # Dữ liệu trong bảng business_info của Tenant sẽ có độ ưu tiên cao nhất
            business_data.update(dict(row))

        return {'info': business_data}

    except Exception as e:
        # Trong trường hợp bảng business_info chưa tồn tại ở một số tenant cũ
        print(f"Lỗi inject_business_info ({g.get('tenant_id', 'Main')}): {e}")
        return {'info': business_data}

@app.context_processor
def inject_now():
    return {'now': datetime.now()}

@app.context_processor
def inject_support_info():
    from Services.support_config import support_context
    ctx = support_context()
    regime = 'HKD'
    try:
        profile = getattr(g, 'tenant_profile', None) or {}
        if profile.get('accounting_regime'):
            regime = str(profile['accounting_regime']).upper()
    except Exception:
        pass
    ctx['tenant_regime'] = regime
    return ctx

bcrypt = Bcrypt(app)
from auth import (
    User,
    admin_or_master_required,
    admin_required,
    init_auth,
    login_manager,
    login_required,
    master_required,
    require_permission,
)
init_auth(app)

register_jinja_filters(app)

 # Khởi tạo bcrypt
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'pos_secret_key_2026_secure')

# --- Database Setup (SQLite) ---
DATABASE = 'database.db'

# ==================== HÀM KẾT NỐI DATABASE THỐNG NHẤT (MULTI-TENANT) ====================

@app.teardown_appcontext
def close_db(error):
    """Đóng connection sau khi request kết thúc"""
    db = g.pop('db', None)
    if db is not None:
        db.close()
# Kéo vào runtime khi cần: xmlsec, lxml, win32crypt — ký số USB token là môi trường đặc thù
try:
    import xmlsec
    from lxml import etree
    import win32crypt
    HAS_XMLSEC = True
except Exception:
    HAS_XMLSEC = False

# === CẤU HÌNH BAN ĐẦU ===
database = 'database.db'


# === CẤU HÌNH TỪ THÔNG SỐ KĨ THUẬT VIETTEL ===
# Cấu hình database (thay đổi URI theo db của bạn, ví dụ PostgreSQL, MySQL, hoặc SQLite)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///pos.db')  # Mặc định SQLite cho test
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Model BusinessInfo (từ models.py, nhưng tích hợp vào đây cho full code)
class BusinessInfo(db.Model):
    __tablename__ = 'business_info'
    
    id = db.Column(db.Integer, primary_key=True)
    tax_code = db.Column(db.String(20), nullable=False, unique=True)      # MST
    business_name = db.Column(db.String(255))                             # Tên doanh nghiệp
    address = db.Column(db.String(500))                                   # Địa chỉ nếu có
    # Các cột khác nếu cần: phone, email, is_active, branch_code, v.v.
    
    def __repr__(self):
        return f"<BusinessInfo {self.tax_code} - {self.business_name}>"

# Tạo bảng nếu chưa tồn tại (chạy lần đầu)
with app.app_context():
    db.create_all()

with app.app_context():
    init_db_columns()
    migrate_database()

# Config Viettel từ .env
VIETTEL_CONFIG = {
    "api_url": "https://demo-sinvoice.viettel.vn:8443/InvoiceAPI/InvoiceWS",
    "username": os.getenv("VIETTEL_USERNAME"),  # Ví dụ: "0100109106-215"
    "password": os.getenv("VIETTEL_PASSWORD"),
}

def load_esign_config():
    global ESIGN_CONFIG
    ESIGN_CONFIG = {
        "provider": get_setting("esign_provider"),
        "api_url": get_setting("esign_api_url") or os.getenv("ESIGN_API_URL", ""),
        "client_id": get_setting("esign_client_id") or os.getenv("ESIGN_CLIENT_ID", ""),
        "client_secret": get_setting("esign_client_secret") or os.getenv("ESIGN_CLIENT_SECRET", ""),
    }

from werkzeug.middleware.proxy_fix import ProxyFix

# Thêm cấu hình này ngay sau dòng khai báo app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Cấu hình bảo mật Cookie Session
app.config.update(
    SESSION_COOKIE_SECURE=True,   # Chỉ gửi cookie qua HTTPS (Vì bạn đã bật SSL thành công)
    SESSION_COOKIE_HTTPONLY=True, # Ngăn chặn Javascript đọc trộm cookie
    SESSION_COOKIE_SAMESITE='Lax' # Chống tấn công CSRF, giúp lưu session ổn định
)
# === SALE routes (POS / Bán hàng) → routes/sale.py ===
from routes.sale import register_sale_routes
register_sale_routes(app)

# === INVOICE routes (Hóa đơn điện tử) → routes/invoice.py ===
from routes.invoice import register_invoice_routes
register_invoice_routes(app)

# === INVENTORY routes (Nhập kho / Tồn kho) → routes/inventory.py ===
from routes.inventory import register_inventory_routes
register_inventory_routes(app)

# === INWARD routes (Hóa đơn đầu vào) → routes/inward.py ===
from routes.inward import register_inward_routes
register_inward_routes(app)

# === KẾ TOÁN HKD routes → routes/ketoan_hkd.py ===
from routes.ketoan_hkd import register_ketoan_hkd_routes
register_ketoan_hkd_routes(app)

# === Tính Giá Thành (Thành Phẩm) → routes/production.py ===
from routes.production import register_production_routes
register_production_routes(app)

# === RENTAL routes → routes/rental.py ===
from routes.rental import register_rental_routes
register_rental_routes(app)

# === F&B routes → routes/fb.py ===
from routes.fb import register_fb_routes
register_fb_routes(app)

# === REGISTRATION / RENEWAL routes → routes/registration.py ===
from routes.registration import register_registration_routes
register_registration_routes(app)

# === AUDIT LOG routes → routes/audit.py ===
from routes.audit import register_audit_routes
register_audit_routes(app)

# === SETTINGS / LOGIN routes → routes/settings.py ===
from routes.settings import register_settings_routes
register_settings_routes(app)

# === KẾ TOÁN SME routes → routes/ketoan_sme.py ===
from routes.ketoan_sme import register_ketoan_sme_routes
register_ketoan_sme_routes(app)

# === PRODUCTS routes → routes/products.py ===
from routes.products import register_products_routes
register_products_routes(app)

# === SUPPLIERS & ORDERS routes → routes/suppliers_orders.py ===
from routes.suppliers_orders import register_suppliers_orders_routes
register_suppliers_orders_routes(app)

# === CUSTOMERS routes → routes/customers.py ===
from routes.customers import register_customers_routes
register_customers_routes(app)

# === EMPLOYEES routes → routes/employees.py ===
from routes.employees import register_employees_routes
register_employees_routes(app)

# === ATTENDANCE routes → routes/attendance.py ===
from routes.attendance import register_attendance_routes
register_attendance_routes(app)
# === CORE routes → routes/core.py ===
from routes.core import register_core_routes
register_core_routes(app)

from routes.knowledge import register_knowledge_routes
register_knowledge_routes(app)

from routes.assistant import register_assistant_routes
register_assistant_routes(app)

# === REPORTS routes → routes/reports.py ===
from routes.reports import register_reports_routes
register_reports_routes(app)

# === TAX routes → routes/tax.py ===
from routes.tax import register_tax_routes
register_tax_routes(app)

# === PAYMENT routes → routes/payment.py ===
from routes.payment import register_payment_routes
register_payment_routes(app)

# === SCALE routes → routes/scale.py ===
from routes.scale import register_scale_routes
register_scale_routes(app)

init_schedulers(app, BACKUP_DIR)
if __name__ == '__main__':
    print("POS System & Scheduler đã sẵn sàng.")
    print("Server running: http://127.0.0.1:5000")

    app.run(
        host='0.0.0.0',
        port=5000,
        debug=True,
        threaded=True,
    )

