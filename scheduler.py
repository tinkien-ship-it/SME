"""Lập lịch backup DB và kiểm tra tenant hết hạn."""
import os
import shutil
import sqlite3
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from flask_apscheduler import APScheduler

from db_utils import BASE_DIR, MAIN_DB_PATH, get_db_connection


def check_tenant_expirations():
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE tenants
        SET is_active = 0
        WHERE expiry_date < ? AND is_active = 1
    """, (today,))
    conn.commit()
    conn.close()
    print(f"--- Đã kiểm tra và khóa các Tenant hết hạn ngày {today} ---")


def backup_database(backup_root):
    """Quét Registry và backup cho tất cả Tenant + Main DB."""
    try:
        if not os.path.exists(backup_root):
            os.makedirs(backup_root, exist_ok=True)

        tasks = [('main', MAIN_DB_PATH)]

        try:
            conn_main = sqlite3.connect(MAIN_DB_PATH)
            tenants = conn_main.execute(
                "SELECT tenant_id, db_path FROM tenants WHERE is_active=1"
            ).fetchall()
            conn_main.close()
        except Exception as db_err:
            print(f"Lỗi truy cập Registry: {db_err}")
            return

        for t_id, t_path in tenants:
            if not t_id or not t_path:
                continue
            t_id = str(t_id).strip()
            abs_path = t_path if os.path.isabs(t_path) else os.path.join(BASE_DIR, t_path)
            tasks.append((t_id, abs_path))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for tenant_id, db_path in tasks:
            try:
                if not os.path.exists(db_path):
                    print(f"Bỏ qua {tenant_id}: File không tồn tại tại {db_path}")
                    continue

                tenant_backup_dir = os.path.join(backup_root, tenant_id)
                os.makedirs(tenant_backup_dir, exist_ok=True)

                filename = f"{tenant_id}_auto_{timestamp}.db"
                dest = os.path.join(tenant_backup_dir, filename)
                shutil.copy2(db_path, dest)

                cutoff = (datetime.now() - timedelta(days=10)).timestamp()
                for f in os.listdir(tenant_backup_dir):
                    f_path = os.path.join(tenant_backup_dir, f)
                    if os.path.isfile(f_path) and f.endswith('.db'):
                        if os.path.getctime(f_path) < cutoff:
                            os.remove(f_path)

                print(f"[{datetime.now()}] Backup OK: {tenant_id}")

            except Exception as e:
                print(f"Lỗi khi backup tenant {tenant_id}: {e}")
                continue

    except Exception as e:
        print(f"Lỗi hệ thống Backup (Tổng quát): {e}")


def init_schedulers(app, backup_root):
    """Khởi tạo APScheduler (tenant expiry) và BackgroundScheduler (backup)."""
    expiry_scheduler = APScheduler()

    @expiry_scheduler.task('cron', id='do_check_expiry', hour=0, minute=1)
    def scheduled_task():
        with app.app_context():
            check_tenant_expirations()

    expiry_scheduler.init_app(app)
    expiry_scheduler.start()

    backup_scheduler = BackgroundScheduler()
    backup_scheduler.add_job(
        func=lambda: backup_database(backup_root),
        trigger="cron",
        hour=20,
        minute=0,
    )
    backup_scheduler.add_job(
        func=_scheduled_knowledge_rss_sync,
        trigger="cron",
        day_of_week='mon',
        hour=6,
        minute=0,
        id='knowledge_rss_sync',
    )
    backup_scheduler.start()

    return expiry_scheduler, backup_scheduler


def _scheduled_knowledge_rss_sync():
    """Đồng bộ RSS pháp luật vào hàng chờ nháp — thứ Hai hàng tuần."""
    try:
        from Services.knowledge_service import run_scheduled_rss_sync
        result = run_scheduled_rss_sync()
        print(
            f"[{datetime.now()}] Knowledge RSS sync: "
            f"+{result.get('inserted', 0)} draft, skip {result.get('skipped', 0)}"
        )
    except Exception as e:
        print(f"[{datetime.now()}] Knowledge RSS sync failed: {e}")
