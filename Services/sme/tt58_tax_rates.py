"""Thuế suất GTGT / TNDN cho DNSN siêu nhỏ (TT58) — theo nhóm ngành + lịch hiệu lực."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

# Nhóm ngành mặc định (tương tự biểu % pháp luật thuế trên doanh thu)
DEFAULT_SECTORS: tuple[dict[str, Any], ...] = (
    {
        'key': 'goods',
        'label': 'Phân phối, cung cấp hàng hóa',
        'vat_pct': 1.0,
        'cit_pct_revenue': 0.5,
    },
    {
        'key': 'service',
        'label': 'Dịch vụ, xây dựng không gồm nguyên vật liệu',
        'vat_pct': 5.0,
        'cit_pct_revenue': 2.0,
    },
    {
        'key': 'production',
        'label': 'Sản xuất, vận tải, dịch vụ có gắn hàng hóa',
        'vat_pct': 3.0,
        'cit_pct_revenue': 1.5,
    },
    {
        'key': 'other',
        'label': 'Hoạt động kinh doanh khác',
        'vat_pct': 2.0,
        'cit_pct_revenue': 1.0,
    },
)

# Thuế TNDN trên thu nhập tính thuế (PP2 / PP4) — mặc định 15% (DN siêu nhỏ thường gặp)
DEFAULT_CIT_INCOME_PCT = 15.0

CIT_INCOME_KEY = '__cit_income__'


def ensure_tt58_tax_rates_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
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
    # Seed nếu trống
    n = conn.execute('SELECT COUNT(*) FROM sme_tt58_tax_rates').fetchone()[0]
    if not n:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for s in DEFAULT_SECTORS:
            conn.execute(
                """
                INSERT INTO sme_tt58_tax_rates
                    (sector_key, vat_pct, cit_pct_revenue, cit_pct_income,
                     effective_from, note, created_by, created_at)
                VALUES (?, ?, ?, NULL, '2020-01-01', 'Mặc định hệ thống', 'system', ?)
                """,
                (s['key'], s['vat_pct'], s['cit_pct_revenue'], now),
            )
        conn.execute(
            """
            INSERT INTO sme_tt58_tax_rates
                (sector_key, vat_pct, cit_pct_revenue, cit_pct_income,
                 effective_from, note, created_by, created_at)
            VALUES (?, NULL, NULL, ?, '2020-01-01', 'Mặc định TNDN trên thu nhập', 'system', ?)
            """,
            (CIT_INCOME_KEY, DEFAULT_CIT_INCOME_PCT, now),
        )
    if commit:
        conn.commit()


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
    """Trả cấu hình thuế suất đang hiệu lực."""
    ensure_tt58_tax_rates_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    day = _as_of(as_of)
    sectors = []
    for s in DEFAULT_SECTORS:
        row = _latest_row(conn, s['key'], day)
        sectors.append({
            'key': s['key'],
            'label': s['label'],
            'vat_pct': float(row['vat_pct']) if row and row['vat_pct'] is not None else s['vat_pct'],
            'cit_pct_revenue': float(row['cit_pct_revenue']) if row and row['cit_pct_revenue'] is not None else s['cit_pct_revenue'],
            'effective_from': (row['effective_from'] if row else '2020-01-01'),
        })
    inc = _latest_row(conn, CIT_INCOME_KEY, day)
    cit_income = float(inc['cit_pct_income']) if inc and inc['cit_pct_income'] is not None else DEFAULT_CIT_INCOME_PCT
    return {
        'as_of': day,
        'sectors': sectors,
        'cit_pct_income': cit_income,
        'cit_income_effective_from': (inc['effective_from'] if inc else '2020-01-01'),
        'defaults': {
            'sectors': [dict(s) for s in DEFAULT_SECTORS],
            'cit_pct_income': DEFAULT_CIT_INCOME_PCT,
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


def get_cit_income_rate_pct(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
) -> float:
    return float(get_tt58_tax_rates(conn, as_of=as_of)['cit_pct_income'])


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
        for s in sectors:
            key = (s.get('key') or '').strip()
            if not key or key == CIT_INCOME_KEY:
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
        conn.commit()
    return get_tt58_tax_rates(conn, as_of=day)


def rates_ui_context_for_method(method_code: str | None) -> dict[str, Any]:
    """Gợi ý field nào hiện trong modal theo PP thuế."""
    from Services.sme.tt58_tax_methods import get_tt58_tax_method_def, normalize_tt58_tax_method
    td = get_tt58_tax_method_def(normalize_tt58_tax_method(method_code))
    vat_mode = td.get('vat_mode')
    cit_mode = td.get('cit_mode')
    return {
        'method': td.get('code'),
        'method_label': td.get('short_label'),
        'show_vat_pct_revenue': vat_mode == 'pct_revenue',  # PP1, PP2
        'show_cit_pct_revenue': cit_mode == 'pct_revenue',  # PP1, PP3
        'show_cit_pct_income': cit_mode == 'taxable_income',  # PP2, PP4
        'vat_mode': vat_mode,
        'cit_mode': cit_mode,
        'hint': (
            'GTGT theo % doanh thu theo nhóm ngành; TNDN theo % doanh thu.'
            if vat_mode == 'pct_revenue' and cit_mode == 'pct_revenue' else
            'GTGT theo % doanh thu; TNDN = thuế suất × (Doanh thu − Chi phí được trừ).'
            if vat_mode == 'pct_revenue' and cit_mode == 'taxable_income' else
            'GTGT khấu trừ (theo hóa đơn); TNDN theo % doanh thu theo nhóm ngành.'
            if vat_mode == 'deduction' and cit_mode == 'pct_revenue' else
            'GTGT khấu trừ (theo hóa đơn); TNDN = thuế suất × thu nhập tính thuế.'
        ),
    }
