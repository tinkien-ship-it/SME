from database import get_db

def get_invoice_config():

    conn = get_db()

    c = conn.cursor()

    c.execute("""
        SELECT * 
        FROM invoice_settings
        WHERE is_active = 1
        LIMIT 1
    """)

    return c.fetchone()