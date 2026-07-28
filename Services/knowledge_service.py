"""Quản lý bản tin Cập Nhật Kiến Thức — lưu trên main DB, dùng chung mọi tenant."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from typing import Any

import requests

from db_utils import get_main_db_connection
from Services.tenant_profile import normalize_accounting_regime

KNOWLEDGE_CATEGORIES = {
    'thue_hkd': 'Thuế HKD',
    'thue_dn': 'Thuế doanh nghiệp',
    'hoa_don': 'Hóa đơn',
    'luong_bhxh': 'Lương & BHXH',
    'thong_tu': 'Thông tư',
    'nghi_dinh': 'Nghị định',
    'phap_luat': 'Pháp luật',
    'huong_dan': 'Hướng dẫn nghiệp vụ',
    'tin_tuc': 'Tin tức',
}

KNOWLEDGE_AUDIENCES = {
    'all': 'Chung (HKD & DN)',
    'hkd': 'Hộ kinh doanh',
    'dn': 'Doanh nghiệp',
}

HKD_RSS_KEYWORDS = [
    'hộ kinh doanh', 'ho kinh doanh', 'hkd', 'tt 88', 'thông tư 88',
    'thuế', 'tt-btc', 'nghị định', 'hóa đơn', 'gtgt', 'tncn', 'kế toán',
    'lương', 'bhxh',
]

DN_RSS_KEYWORDS = [
    'doanh nghiệp', 'tt99', 'tt 99', 'tt58', 'tt 58', 'vas', 'báo cáo tài chính',
    'thuế', 'tt-btc', 'nghị định', 'hóa đơn', 'gtgt', 'tncn', 'kế toán',
    'lương', 'bhxh', 'niêm yết',
]

DEFAULT_RSS_FEEDS = [
    {
        'name': 'Thư viện Pháp luật',
        'url': 'https://thuvienphapluat.vn/page/rss.aspx',
        'keywords': list(dict.fromkeys(HKD_RSS_KEYWORDS + DN_RSS_KEYWORDS)),
    },
]

_TAG_RE = re.compile(r'<[^>]+>')
_HKD_HINTS = (
    'hộ kinh doanh', 'ho kinh doanh', 'hkd', 'thông tư 88', 'tt 88', 'tt88',
    'sổ kế toán hộ', 'cá nhân kinh doanh', 'hộ kinh doanh cá thể',
)
_DN_HINTS = (
    'doanh nghiệp', 'tt99', 'tt 99', 'tt58', 'tt 58', 'vas', 'báo cáo tài chính',
    'công ty', 'niêm yết', 'kiểm toán', 'tt200', 'tt 200',
)


def audience_for_regime(regime: str) -> str:
    """hkd hoặc dn — dùng lọc bản tin theo tenant."""
    return 'hkd' if normalize_accounting_regime(regime) == 'HKD' else 'dn'


def is_hkd_regime(regime: str) -> bool:
    return audience_for_regime(regime) == 'hkd'


def article_matches_regime(article_audience: str, regime: str) -> bool:
    aud = (article_audience or 'all').strip().lower()
    if aud == 'all':
        return True
    return aud == audience_for_regime(regime)


def _has_column(conn, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
        return column in [r[1] for r in rows]
    except sqlite3.Error:
        return False


def ensure_knowledge_schema(conn=None):
    own = conn is None
    if own:
        conn = get_main_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT,
            content TEXT,
            external_url TEXT,
            category TEXT NOT NULL DEFAULT 'tin_tuc',
            audience TEXT NOT NULL DEFAULT 'all',
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_name TEXT,
            source_id TEXT,
            status TEXT NOT NULL DEFAULT 'published',
            is_pinned INTEGER DEFAULT 0,
            published_at TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    if not _has_column(conn, 'knowledge_articles', 'audience'):
        conn.execute(
            "ALTER TABLE knowledge_articles ADD COLUMN audience TEXT NOT NULL DEFAULT 'all'"
        )
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_source
        ON knowledge_articles(source_type, source_id)
        WHERE source_id IS NOT NULL AND source_id != ''
    """)
    _migrate_audience_on_existing(conn)
    conn.commit()
    if own:
        conn.close()


def _migrate_audience_on_existing(conn):
    """Gán audience cho bản tin cũ nếu chưa có."""
    conn.execute("""
        UPDATE knowledge_articles SET audience = 'hkd'
        WHERE (audience IS NULL OR audience = '' OR audience = 'all')
          AND (
            lower(title) LIKE '%hkd%' OR lower(title) LIKE '%hộ kinh doanh%'
            OR lower(summary) LIKE '%hộ kinh doanh%' OR lower(title) LIKE '%tt 88%'
          )
    """)
    conn.execute("""
        UPDATE knowledge_articles SET audience = 'dn'
        WHERE (audience IS NULL OR audience = '' OR audience = 'all')
          AND (
            lower(title) LIKE '%doanh nghiệp%' OR lower(title) LIKE '%tt99%'
            OR lower(title) LIKE '%tt58%' OR lower(summary) LIKE '%doanh nghiệp%'
          )
    """)
    conn.execute("""
        UPDATE knowledge_articles SET audience = 'all'
        WHERE audience IS NULL OR audience = ''
    """)


def _now_iso():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _strip_html(text):
    if not text:
        return ''
    clean = unescape(_TAG_RE.sub(' ', str(text)))
    return re.sub(r'\s+', ' ', clean).strip()


def _row_to_dict(row) -> dict[str, Any]:
    if not row:
        return {}
    d = dict(row)
    d['category_label'] = KNOWLEDGE_CATEGORIES.get(d.get('category') or '', d.get('category') or '')
    d['audience_label'] = KNOWLEDGE_AUDIENCES.get(d.get('audience') or 'all', d.get('audience') or '')
    d['is_pinned'] = bool(d.get('is_pinned'))
    return d


def _guess_category(title: str, summary: str, audience: str = 'all') -> str:
    text = f'{title} {summary}'.lower()
    if 'nghị định' in text or 'nd-cp' in text:
        return 'nghi_dinh'
    if 'thông tư' in text or 'tt-btc' in text or 'tt-tct' in text:
        return 'thong_tu'
    if 'hóa đơn' in text or 'hddt' in text:
        return 'hoa_don'
    if 'lương' in text or 'bhxh' in text or 'bảo hiểm' in text:
        return 'luong_bhxh'
    if 'thuế' in text:
        return 'thue_dn' if audience == 'dn' else 'thue_hkd'
    if 'pháp luật' in text or 'luật' in text:
        return 'phap_luat'
    return 'phap_luat'


def _guess_audience(title: str, summary: str) -> str:
    text = f'{title} {summary}'.lower()
    hkd = any(h in text for h in _HKD_HINTS)
    dn = any(h in text for h in _DN_HINTS)
    if hkd and not dn:
        return 'hkd'
    if dn and not hkd:
        return 'dn'
    if hkd and dn:
        return 'all'
    return 'all'


def _normalize_audience(value) -> str:
    aud = (value or 'all').strip().lower()
    return aud if aud in KNOWLEDGE_AUDIENCES else 'all'


def seed_default_articles(conn=None):
    """Bản tin mặc định khi bảng còn trống."""
    own = conn is None
    if own:
        conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    count = conn.execute('SELECT COUNT(*) AS c FROM knowledge_articles').fetchone()['c']
    if count:
        if own:
            conn.close()
        return 0
    defaults = [
        {
            'title': 'Sổ sách kế toán HKD — Thông tư 88/2021/TT-BTC',
            'summary': 'Hệ thống sổ S1a, S2a–S2e, S3, S3a, S4, S5 và sổ phụ SP1–SP4 theo mẫu sổ kế toán cho hộ kinh doanh.',
            'content': 'Xem trực tiếp tại menu Sổ Sách Kế Toán trong phần mềm.',
            'external_url': '',
            'category': 'thong_tu',
            'audience': 'hkd',
            'source_type': 'system',
            'is_pinned': 1,
        },
        {
            'title': 'Kế toán doanh nghiệp — Thông tư 99/2025/TT-BTC & TT58',
            'summary': 'Hướng dẫn chế độ kế toán doanh nghiệp, báo cáo tài chính và nghĩa vụ thuế áp dụng cho DN siêu nhỏ / DN lớn.',
            'content': 'Theo dõi các mục Kế Toán Doanh Nghiệp trong phần mềm khi chế độ SME được kích hoạt.',
            'external_url': '',
            'category': 'thong_tu',
            'audience': 'dn',
            'source_type': 'system',
            'is_pinned': 1,
        },
        {
            'title': 'Chấm công & tính lương',
            'summary': 'Đồng bộ dữ liệu máy chấm công ZKTeco hoặc nhập Excel để tính ngày công thực tế khi lập bảng lương.',
            'content': '',
            'external_url': '',
            'category': 'huong_dan',
            'audience': 'all',
            'source_type': 'system',
            'is_pinned': 0,
        },
        {
            'title': 'Công nợ nhân viên',
            'summary': 'Theo dõi khoản lương đã hạch toán nhưng chưa chi trả qua sổ công nợ phải trả nhân viên.',
            'content': '',
            'external_url': '',
            'category': 'huong_dan',
            'audience': 'all',
            'source_type': 'system',
            'is_pinned': 0,
        },
    ]
    now = _now_iso()
    for item in defaults:
        conn.execute("""
            INSERT INTO knowledge_articles
            (title, summary, content, external_url, category, audience, source_type, status,
             is_pinned, published_at, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, 'system', ?, ?)
        """, (
            item['title'], item['summary'], item['content'], item['external_url'],
            item['category'], item['audience'], item['source_type'],
            item['is_pinned'], now, now, now,
        ))
    conn.commit()
    if own:
        conn.close()
    return len(defaults)


def list_articles(
    *,
    category=None,
    keyword=None,
    limit=100,
    tenant_regime=None,
    for_management=False,
    status_filter=None,
):
    conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    sql = 'SELECT * FROM knowledge_articles WHERE 1=1'
    params: list[Any] = []

    if for_management:
        if status_filter and status_filter != 'all':
            sql += ' AND status = ?'
            params.append(status_filter)
    else:
        sql += " AND status = 'published'"
        if tenant_regime:
            aud = audience_for_regime(tenant_regime)
            sql += " AND (audience = 'all' OR audience = ?)"
            params.append(aud)

    if category:
        sql += ' AND category = ?'
        params.append(category)
    if keyword:
        sql += ' AND (title LIKE ? OR summary LIKE ? OR content LIKE ?)'
        like = f'%{keyword.strip()}%'
        params.extend([like, like, like])

    sql += ' ORDER BY is_pinned DESC, published_at DESC, id DESC LIMIT ?'
    params.append(min(int(limit or 100), 500))
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def count_drafts() -> int:
    conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM knowledge_articles WHERE status = 'draft'"
    ).fetchone()
    conn.close()
    return int(row['c'] or 0)


def get_article(article_id: int):
    conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    row = conn.execute(
        'SELECT * FROM knowledge_articles WHERE id = ?', (article_id,)
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def create_article(data: dict, created_by: str = ''):
    conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    now = _now_iso()
    pub = (data.get('published_at') or '').strip() or now
    category = data.get('category') or 'tin_tuc'
    if category not in KNOWLEDGE_CATEGORIES:
        category = 'tin_tuc'
    audience = _normalize_audience(data.get('audience'))
    status = data.get('status') or 'published'
    if status == 'published' and not (data.get('published_at') or '').strip():
        pub = now
    cur = conn.execute("""
        INSERT INTO knowledge_articles
        (title, summary, content, external_url, category, audience, source_type, source_name,
         source_id, status, is_pinned, published_at, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'manual', NULL, NULL, ?, ?, ?, ?, ?, ?)
    """, (
        (data.get('title') or '').strip(),
        (data.get('summary') or '').strip(),
        (data.get('content') or '').strip(),
        (data.get('external_url') or '').strip(),
        category,
        audience,
        status,
        1 if data.get('is_pinned') else 0,
        pub if status == 'published' else None,
        created_by or '',
        now,
        now,
    ))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return get_article(new_id)


def update_article(article_id: int, data: dict):
    conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    existing = conn.execute(
        'SELECT id, status, published_at FROM knowledge_articles WHERE id = ?', (article_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return None
    category = data.get('category') or 'tin_tuc'
    if category not in KNOWLEDGE_CATEGORIES:
        category = 'tin_tuc'
    audience = _normalize_audience(data.get('audience'))
    status = data.get('status') or existing['status'] or 'published'
    now = _now_iso()
    pub = (data.get('published_at') or '').strip() or existing['published_at'] or now
    if status == 'published' and not existing['published_at']:
        pub = now
    conn.execute("""
        UPDATE knowledge_articles SET
            title = ?, summary = ?, content = ?, external_url = ?,
            category = ?, audience = ?, status = ?, is_pinned = ?,
            published_at = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        (data.get('title') or '').strip(),
        (data.get('summary') or '').strip(),
        (data.get('content') or '').strip(),
        (data.get('external_url') or '').strip(),
        category,
        audience,
        status,
        1 if data.get('is_pinned') else 0,
        pub if status == 'published' else None,
        now,
        article_id,
    ))
    conn.commit()
    conn.close()
    return get_article(article_id)


def publish_article(article_id: int):
    conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    existing = conn.execute(
        'SELECT id FROM knowledge_articles WHERE id = ?', (article_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return None
    now = _now_iso()
    conn.execute("""
        UPDATE knowledge_articles
        SET status = 'published',
            published_at = COALESCE(NULLIF(published_at, ''), ?),
            updated_at = ?
        WHERE id = ?
    """, (now, now, article_id))
    conn.commit()
    conn.close()
    return get_article(article_id)


def delete_article(article_id: int) -> bool:
    conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    cur = conn.execute('DELETE FROM knowledge_articles WHERE id = ?', (article_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def _parse_rss_items(xml_text: str) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for item in root.iter('item'):
        title = _strip_html(item.findtext('title') or '')
        link = (item.findtext('link') or '').strip()
        desc = _strip_html(item.findtext('description') or item.findtext('summary') or '')
        pub = (item.findtext('pubDate') or item.findtext('dc:date') or '').strip()
        if title and link:
            items.append({'title': title, 'link': link, 'summary': desc, 'pub_date': pub})
    return items


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def sync_rss_feeds(*, created_by: str = 'rss_sync', max_per_feed: int = 25, as_draft: bool = True) -> dict:
    """
    Đồng bộ văn bản pháp luật từ RSS.
    Mặc định tạo bản tin ở trạng thái draft để master duyệt trước khi đăng.
    """
    conn = get_main_db_connection()
    ensure_knowledge_schema(conn)
    now = _now_iso()
    inserted = 0
    skipped = 0
    errors: list[str] = []
    status = 'draft' if as_draft else 'published'

    for feed in DEFAULT_RSS_FEEDS:
        try:
            resp = requests.get(
                feed['url'],
                timeout=20,
                headers={'User-Agent': 'KETO-POS-KnowledgeSync/1.0'},
            )
            resp.raise_for_status()
            rss_items = _parse_rss_items(resp.content)
        except Exception as exc:
            errors.append(f"{feed['name']}: {exc}")
            continue

        count = 0
        for item in rss_items:
            if count >= max_per_feed:
                break
            title = item['title']
            summary = item['summary'][:500] if item['summary'] else title[:200]
            if not _matches_keywords(f'{title} {summary}', feed['keywords']):
                skipped += 1
                continue
            source_id = hashlib.sha256(item['link'].encode('utf-8')).hexdigest()[:40]
            exists = conn.execute(
                'SELECT id FROM knowledge_articles WHERE source_type = ? AND source_id = ?',
                ('rss', source_id),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            audience = _guess_audience(title, summary)
            category = _guess_category(title, summary, audience)
            conn.execute("""
                INSERT INTO knowledge_articles
                (title, summary, content, external_url, category, audience, source_type, source_name,
                 source_id, status, is_pinned, published_at, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'rss', ?, ?, ?, 0, ?, ?, ?, ?)
            """, (
                title,
                summary,
                summary,
                item['link'],
                category,
                audience,
                feed['name'],
                source_id,
                status,
                item.get('pub_date') or now if status == 'published' else None,
                created_by,
                now,
                now,
            ))
            inserted += 1
            count += 1

    conn.commit()
    conn.close()
    return {'inserted': inserted, 'skipped': skipped, 'errors': errors, 'as_draft': as_draft}


def run_scheduled_rss_sync():
    """Gọi từ APScheduler — đồng bộ RSS vào hàng chờ nháp."""
    return sync_rss_feeds(created_by='scheduler', as_draft=True)
