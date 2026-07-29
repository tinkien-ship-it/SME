"""API trợ lý AI, Zalo OA webhook, quản trị FAQ — 5 giai đoạn."""
from __future__ import annotations

from flask import g, jsonify, request, session, url_for

from auth import login_required, master_required
from Services.ai_assistant_service import ask_assistant, get_suggestions
from Services.assistant_faq import invalidate_dynamic_cache
from Services.assistant_store import (
    approve_faq_from_log,
    assistant_stats,
    dismiss_review,
    ensure_assistant_schema,
    get_assistant_settings,
    list_dynamic_faq,
    list_pending_reviews,
    save_assistant_settings,
)
from Services.support_config import get_assistant_runtime_config, support_context
from Services.zalo_oa_service import (
    handle_zalo_message,
    is_zalo_oa_configured,
    parse_webhook_event,
    verify_webhook_get,
)


def _tenant_regime() -> str:
    profile = getattr(g, 'tenant_profile', None) or {}
    regime = profile.get('accounting_regime')
    if regime:
        return str(regime).upper()
    return 'HKD'


def _build_context(payload: dict) -> dict:
    ctx = {
        'page': (payload.get('page') or request.args.get('page') or '').strip() or None,
        'path': (payload.get('path') or '').strip(),
        'page_title': (payload.get('page_title') or '').strip(),
        'form_id': (payload.get('form_id') or '').strip(),
        'screen_hint': (payload.get('screen_hint') or '').strip(),
        'regime': (payload.get('regime') or _tenant_regime()).strip(),
        'role': (payload.get('role') or session.get('role') or '').strip(),
        'tenant_id': getattr(g, 'tenant_id', None) or session.get('tenant_id'),
    }
    return {k: v for k, v in ctx.items() if v}


def register_assistant_routes(app):
    ensure_assistant_schema()

    @app.route('/api/assistant/chat', methods=['POST'])
    @login_required
    def api_assistant_chat():
        rt = get_assistant_runtime_config()
        if not rt.get('widget_enabled'):
            return jsonify({'success': False, 'error': 'Trợ lý AI đang bảo trì'}), 503

        payload = request.get_json(silent=True) or {}
        message = (payload.get('message') or '').strip()
        ctx = _build_context(payload)

        try:
            help_url = url_for('huong_dan_su_dung')
        except Exception:
            help_url = '/huong-dan-su-dung'

        user = session.get('user') or {}
        reply = ask_assistant(
            message,
            page=ctx.get('page'),
            help_url=help_url,
            context=ctx,
            channel='web',
            tenant_id=ctx.get('tenant_id'),
            username=user.get('username') or session.get('username'),
        )
        return jsonify({
            'success': True,
            'reply': reply,
            'support': support_context(),
            'assistant': get_assistant_runtime_config(),
        })

    @app.route('/api/assistant/suggestions', methods=['GET'])
    @login_required
    def api_assistant_suggestions():
        page = (request.args.get('page') or '').strip() or None
        ctx = _build_context({'page': page})
        suggestions = get_suggestions(ctx.get('page') or page)
        return jsonify({
            'success': True,
            'suggestions': suggestions,
            'support': support_context(),
            'assistant': get_assistant_runtime_config(),
            'openai_configured': get_assistant_runtime_config().get('openai_configured'),
            'zalo_oa_configured': is_zalo_oa_configured(),
        })

    @app.route('/api/zalo/webhook', methods=['GET', 'POST'])
    def api_zalo_webhook():
        if request.method == 'GET':
            code = request.args.get('code') or request.args.get('verify') or ''
            oa_id = request.args.get('oa_id')
            verified = verify_webhook_get(code, oa_id)
            if verified:
                return verified, 200, {'Content-Type': 'text/plain'}
            return 'Forbidden', 403

        payload = request.get_json(silent=True) or {}
        event = parse_webhook_event(payload)
        if not event:
            return jsonify({'success': True, 'ignored': True})

        def reply_fn(text):
            try:
                help_url = url_for('huong_dan_su_dung')
            except Exception:
                help_url = '/huong-dan-su-dung'
            return ask_assistant(
                text,
                help_url=help_url,
                context={'channel': 'zalo'},
                channel='zalo',
                zalo_user_id=event['user_id'],
                log_interaction=True,
            )

        try:
            handle_zalo_message(
                event['user_id'],
                event['text'],
                display_name=event.get('display_name'),
                reply_fn=reply_fn,
            )
        except Exception as exc:
            app.logger.exception('Zalo webhook error: %s', exc)
        return jsonify({'success': True})

    @app.route('/api/master/assistant/settings', methods=['GET'])
    @login_required
    @master_required
    def api_master_assistant_settings_get():
        cfg = get_assistant_settings()
        safe = dict(cfg)
        for k in ('zalo_oa_secret', 'zalo_oa_refresh_token', 'zalo_oa_access_token'):
            safe[f'has_{k}'] = bool((cfg.get(k) or '').strip())
            safe[k] = ''
        return jsonify({
            'success': True,
            'settings': safe,
            'stats': assistant_stats(),
            'assistant': get_assistant_runtime_config(),
            'openai_configured': get_assistant_runtime_config().get('openai_configured'),
            'webhook_url': request.url_root.rstrip('/') + '/api/zalo/webhook',
        })

    @app.route('/api/master/assistant/settings', methods=['POST'])
    @login_required
    @master_required
    def api_master_assistant_settings_save():
        data = request.get_json(silent=True) or {}
        saved = save_assistant_settings(data)
        return jsonify({'success': True, 'settings': saved, 'message': 'Đã lưu cấu hình trợ lý AI'})

    @app.route('/api/master/assistant/pending', methods=['GET'])
    @login_required
    @master_required
    def api_master_assistant_pending():
        return jsonify({
            'success': True,
            'data': list_pending_reviews(limit=80),
            'stats': assistant_stats(),
        })

    @app.route('/api/master/assistant/faq/approve', methods=['POST'])
    @login_required
    @master_required
    def api_master_assistant_faq_approve():
        data = request.get_json(silent=True) or {}
        log_id = int(data.get('log_id') or 0)
        if not log_id:
            return jsonify({'success': False, 'error': 'Thiếu log_id'}), 400
        question = (data.get('question') or '').strip()
        answer = (data.get('answer') or '').strip()
        if not question or not answer:
            return jsonify({'success': False, 'error': 'Cần câu hỏi và câu trả lời'}), 400
        faq = approve_faq_from_log(
            log_id,
            question=question,
            answer=answer,
            keywords=data.get('keywords') or [],
            pages=data.get('pages') or [],
            created_by=session.get('username') or 'master',
        )
        invalidate_dynamic_cache()
        return jsonify({'success': True, 'data': faq, 'message': 'Đã duyệt và thêm FAQ'})

    @app.route('/api/master/assistant/faq/dismiss', methods=['POST'])
    @login_required
    @master_required
    def api_master_assistant_faq_dismiss():
        data = request.get_json(silent=True) or {}
        log_id = int(data.get('log_id') or 0)
        if not log_id:
            return jsonify({'success': False, 'error': 'Thiếu log_id'}), 400
        dismiss_review(log_id)
        return jsonify({'success': True, 'message': 'Đã bỏ qua'})

    @app.route('/api/master/assistant/faq/list', methods=['GET'])
    @login_required
    @master_required
    def api_master_assistant_faq_list():
        return jsonify({'success': True, 'data': list_dynamic_faq()})
