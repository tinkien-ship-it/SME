"""Routes trang chủ & tiện ích — tách từ app.py."""
from flask import redirect, render_template, url_for

from auth import login_required


def register_core_routes(app):

    @app.route('/')
    def index():
        return redirect(url_for('sale'))

    # ==================== HƯỚNG DẪN SỬ DỤNG ====================
    @app.route('/huong-dan-su-dung')
    @login_required  # hoặc bỏ nếu muốn ai cũng xem được
    def huong_dan_su_dung():
        return render_template('huongdansudung.html')

    @app.route('/cap-nhat-kien-thuc')
    @login_required
    def cap_nhat_kien_thuc_page():
        return render_template('cap_nhat_kien_thuc.html')
