"""Đăng ký dùng thử Google và gia hạn subscription."""
import sqlite3
from datetime import datetime, timedelta

from flask import jsonify, redirect, render_template, request, session, url_for

from auth import login_required
from db_utils import BASE_DIR, get_main_db_connection, open_sqlite
from Services.login_service import verify_google_credential
from Services.payment_bank import get_sale_payment_status
from Services.subscription_service import (
    BUSINESS_LINE_OPTIONS,
    HKD_SECTOR_CHOICES,
    create_renewal_checkout,
    find_account_by_email,
    find_inactive_tenant_by_username,
    get_subscription_plans,
    get_tenant_business_info,
    get_tenant_record,
    normalize_tenant_phone,
    provision_trial_tenant,
    tenant_is_expired,
)
from tenant_middleware import get_tenant_by_username


TRIAL_GOOGLE_TTL_MINUTES = 60


def _valid_trial_google_email(trial_sess):
    """Email Google đã xác thực qua OAuth redirect, còn hiệu lực trong phiên."""
    email = (trial_sess or {}).get('email')
    if not email:
        return ''
    verified_at = (trial_sess or {}).get('verified_at')
    if verified_at:
        try:
            age = datetime.now() - datetime.fromisoformat(verified_at)
            if age > timedelta(minutes=TRIAL_GOOGLE_TTL_MINUTES):
                return ''
        except ValueError:
            return ''
    return str(email).strip().lower()


def register_registration_routes(app):

    @app.route('/api/tenant/profile/options', methods=['GET'])
    def api_tenant_profile_options():
        from Services.tenant_profile import profile_options_payload
        from Services.subscription_service import BUSINESS_LINE_OPTIONS

        payload = profile_options_payload()
        payload['business_lines'] = [
            {'code': k, 'label': v['label']}
            for k, v in BUSINESS_LINE_OPTIONS.items()
        ]
        return jsonify({'success': True, **payload})

    @app.route('/renewal')
    def renewal_page():
        ctx = session.get('renewal_context') or {}
        tenant_id = ctx.get('tenant_id') or request.args.get('tenant_id', '').strip()
        tenant = get_tenant_record(tenant_id, include_inactive=True) if tenant_id else None
        biz = get_tenant_business_info(tenant_id) if tenant_id else {}
        plans = []
        for p in get_subscription_plans():
            plans.append({
                **p,
                'price_fmt': f"{int(p['price']):,}".replace(',', '.'),
            })
        return render_template(
            'renewal.html',
            tenant=tenant,
            tenant_id=tenant_id,
            business_info=biz,
            plans=plans,
            renewal_context=ctx,
        )

    @app.route('/onboarding')
    @login_required
    def onboarding_page():
        tenant_id = session.get('last_tenant_id')
        if not tenant_id:
            return redirect(url_for('sale'))
        rec = get_tenant_record(tenant_id, include_inactive=True)
        settings = {}
        if rec and rec.get('settings'):
            import json
            try:
                settings = json.loads(rec['settings']) if isinstance(rec['settings'], str) else rec['settings']
            except Exception:
                settings = {}
        if settings.get('onboarding_completed'):
            return redirect(url_for('sale'))
        return render_template('onboarding.html', tenant_id=tenant_id)

    @app.route('/api/subscription/plans', methods=['GET'])
    def api_subscription_plans():
        plans = []
        for p in get_subscription_plans():
            plans.append({
                'code': p['code'],
                'name': p['name'],
                'price': p['price'],
                'has_einvoice': bool(p.get('has_einvoice')),
            })
        return jsonify({'success': True, 'plans': plans})

    @app.route('/api/trial/google-check', methods=['POST'])
    def api_trial_google_check():
        payload = request.get_json(silent=True) or {}
        user_info, err = verify_google_credential(payload.get('credential'))
        if err:
            return jsonify({'success': False, 'error': err}), 400

        email = (user_info.get('email') or '').strip().lower()
        account = find_account_by_email(email, active_only=False)

        if not account:
            return jsonify({
                'success': True,
                'status': 'new',
                'email': email,
                'name': user_info.get('name') or '',
                'business_lines': list(BUSINESS_LINE_OPTIONS.values()),
                'hkd_sectors': list(HKD_SECTOR_CHOICES),
            })

        if not account.get('tenant_active') or tenant_is_expired({
            'is_active': account.get('tenant_active'),
            'expiry_date': account.get('expiry_date'),
        }):
            session['renewal_context'] = {
                'tenant_id': account['tenant_id'],
                'email': email,
                'business_name': account.get('business_name') or '',
            }
            return jsonify({
                'success': True,
                'status': 'expired',
                'redirect': url_for('renewal_page'),
                'tenant_id': account['tenant_id'],
            })

        return jsonify({
            'success': True,
            'status': 'active',
            'message': 'Email đã có tài khoản — vui lòng đăng nhập.',
        })

    @app.route('/api/trial/register', methods=['POST'])
    def api_trial_register():
        payload = request.get_json(silent=True) or {}
        google_email = ''

        credential = (payload.get('credential') or '').strip()
        trial_sess = session.get('trial_google') or {}
        session_email = _valid_trial_google_email(trial_sess)

        if credential:
            user_info, err = verify_google_credential(credential)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            google_email = (user_info.get('email') or '').strip().lower()
        elif session_email:
            google_email = session_email
        else:
            session.pop('trial_google', None)
            return jsonify({
                'success': False,
                'error': 'Phiên xác thực Google đã hết hạn. Vui lòng xác thực lại để tiếp tục đăng ký.',
                'retry_url': url_for('trial_google_start'),
            }), 400

        if not google_email:
            return jsonify({'success': False, 'error': 'Thiếu xác thực Google'}), 400

        existing = find_account_by_email(google_email, active_only=False)
        if existing:
            return jsonify({'success': False, 'error': 'Email Google đã được đăng ký'}), 400

        phone_raw = payload.get('phone', '').strip()
        tenant_id = normalize_tenant_phone(phone_raw)
        if not tenant_id:
            return jsonify({'success': False, 'error': 'Số điện thoại không hợp lệ (10 số, bắt đầu 0)'}), 400

        business_name = (payload.get('business_name') or '').strip()
        if not business_name:
            return jsonify({
                'success': False,
                'error': 'Vui lòng nhập tên doanh nghiệp hoặc hộ kinh doanh',
            }), 400

        from Services.tenant_profile import (
            default_vat_filing_period_for_regime,
            is_sme_regime,
            normalize_accounting_regime,
            normalize_vat_filing_period,
        )

        regime = normalize_accounting_regime(payload.get('accounting_regime') or 'HKD')
        sme = is_sme_regime(regime)
        extra_settings = None
        if sme:
            from Services.sme.micro_enterprise import (
                check_tt58_provision_eligibility,
                normalize_enterprise_sector,
            )
            sector = normalize_enterprise_sector(payload.get('enterprise_sector'))
            band = (payload.get('sme_revenue_band') or '').strip()
            provision = check_tt58_provision_eligibility(
                accounting_regime=regime,
                enterprise_sector=sector,
                sme_revenue_band=band,
            )
            if provision.get('warn'):
                return jsonify({'success': False, 'error': provision.get('message')}), 400
            fp = normalize_vat_filing_period(
                payload.get('vat_filing_period') or payload.get('filing_period'),
                default=default_vat_filing_period_for_regime(regime),
            )
            extra_settings = {
                'vat_filing_period': fp,
                'filing_period': fp,
                'enterprise_sector': sector,
            }
            if band:
                extra_settings['sme_revenue_band'] = band

        result = provision_trial_tenant(
            tenant_id=tenant_id,
            business_name=business_name,
            phone=tenant_id,
            email=(payload.get('email') or google_email).strip(),
            address=(payload.get('address') or '').strip(),
            tax_code=(payload.get('tax_code') or '').strip(),
            business_line=(payload.get('business_line') or 'pos').strip(),
            hkd_sector='' if sme else (
                payload.get('hkd_sector') or payload.get('primary_nn_sector') or 'NN1'
            ).strip(),
            google_email=google_email,
            representative_name=(payload.get('representative_name') or '').strip(),
            revenue_tier=None if sme else (payload.get('revenue_tier') or 'DT1').strip(),
            accounting_regime=regime,
            enabled_nn_sectors=[] if sme else payload.get('enabled_nn_sectors'),
            extra_settings=extra_settings,
        )
        if not result.get('success'):
            return jsonify(result), 400

        session['post_register_login'] = {
            'username': result['username'],
            'tenant_id': tenant_id,
            'message': 'Đăng ký thành công! Mật khẩu đã gửi qua email.',
        }
        session.pop('trial_google', None)
        return jsonify({
            'success': True,
            'tenant_id': tenant_id,
            'redirect': url_for('login'),
            'message': 'Đăng ký thành công! Kiểm tra email để nhận mật khẩu đăng nhập.',
        })

    @app.route('/api/renewal/checkout', methods=['POST'])
    def api_renewal_checkout():
        payload = request.get_json(silent=True) or {}
        ctx = session.get('renewal_context') or {}
        tenant_id = (payload.get('tenant_id') or ctx.get('tenant_id') or '').strip()
        plan_code = (payload.get('plan_code') or '').strip().upper()
        years = payload.get('years', 1)

        if not tenant_id:
            return jsonify({'success': False, 'error': 'Thiếu mã cửa hàng (tenant)'}), 400

        customer = {
            'customer_name': (payload.get('customer_name') or '').strip(),
            'company_name': (payload.get('company_name') or '').strip(),
            'tax_code': (payload.get('tax_code') or '').strip(),
            'phone': (payload.get('phone') or '').strip(),
            'address': (payload.get('address') or '').strip(),
            'email': (payload.get('email') or ctx.get('email') or '').strip(),
        }

        result = create_renewal_checkout(tenant_id, plan_code, years, customer)
        if not result.get('success'):
            return jsonify(result), 400

        session['renewal_checkout'] = {
            'sale_id': result['sale_id'],
            'tenant_id': tenant_id,
        }
        return jsonify(result)

    @app.route('/api/renewal/status/<int:sale_id>', methods=['GET'])
    def api_renewal_status(sale_id):
        checkout = session.get('renewal_checkout') or {}
        if checkout.get('sale_id') != sale_id:
            ctx = session.get('renewal_context') or {}
            if not ctx.get('tenant_id'):
                return jsonify({'success': False, 'error': 'Phiên gia hạn không hợp lệ'}), 403

        status = get_sale_payment_status(sale_id)
        if status.get('paid'):
            session.pop('renewal_checkout', None)
            session.pop('renewal_context', None)
            meta = status.get('renewal') or {}
            return jsonify({
                'success': True,
                'paid': True,
                'sale_id': sale_id,
                'tenant_id': meta.get('tenant_id') or checkout.get('tenant_id'),
                'expiry_date': meta.get('expiry_date'),
                'redirect': url_for('login'),
                'message': 'Thanh toán thành công! Tài khoản đã được gia hạn.',
            })
        return jsonify({'success': True, 'paid': False, 'status': status})

    @app.route('/api/onboarding/status', methods=['GET'])
    @login_required
    def api_onboarding_status():
        from Services.payment_bank import validate_vietqr_setup
        tenant_id = session.get('last_tenant_id')
        rec = get_tenant_record(tenant_id, include_inactive=True) if tenant_id else None
        settings = {}
        if rec and rec.get('settings'):
            import json
            try:
                settings = json.loads(rec['settings']) if isinstance(rec['settings'], str) else rec['settings']
            except Exception:
                pass
        vqr = validate_vietqr_setup()
        return jsonify({
            'success': True,
            'onboarding_completed': bool(settings.get('onboarding_completed')),
            'vietqr_ready': vqr.get('ready'),
            'vietqr_missing': vqr.get('missing', []),
        })

    def _mark_onboarding_completed(tenant_id, extra_settings=None):
        import json
        main = get_main_db_connection()
        try:
            rec = main.execute(
                "SELECT settings FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            settings = {}
            if rec and rec['settings']:
                try:
                    settings = json.loads(rec['settings'])
                except Exception:
                    settings = {}
            settings['onboarding_completed'] = True
            if extra_settings:
                settings.update(extra_settings)
            main.execute(
                "UPDATE tenants SET settings = ? WHERE tenant_id = ?",
                (json.dumps(settings, ensure_ascii=False), tenant_id),
            )
            main.commit()
        finally:
            main.close()

    @app.route('/api/onboarding/skip', methods=['POST'])
    @login_required
    def api_onboarding_skip():
        """Bỏ qua VietQR — cho phép vào hệ thống, cấu hình sau tại Cài đặt."""
        tenant_id = session.get('last_tenant_id')
        if not tenant_id:
            return jsonify({'success': False, 'error': 'Không xác định được cửa hàng'}), 400
        _mark_onboarding_completed(tenant_id, {'vietqr_skipped': True})
        return jsonify({
            'success': True,
            'redirect': url_for('sale'),
            'message': 'Bạn có thể cấu hình VietQR sau tại Hệ Thống → Thiết lập.',
        })

    @app.route('/api/onboarding/complete', methods=['POST'])
    @login_required
    def api_onboarding_complete():
        """Lưu VietQR + đánh dấu hoàn tất onboarding."""
        from Services.payment_bank import save_payment_settings, validate_vietqr_setup

        tenant_id = session.get('last_tenant_id')
        if not tenant_id:
            return jsonify({'success': False, 'error': 'Không xác định được cửa hàng'}), 400

        payload = request.get_json(silent=True) or {}
        db_path = session.get('db_path')
        if db_path and not __import__('os').path.isabs(db_path):
            db_path = __import__('os').path.join(BASE_DIR, db_path)

        conn = open_sqlite(db_path or __import__('os').path.join(BASE_DIR, 'database.db'))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            biz_fields = {
                'business_name': payload.get('business_name'),
                'tax_code': payload.get('tax_code'),
                'phone': payload.get('phone'),
                'email': payload.get('email'),
                'address': payload.get('address'),
                'representative_name': payload.get('representative_name'),
                'bank_name': payload.get('bank_name'),
                'bank_account': payload.get('bank_account'),
                'bank_code': payload.get('bank_code'),
                'account_holder': payload.get('account_holder'),
            }
            row = cur.execute("SELECT id FROM business_info LIMIT 1").fetchone()
            if row:
                sets = ', '.join(f"{k} = ?" for k in biz_fields if biz_fields.get(k) is not None)
                vals = [v for v in biz_fields.values() if v is not None]
                if sets:
                    cur.execute(f"UPDATE business_info SET {sets} WHERE id = ?", vals + [row['id']])
            conn.commit()
        finally:
            conn.close()

        save_payment_settings(payload)

        vqr = validate_vietqr_setup()
        if not vqr.get('ready'):
            return jsonify({
                'success': False,
                'error': 'Chưa đủ thông tin VietQR: ' + ', '.join(vqr.get('missing', [])),
                'can_skip': True,
            }), 400

        _mark_onboarding_completed(tenant_id, {'vietqr_skipped': False})
        return jsonify({'success': True, 'redirect': url_for('sale')})
