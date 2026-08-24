"""Thuế suất GTGT / TNDN DNSN siêu nhỏ — TT58/2026/TT-BTC (bảng 4 trường hợp).

Hiệu lực thông tư: 01/07/2026. Số liệu mặc định theo bảng tóm tắt:
  A. GTGT % trên doanh thu — Trường hợp 1 & 2
  B. TNDN % trên doanh thu — Trường hợp 1 & 3
  C. TNDN trên thu nhập tính thuế — Trường hợp 2 & 4 (bậc 15 / 17%, tối đa DT 10 tỷ)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from db_utils import sqlite_commit

OFFICIAL_EFFECTIVE_FROM = '2026-07-01'
SEED_NOTE = 'TT58/2026/TT-BTC — bảng 4 trường hợp DNSN'

# A + B: nhóm ngành (VAT áp dụng TH1/TH2; CIT % DT áp dụng TH1/TH3)
DEFAULT_SECTORS: tuple[dict[str, Any], ...] = (
    {
        'key': 'goods',
        'label': 'Phân phối, cung cấp hàng hóa',
        'vat_pct': 1.0,
        'cit_pct_revenue': 0.3,
        'in_vat_table': True,
        'in_cit_table': True,
    },
    {
        'key': 'production',
        'label': 'Sản xuất, vận tải, dịch vụ / xây dựng có nguyên vật liệu',
        'vat_pct': 3.0,
        'cit_pct_revenue': 1.2,
        'in_vat_table': True,
        'in_cit_table': True,
    },
    {
        'key': 'service',
        'label': 'Dịch vụ, xây dựng không gồm nguyên vật liệu',
        'vat_pct': 5.0,
        'cit_pct_revenue': 1.5,
        'in_vat_table': True,
        'in_cit_table': True,
    },
    {
        'key': 'leasing',
        'label': 'Cho thuê tài sản, đại lý bảo hiểm / xổ số / bán hàng đa cấp',
        'vat_pct': 5.0,
        'cit_pct_revenue': 4.0,
        'in_vat_table': True,
        'in_cit_table': True,
    },
    {
        'key': 'digital',
        'label': 'Hoạt động nội dung số (nhạc, game, quảng cáo…)',
        'vat_pct': 5.0,  # không tách riêng ở bảng GTGT — áp dụng như dịch vụ
        'cit_pct_revenue': 4.0,
        'in_vat_table': False,
        'in_cit_table': True,
    },
    {
        'key': 'other',
        'label': 'Hoạt động kinh doanh khác',
        'vat_pct': 2.0,
        'cit_pct_revenue': 0.5,
        'in_vat_table': True,
        'in_cit_table': True,
    },
)

# C. TNDN trên thu nhập tính thuế (TH2 / TH4)
# TT58/DNSN chỉ thiết lập đến doanh thu 10 tỷ: ≤ 3 tỷ → 15%; > 3 đến 10 tỷ → 17%.
# Doanh thu > 10 tỷ không áp dụng TT58 — chuyển TT99 (không liệt kê bậc > 50 tỷ / 20%).
TT58_MAX_REVENUE = 10_000_000_000

CIT_INCOME_BRACKETS: tuple[dict[str, Any], ...] = (
    {
        'key': 'le3b',
        'label': 'Tổng doanh thu năm ≤ 3 tỷ đồng',
        'max_revenue': 3_000_000_000,
        'pct': 15.0,
        'db_key': '__cit_bracket_le3b__',
    },
    {
        'key': 'gt3_le10b',
        'label': 'Tổng doanh thu năm > 3 tỷ đến 10 tỷ đồng',
        'max_revenue': TT58_MAX_REVENUE,
        'pct': 17.0,
        'db_key': '__cit_bracket_gt3_le50b__',
    },
)

DEFAULT_CIT_INCOME_PCT = 15.0
CIT_COMMON_PCT = 20.0
CIT_INCOME_KEY = '__cit_income__'  # legacy 1 mức — vẫn đọc nếu chưa có bậc

# Tránh CREATE/INSERT trên mọi GET — SQLite locked khi Flask reloader + request song song.
_SCHEMA_READY: set[str] = set()
_EXPECTED_OFFICIAL_KEYS = (
    tuple(s['key'] for s in DEFAULT_SECTORS)
    + tuple(b['db_key'] for b in CIT_INCOME_BRACKETS)
    + (CIT_INCOME_KEY,)
)


def _db_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
        if row:
            path = row[2] if not isinstance(row, sqlite3.Row) else row['file']
            if path:
                return str(path)
    except sqlite3.Error:
        pass
    return f'conn:{id(conn)}'


def _is_locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return 'database is locked' in msg or 'database table is locked' in msg


def _rates_from_defaults(as_of: str) -> dict[str, Any]:
    day = _as_of(as_of)
    sectors = [
        {
            'key': s['key'],
            'label': s['label'],
            'vat_pct': s['vat_pct'],
            'cit_pct_revenue': s['cit_pct_revenue'],
            'in_vat_table': bool(s.get('in_vat_table', True)),
            'in_cit_table': bool(s.get('in_cit_table', True)),
            'effective_from': OFFICIAL_EFFECTIVE_FROM,
        }
        for s in DEFAULT_SECTORS
    ]
    brackets = [
        {
            'key': b['key'],
            'label': b['label'],
            'max_revenue': b['max_revenue'],
            'pct': b['pct'],
            'effective_from': OFFICIAL_EFFECTIVE_FROM,
        }
        for b in CIT_INCOME_BRACKETS
    ]
    return {
        'as_of': day,
        'official_from': OFFICIAL_EFFECTIVE_FROM,
        'legal_source': 'TT58/2026/TT-BTC',
        'sectors': sectors,
        'cit_income_brackets': brackets,
        'cit_common_pct': CIT_COMMON_PCT,
        'cit_pct_income': DEFAULT_CIT_INCOME_PCT,
        'cit_income_effective_from': OFFICIAL_EFFECTIVE_FROM,
        'defaults': {
            'sectors': [dict(s) for s in DEFAULT_SECTORS],
            'cit_income_brackets': [dict(b) for b in CIT_INCOME_BRACKETS],
            'cit_pct_income': DEFAULT_CIT_INCOME_PCT,
            'cit_common_pct': CIT_COMMON_PCT,
        },
    }


def ensure_tt58_tax_rates_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    key = _db_key(conn)
    if key in _SCHEMA_READY:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_tt58_tax_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector_key TEXT NOT NULL,
            vat_pct REAL,
            cit_pct_revenue REAL,
            cit_pct_income REAL,
            effective_from TEXT NOT NULL,
            note TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(sector_key, effective_from)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sme_tt58_tax_rates_lookup "
        "ON sme_tt58_tax_rates(sector_key, effective_from)"
    )
    wrote = _seed_official_rates(conn)
    # Seed phải commit ngay — GET thường close() không commit, rollback sẽ mất dữ liệu
    # rồi cache _SCHEMA_READY khiến lần sau không seed nữa.
    if wrote or commit:
        sqlite_commit(conn, label='tt58_tax_rates')
    _SCHEMA_READY.add(key)


def _seed_official_rates(conn: sqlite3.Connection) -> bool:
    """Chỉ INSERT dòng còn thiếu. Trả True nếu đã ghi."""
    existing = {
        r[0]
        for r in conn.execute(
            """
            SELECT sector_key FROM sme_tt58_tax_rates
            WHERE effective_from = ?
            """,
            (OFFICIAL_EFFECTIVE_FROM,),
        ).fetchall()
    }
    missing = [k for k in _EXPECTED_OFFICIAL_KEYS if k not in existing]
    if not missing:
        return False

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    by_sector = {s['key']: s for s in DEFAULT_SECTORS}
    by_bracket = {b['db_key']: b for b in CIT_INCOME_BRACKETS}
    rows: list[tuple] = []
    for key in missing:
        if key == CIT_INCOME_KEY:
            rows.append((
                key, None, None, DEFAULT_CIT_INCOME_PCT,
                OFFICIAL_EFFECTIVE_FROM, SEED_NOTE, 'system', now,
            ))
            continue
        s = by_sector.get(key)
        if s:
            rows.append((
                s['key'], s['vat_pct'], s['cit_pct_revenue'], None,
                OFFICIAL_EFFECTIVE_FROM, SEED_NOTE, 'system', now,
            ))
            continue
        b = by_bracket.get(key)
        if b:
            rows.append((
                b['db_key'], None, None, b['pct'],
                OFFICIAL_EFFECTIVE_FROM, SEED_NOTE, 'system', now,
            ))
    for row in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO sme_tt58_tax_rates
                (sector_key, vat_pct, cit_pct_revenue, cit_pct_income,
                 effective_from, note, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
    return bool(rows)


def _as_of(date_s: str | None) -> str:
    if date_s and len(str(date_s)) >= 10:
        return str(date_s)[:10]
    return datetime.now().strftime('%Y-%m-%d')


def _latest_row(
    conn: sqlite3.Connection, sector_key: str, as_of: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM sme_tt58_tax_rates
        WHERE sector_key = ? AND effective_from <= ?
        ORDER BY effective_from DESC, id DESC
        LIMIT 1
        """,
        (sector_key, as_of),
    ).fetchone()
    return dict(row) if row else None


def get_tt58_tax_rates(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Trả cấu hình thuế suất đang hiệu lực (bảng A/B/C).

    GET không được 500 vì database locked: nếu không ghi/đọc được thì trả mặc định.
    """
    day = _as_of(as_of)
    try:
        ensure_tt58_tax_rates_schema(conn, commit=False)
    except sqlite3.OperationalError as exc:
        if not _is_locked(exc):
            raise
        return _rates_from_defaults(day)

    conn.row_factory = sqlite3.Row
    try:
        sectors = []
        for s in DEFAULT_SECTORS:
            row = _latest_row(conn, s['key'], day)
            sectors.append({
                'key': s['key'],
                'label': s['label'],
                'vat_pct': float(row['vat_pct']) if row and row['vat_pct'] is not None else s['vat_pct'],
                'cit_pct_revenue': (
                    float(row['cit_pct_revenue'])
                    if row and row['cit_pct_revenue'] is not None
                    else s['cit_pct_revenue']
                ),
                'in_vat_table': bool(s.get('in_vat_table', True)),
                'in_cit_table': bool(s.get('in_cit_table', True)),
                'effective_from': (row['effective_from'] if row else OFFICIAL_EFFECTIVE_FROM),
            })

        brackets = []
        for b in CIT_INCOME_BRACKETS:
            row = _latest_row(conn, b['db_key'], day)
            brackets.append({
                'key': b['key'],
                'label': b['label'],
                'max_revenue': b['max_revenue'],
                'pct': float(row['cit_pct_income']) if row and row['cit_pct_income'] is not None else b['pct'],
                'effective_from': (row['effective_from'] if row else OFFICIAL_EFFECTIVE_FROM),
            })

        inc = _latest_row(conn, CIT_INCOME_KEY, day)
        cit_income = (
            float(inc['cit_pct_income'])
            if inc and inc['cit_pct_income'] is not None
            else brackets[0]['pct']
        )
    except sqlite3.OperationalError as exc:
        if not _is_locked(exc):
            raise
        return _rates_from_defaults(day)

    return {
        'as_of': day,
        'official_from': OFFICIAL_EFFECTIVE_FROM,
        'legal_source': 'TT58/2026/TT-BTC',
        'sectors': sectors,
        'cit_income_brackets': brackets,
        'cit_common_pct': CIT_COMMON_PCT,
        'cit_pct_income': cit_income,
        'cit_income_effective_from': (inc['effective_from'] if inc else OFFICIAL_EFFECTIVE_FROM),
        'defaults': {
            'sectors': [dict(s) for s in DEFAULT_SECTORS],
            'cit_income_brackets': [dict(b) for b in CIT_INCOME_BRACKETS],
            'cit_pct_income': DEFAULT_CIT_INCOME_PCT,
            'cit_common_pct': CIT_COMMON_PCT,
        },
    }


def sector_tax_map(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
) -> dict[str, dict[str, float]]:
    """Map key → {vat_pct, cit_pct, label} dùng trong sổ doanh thu."""
    data = get_tt58_tax_rates(conn, as_of=as_of)
    out = {}
    for s in data['sectors']:
        out[s['key']] = {
            'vat_pct': float(s['vat_pct']),
            'cit_pct': float(s['cit_pct_revenue']),
            'label': s['label'],
        }
    return out


def cit_income_pct_for_revenue(revenue, brackets: list[dict[str, Any]] | None = None) -> float:
    """Chọn bậc 15 / 17% theo tổng doanh thu năm (TT58 — tối đa 10 tỷ)."""
    rev = float(revenue or 0)
    rows = brackets if brackets is not None else [dict(b) for b in CIT_INCOME_BRACKETS]
    ordered = sorted(
        rows,
        key=lambda x: (10**18 if x.get('max_revenue') is None else float(x.get('max_revenue') or 0)),
    )
    for b in ordered:
        cap = b.get('max_revenue')
        if cap is None or rev <= float(cap):
            return float(b.get('pct') or DEFAULT_CIT_INCOME_PCT)
    # Trên 10 tỷ: không áp dụng TT58 — dùng bậc cao nhất còn liệt kê (17%)
    if ordered:
        return float(ordered[-1].get('pct') or DEFAULT_CIT_INCOME_PCT)
    return float(DEFAULT_CIT_INCOME_PCT)


def get_cit_income_rate_pct(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    revenue: float | None = None,
) -> float:
    data = get_tt58_tax_rates(conn, as_of=as_of)
    if revenue is not None:
        return cit_income_pct_for_revenue(revenue, data.get('cit_income_brackets'))
    return float(data.get('cit_pct_income') or DEFAULT_CIT_INCOME_PCT)


def list_tt58_tax_rate_history(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_tt58_tax_rates_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT * FROM sme_tt58_tax_rates
        ORDER BY effective_from DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_tt58_tax_rates(
    conn: sqlite3.Connection,
    *,
    sectors: list[dict[str, Any]] | None = None,
    cit_pct_income: float | None = None,
    cit_income_brackets: list[dict[str, Any]] | None = None,
    effective_from: str | None = None,
    note: str | None = None,
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Ghi lịch thuế suất mới (không xóa bản cũ — theo dõi thay đổi)."""
    ensure_tt58_tax_rates_schema(conn, commit=False)
    day = _as_of(effective_from)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    note_s = (note or 'Cập nhật thuế suất').strip()
    user = (created_by or '').strip() or 'user'

    if sectors:
        known = {s['key'] for s in DEFAULT_SECTORS}
        for s in sectors:
            key = (s.get('key') or '').strip()
            if not key or key == CIT_INCOME_KEY or key.startswith('__cit_'):
                continue
            if key not in known:
                continue
            vat = s.get('vat_pct')
            cit_r = s.get('cit_pct_revenue', s.get('cit_pct'))
            conn.execute(
                """
                INSERT INTO sme_tt58_tax_rates
                    (sector_key, vat_pct, cit_pct_revenue, cit_pct_income,
                     effective_from, note, created_by, created_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                ON CONFLICT(sector_key, effective_from) DO UPDATE SET
                    vat_pct = excluded.vat_pct,
                    cit_pct_revenue = excluded.cit_pct_revenue,
                    note = excluded.note,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at
                """,
                (
                    key,
                    float(vat) if vat is not None else None,
                    float(cit_r) if cit_r is not None else None,
                    day, note_s, user, now,
                ),
            )

    if cit_income_brackets:
        by_key = {b['key']: b for b in CIT_INCOME_BRACKETS}
        by_key['gt3_le50b'] = by_key.get('gt3_le10b')  # legacy API key
        for item in cit_income_brackets:
            bk = (item.get('key') or '').strip()
            spec = by_key.get(bk)
            if not spec:
                continue
            pct = item.get('pct', item.get('cit_pct_income'))
            if pct is None:
                continue
            conn.execute(
                """
                INSERT INTO sme_tt58_tax_rates
                    (sector_key, vat_pct, cit_pct_revenue, cit_pct_income,
                     effective_from, note, created_by, created_at)
                VALUES (?, NULL, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(sector_key, effective_from) DO UPDATE SET
                    cit_pct_income = excluded.cit_pct_income,
                    note = excluded.note,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at
                """,
                (spec['db_key'], float(pct), day, note_s, user, now),
            )
        # Đồng bộ mức legacy = bậc ≤ 3 tỷ (siêu nhỏ)
        le3 = next((i for i in cit_income_brackets if (i.get('key') or '') == 'le3b'), None)
        if le3 and le3.get('pct') is not None:
            cit_pct_income = float(le3['pct'])

    if cit_pct_income is not None:
        conn.execute(
            """
            INSERT INTO sme_tt58_tax_rates
                (sector_key, vat_pct, cit_pct_revenue, cit_pct_income,
                 effective_from, note, created_by, created_at)
            VALUES (?, NULL, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(sector_key, effective_from) DO UPDATE SET
                cit_pct_income = excluded.cit_pct_income,
                note = excluded.note,
                created_by = excluded.created_by,
                created_at = excluded.created_at
            """,
            (CIT_INCOME_KEY, float(cit_pct_income), day, note_s, user, now),
        )

    if commit:
        sqlite_commit(conn, label='tt58_tax_rates')
    return get_tt58_tax_rates(conn, as_of=day)


def rates_ui_context_for_method(method_code: str | None) -> dict[str, Any]:
    """Gợi ý field nào hiện trong modal theo trường hợp thuế."""
    from Services.sme.tt58_tax_methods import get_tt58_tax_method_def, normalize_tt58_tax_method
    if not (method_code or '').strip():
        return {
            'method': None,
            'method_label': 'Chưa chọn trường hợp',
            'show_vat_pct_revenue': True,
            'show_cit_pct_revenue': True,
            'show_cit_pct_income': True,
            'vat_mode': None,
            'cit_mode': None,
            'hint': (
                'Chọn Trường hợp 1–4 rồi lưu — hệ thống chỉ hiện thuế suất đúng trường hợp '
                '(bảng A GTGT % DT, B TNDN % DT, C TNDN trên thu nhập 15/17% — tối đa DT 10 tỷ).'
            ),
        }
    td = get_tt58_tax_method_def(normalize_tt58_tax_method(method_code))
    vat_mode = td.get('vat_mode')
    cit_mode = td.get('cit_mode')
    return {
        'method': td.get('code'),
        'method_label': td.get('short_label'),
        'case_no': td.get('case_no'),
        'show_vat_pct_revenue': vat_mode == 'pct_revenue',
        'show_cit_pct_revenue': cit_mode == 'pct_revenue',
        'show_cit_pct_income': cit_mode == 'taxable_income',
        'vat_mode': vat_mode,
        'cit_mode': cit_mode,
        'vat_label': td.get('vat_label'),
        'cit_label': td.get('cit_label'),
        'hint': (
            'Bảng A+B: GTGT và TNDN đều theo % doanh thu theo nhóm ngành (Trường hợp 1).'
            if vat_mode == 'pct_revenue' and cit_mode == 'pct_revenue' else
            'Bảng A: GTGT % doanh thu. Bảng C: TNDN 15/17% trên thu nhập tính thuế (Trường hợp 2, DT ≤ 10 tỷ).'
            if vat_mode == 'pct_revenue' and cit_mode == 'taxable_income' else
            'GTGT khấu trừ theo hóa đơn. Bảng B: TNDN % doanh thu theo nhóm ngành (Trường hợp 3).'
            if vat_mode == 'deduction' and cit_mode == 'pct_revenue' else
            'GTGT khấu trừ theo hóa đơn. Bảng C: TNDN 15/17% trên thu nhập tính thuế (Trường hợp 4, DT ≤ 10 tỷ).'
        ),
    }
