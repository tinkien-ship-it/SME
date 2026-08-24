"""Legacy helper đọc cấu hình HĐĐT đang active."""
from db_utils import get_db_connection


def get_invoice_config():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT *
            FROM invoice_settings
            WHERE is_active = 1
            LIMIT 1
            """
        )
        return c.fetchone()
    finally:
        conn.close()
