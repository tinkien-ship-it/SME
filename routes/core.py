"""Routes trang chủ & tiện ích — tách từ app.py."""
from flask import redirect, render_template, url_for

from auth import login_required


def register_core_routes(app):

    @app.route('/')
    def index():
        from flask import session
        # Khách / crawler Facebook: vào /login (1 lần redirect). User đã đăng nhập → POS.
        if session.get('session_token') and session.get('user'):
            return redirect(url_for('sale'))
        return redirect(url_for('login'))

    # ==================== HƯỚNG DẪN SỬ DỤNG ====================
    @app.route('/huong-dan-su-dung')
    @login_required  # hoặc bỏ nếu muốn ai cũng xem được
    def huong_dan_su_dung():
        return render_template('huongdansudung.html')
