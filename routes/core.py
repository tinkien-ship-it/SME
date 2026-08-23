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

    @app.route('/gioi-thieu-keto-pos')
    def keto_pos_intro():
        """Trang giới thiệu sản phẩm — public để chia sẻ; có trong menu Tiện ích / Hệ thống."""
        from flask import request, session

        back_url = url_for('login')
        if session.get('session_token') and session.get('user'):
            user = session.get('user') or {}
            role = str(user.get('role') or '')
            if role == 'master':
                back_url = url_for('master_settings')
            elif session.get('accounting_regime', '').upper().startswith('SME') or 'SME_' in str(
                session.get('permissions') or ''
            ):
                back_url = url_for('SME_dashboard')
            else:
                back_url = url_for('HKD_dashboard')

        share_url = request.url_root.rstrip('/') + url_for('keto_pos_intro')
        og_image_url = request.url_root.rstrip('/') + url_for(
            'static', filename='branding/main/logo.jpg'
        )
        return render_template(
            'keto_pos_gioi_thieu.html',
            intro_mode='page',
            share_url=share_url,
            og_image_url=og_image_url,
            back_url=back_url,
        )
