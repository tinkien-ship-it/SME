# models.py
from flask_login import UserMixin
from app import db, bcrypt

class User(UserMixin):
    def __init__(self, id, username, email, role, is_active=True):
        self.id = id
        self.username = username
        self.email = email
        self.role = role
        self.is_active = is_active

    @staticmethod
    def get(user_id):
        conn = db.get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        if user:
            return User(user['id'], user['username'], user['email'], user['role'], user['is_active'])
        return None

    @staticmethod
    def create(username, password, email='', role='nhanvien'):
        password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
        conn = db.get_db_connection()
        try:
            conn.execute("INSERT INTO users (username, password_hash, email, role) VALUES (?, ?, ?, ?)",
                        (username, password_hash, email, role))
            conn.commit()
            return True
        except:
            return False

    def has_permission(self, permission):
        if self.role == 'admin':
            return True
        conn = db.get_db_connection()
        row = conn.execute("SELECT allowed FROM permissions WHERE role = ? AND permission = ?",
                          (self.role, permission)).fetchone()
        conn.close()
        return row and row['allowed'] == 1