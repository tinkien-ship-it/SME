"""Trang và API Cập Nhật Kiến Thức."""
from flask import g, jsonify, render_template, request, session

from auth import login_required, master_required
from db_utils import get_db_connection
from Services.knowledge_service import (
    KNOWLEDGE_AUDIENCES,
    KNOWLEDGE_CATEGORIES,
    audience_for_regime,
    count_drafts,
    create_article,
    delete_article,
    get_article,
    is_hkd_regime,
    list_articles,
    maybe_auto_sync_rss,
    publish_article,
    seed_default_articles,
    sync_rss_feeds,
    update_article,
)
from Services.tenant_profile import normalize_accounting_regime


def _can_manage_knowledge():
    return session.get('role') == 'master'


def _tenant_regime():
    profile = getattr(g, 'tenant_profile', None) or {}
    regime = profile.get('accounting_regime')
    if regime:
        return normalize_accounting_regime(regime)
    try:
        conn = get_db_connection()
        row = conn.execute(
            'SELECT accounting_regime FROM business_info LIMIT 1'
        ).fetchone()
        conn.close()
        if row and row['accounting_regime']:
            return normalize_accounting_regime(row['accounting_regime'])
    except Exception:
        pass
    return 'HKD'


def register_knowledge_routes(app):

    @app.route('/cap-nhat-kien-thuc')
    @login_required
    def cap_nhat_kien_thuc_page():
        seed_default_articles()
        regime = _tenant_regime()
        is_hkd = is_hkd_regime(regime)
        draft_count = count_drafts() if _can_manage_knowledge() else 0
        return render_template(
            'cap_nhat_kien_thuc.html',
            categories=KNOWLEDGE_CATEGORIES,
            audiences=KNOWLEDGE_AUDIENCES,
            can_manage=_can_manage_knowledge(),
            is_hkd=is_hkd,
            tenant_audience=audience_for_regime(regime),
            draft_count=draft_count,
        )

    @app.route('/api/knowledge/articles', methods=['GET'])
    @login_required
    def api_knowledge_list():
        category = (request.args.get('category') or '').strip() or None
        keyword = (request.args.get('keyword') or '').strip() or None
        for_mgmt = _can_manage_knowledge()
        status_filter = (request.args.get('status') or '').strip() or None
        if for_mgmt and request.args.get('all_status') == '1':
            status_filter = status_filter or 'all'
        data = list_articles(
            category=category,
            keyword=keyword,
            tenant_regime=None if for_mgmt else _tenant_regime(),
            for_management=for_mgmt,
            status_filter=status_filter,
        )
        return jsonify({
            'success': True,
            'data': data,
            'tenant_audience': audience_for_regime(_tenant_regime()),
        })

    @app.route('/api/knowledge/articles/<int:article_id>', methods=['GET'])
    @login_required
    def api_knowledge_get(article_id):
        article = get_article(article_id)
        if not article:
            return jsonify({'success': False, 'error': 'Không tìm thấy bản tin'}), 404
        if article.get('status') != 'published' and not _can_manage_knowledge():
            return jsonify({'success': False, 'error': 'Không có quyền xem bản tin này'}), 403
        if not _can_manage_knowledge():
            from Services.knowledge_service import article_matches_regime
            if not article_matches_regime(article.get('audience'), _tenant_regime()):
                return jsonify({'success': False, 'error': 'Bản tin không áp dụng cho loại hình kinh doanh của bạn'}), 403
        return jsonify({'success': True, 'data': article})

    @app.route('/api/knowledge/articles', methods=['POST'])
    @login_required
    @master_required
    def api_knowledge_create():
        payload = request.get_json(silent=True) or {}
        title = (payload.get('title') or '').strip()
        if not title:
            return jsonify({'success': False, 'error': 'Tiêu đề không được để trống'}), 400
        user = session.get('user') or {}
        created_by = user.get('username') or session.get('username') or 'master'
        article = create_article(payload, created_by=created_by)
        return jsonify({'success': True, 'data': article, 'message': 'Đã lưu bản tin'})

    @app.route('/api/knowledge/articles/<int:article_id>', methods=['PUT'])
    @login_required
    @master_required
    def api_knowledge_update(article_id):
        payload = request.get_json(silent=True) or {}
        title = (payload.get('title') or '').strip()
        if not title:
            return jsonify({'success': False, 'error': 'Tiêu đề không được để trống'}), 400
        article = update_article(article_id, payload)
        if not article:
            return jsonify({'success': False, 'error': 'Không tìm thấy bản tin'}), 404
        return jsonify({'success': True, 'data': article, 'message': 'Đã cập nhật bản tin'})

    @app.route('/api/knowledge/articles/<int:article_id>/publish', methods=['POST'])
    @login_required
    @master_required
    def api_knowledge_publish(article_id):
        article = publish_article(article_id)
        if not article:
            return jsonify({'success': False, 'error': 'Không tìm thấy bản tin'}), 404
        return jsonify({'success': True, 'data': article, 'message': 'Đã duyệt và đăng bản tin'})

    @app.route('/api/knowledge/articles/<int:article_id>', methods=['DELETE'])
    @login_required
    @master_required
    def api_knowledge_delete(article_id):
        if not delete_article(article_id):
            return jsonify({'success': False, 'error': 'Không tìm thấy bản tin'}), 404
        return jsonify({'success': True, 'message': 'Đã xóa bản tin'})

    @app.route('/api/knowledge/sync-rss', methods=['POST'])
    @login_required
    @master_required
    def api_knowledge_sync_rss():
        user = session.get('user') or {}
        created_by = user.get('username') or session.get('username') or 'master'
        result = sync_rss_feeds(created_by=created_by, as_draft=None)
        msg = f"Đã thêm {result['inserted']} bản tin mới"
        if result.get('published'):
            msg += f" ({result['published']} tin TCT/BTC đã tự đăng)"
        if result['errors']:
            msg += f" ({len(result['errors'])} nguồn lỗi)"
        return jsonify({'success': True, 'message': msg, **result})

    @app.route('/api/knowledge/auto-sync', methods=['POST'])
    @login_required
    def api_knowledge_auto_sync():
        """Tự đồng bộ tin TCT/BTC — tối đa mỗi 12 giờ/lần."""
        result = maybe_auto_sync_rss(min_hours=12)
        return jsonify({'success': True, **result})

    @app.route('/api/knowledge/draft-count', methods=['GET'])
    @login_required
    @master_required
    def api_knowledge_draft_count():
        return jsonify({'success': True, 'count': count_drafts()})
