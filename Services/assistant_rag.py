"""RAG — trích xuất và tìm kiếm tài liệu hướng dẫn KETO POS."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from unicodedata import normalize

from db_utils import BASE_DIR

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')
_H5_RE = re.compile(r'<h5[^>]*>(.*?)</h5>', re.I | re.S)
_H6_RE = re.compile(r'<h6[^>]*>(.*?)</h6>', re.I | re.S)
_LI_RE = re.compile(r'<li[^>]*>(.*?)</li>', re.I | re.S)
_P_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.I | re.S)
_TAB_RE = re.compile(r'id="(banhang|ketoan|phongtro)"', re.I)

_CACHE: dict[str, Any] = {'mtime': 0.0, 'chunks': []}


@dataclass
class RagChunk:
    doc_id: str
    title: str
    section: str
    text: str
    score: float = 0.0


def _normalize(text: str) -> str:
    if not text:
        return ''
    t = normalize('NFD', text.lower())
    return ''.join(c for c in t if ord(c) < 768).strip()


def _strip_html(html: str) -> str:
    t = unescape(_TAG_RE.sub(' ', html or ''))
    return _WS_RE.sub(' ', t).strip()


def _tokenize(text: str) -> set[str]:
    norm = _normalize(text)
    return {w for w in re.split(r'[\s,./;:!?()\[\]"\']+', norm) if len(w) >= 2}


def _read_file(path: str) -> str:
    if not os.path.isfile(path):
        return ''
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _parse_huongdan_chunks(html: str) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    section_names = {'banhang': 'Phân hệ Bán hàng', 'ketoan': 'Phân hệ Kế toán', 'phongtro': 'Quản lý Phòng trọ'}

    for tab_id, section in section_names.items():
        tab_match = re.search(
            rf'<div class="tab-pane[^"]*"[^>]*id="{tab_id}"[^>]*>(.*?)</div>\s*(?:<!--|<div class="tab-pane|$)',
            html,
            re.I | re.S,
        )
        if not tab_match:
            continue
        block = tab_match.group(1)
        parts = re.split(r'(<h5[^>]*>.*?</h5>)', block, flags=re.I | re.S)
        current_title = section
        buf: list[str] = []
        for part in parts:
            h5 = _H5_RE.search(part)
            if h5:
                if buf:
                    text = _strip_html(' '.join(buf))
                    if len(text) > 40:
                        chunks.append({
                            'doc_id': f'huongdan_{tab_id}_{len(chunks)}',
                            'title': current_title,
                            'section': section,
                            'text': text[:1200],
                        })
                    buf = []
                current_title = _strip_html(h5.group(1))
            else:
                buf.append(part)
        if buf:
            text = _strip_html(' '.join(buf))
            if len(text) > 40:
                chunks.append({
                    'doc_id': f'huongdan_{tab_id}_{len(chunks)}',
                    'title': current_title,
                    'section': section,
                    'text': text[:1200],
                })

    for p in _P_RE.findall(html):
        text = _strip_html(p)
        if len(text) > 80 and 'Lưu ý quan trọng' not in text:
            chunks.append({
                'doc_id': f'huongdan_p_{len(chunks)}',
                'title': 'Hướng dẫn sử dụng',
                'section': 'Chung',
                'text': text[:800],
            })

    return chunks


def _parse_release_notes(text: str) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    current_title = 'Release notes'
    buf: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            if line.startswith('##'):
                if buf:
                    body = ' '.join(buf).strip()
                    if body:
                        chunks.append({
                            'doc_id': f'release_{len(chunks)}',
                            'title': current_title,
                            'section': 'Phiên bản',
                            'text': body,
                        })
                    buf = []
                current_title = line.lstrip('#').strip()
            continue
        if line.startswith('-'):
            buf.append(line.lstrip('- '))
    if buf:
        body = ' '.join(buf).strip()
        if body:
            chunks.append({
                'doc_id': f'release_{len(chunks)}',
                'title': current_title,
                'section': 'Phiên bản',
                'text': body,
            })
    return chunks


def _load_all_chunks() -> list[dict[str, str]]:
    huongdan_path = os.path.join(BASE_DIR, 'templates', 'huongdansudung.html')
    notes_path = os.path.join(BASE_DIR, 'data', 'assistant_release_notes.txt')
    mtimes = []
    for p in (huongdan_path, notes_path):
        if os.path.isfile(p):
            mtimes.append(os.path.getmtime(p))
    cache_mtime = max(mtimes) if mtimes else 0.0
    if _CACHE['chunks'] and _CACHE['mtime'] >= cache_mtime:
        return _CACHE['chunks']

    chunks: list[dict[str, str]] = []
    hd = _read_file(huongdan_path)
    if hd:
        chunks.extend(_parse_huongdan_chunks(hd))
    notes = _read_file(notes_path)
    if notes:
        chunks.extend(_parse_release_notes(notes))

    _CACHE['mtime'] = cache_mtime
    _CACHE['chunks'] = chunks
    return chunks


def search_rag(query: str, *, top_k: int = 3, section: str | None = None) -> list[RagChunk]:
    q_tokens = _tokenize(query)
    q_norm = _normalize(query)
    if not q_tokens and not q_norm:
        return []

    results: list[RagChunk] = []
    for raw in _load_all_chunks():
        if section and section.lower() not in (raw.get('section') or '').lower():
            continue
        text = raw['text']
        title = raw.get('title') or ''
        blob = f'{title} {text}'
        blob_norm = _normalize(blob)
        score = 0.0
        if q_norm and q_norm in blob_norm:
            score += 4.0
        t_tokens = _tokenize(blob)
        overlap = len(q_tokens & t_tokens)
        score += overlap * 1.5
        for tok in q_tokens:
            if len(tok) >= 4 and tok in blob_norm:
                score += 0.4
        if score >= 2.0:
            results.append(RagChunk(
                doc_id=raw['doc_id'],
                title=title,
                section=raw.get('section') or '',
                text=text,
                score=score,
            ))

    results.sort(key=lambda c: c.score, reverse=True)
    return results[:top_k]


def rag_context_for_prompt(query: str, *, section: str | None = None) -> str:
    hits = search_rag(query, top_k=3, section=section)
    if not hits:
        return ''
    lines = ['Tài liệu tham khảo nội bộ (ưu tiên trả lời theo đây):']
    for i, h in enumerate(hits, 1):
        lines.append(f'{i}. [{h.section}] {h.title}: {h.text[:500]}')
    return '\n'.join(lines)
