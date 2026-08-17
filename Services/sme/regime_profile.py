"""Hồ sơ chế độ kế toán SME (TT99 vs TT58 Micro) — BCTC / filing / UI."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from Services.sme.tt58_tax_methods import (
    DEFAULT_TT58_TAX_METHOD,
    get_tt58_tax_method_def,
    list_tt58_tax_methods,
    normalize_tt58_tax_method,
)


def _read_meta(conn: sqlite3.Connection, keys: tuple[str, ...]) -> dict[str, str]:
    try:
        ph = ','.join('?' * len(keys))
        rows = conn.execute(
            f'SELECT key, value FROM sme_coa_seed_meta WHERE key IN ({ph})',
            keys,
        ).fetchall()
        return {
            (r[0] if not isinstance(r, sqlite3.Row) else r['key']):
            str((r[1] if not isinstance(r, sqlite3.Row) else r['value']) or '')
            for r in rows
        }
    except sqlite3.Error:
        return {}


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_coa_seed_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
        """
    )


def set_tt58_tax_method(
    conn: sqlite3.Connection,
    method: str,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """Lưu phương pháp nộp thuế TT58 (Điều 5–8)."""
    code = normalize_tt58_tax_method(method)
    _ensure_meta_table(conn)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """
        INSERT INTO sme_coa_seed_meta(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ('tt58_tax_method', code, now),
    )
    if commit:
        conn.commit()
    return get_tt58_tax_method_def(code)


def get_ledger_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    profile = 'sme_tt99'
    regime = 'SME_TT99'
    tax_method_raw = ''
    meta = _read_meta(
        conn, ('ledger_profile', 'accounting_regime', 'tt58_tax_method'),
    )
    if meta.get('ledger_profile'):
        profile = str(meta['ledger_profile'])
    if meta.get('accounting_regime'):
        regime = str(meta['accounting_regime'])
    tax_method_raw = meta.get('tt58_tax_method') or ''

    # Fallback registry session nếu meta trống
    if 'TT58' not in regime.upper() and 'tt58' not in profile.lower():
        try:
            from flask import has_request_context, session
            if has_request_context():
                from Services.tenant_profile import get_current_tenant_profile, normalize_accounting_regime
                reg = normalize_accounting_regime(
                    (get_current_tenant_profile() or {}).get('accounting_regime') or ''
                )
                if 'TT58' in reg.upper():
                    regime = reg
                    profile = 'sme_tt58'
        except Exception:
            pass

    is_tt58 = 'tt58' in profile.lower() or 'TT58' in regime.upper()
    if is_tt58:
        tax_code = normalize_tt58_tax_method(tax_method_raw or DEFAULT_TT58_TAX_METHOD)
        tax_def = get_tt58_tax_method_def(tax_code)
        show_bctc = bool(tax_def.get('show_bctc'))
        require_bctc = bool(tax_def.get('require_bctc'))
        bctc_hint = (
            f"PP{tax_def['method_no']} (Điều {tax_def['article']}): "
            + (
                'Bắt buộc lập B01-DNSN / B02-DNSN năm (TNDN trên thu nhập tính thuế).'
                if require_bctc else
                'Không bắt buộc lập BCTC nộp CQNN (TNDN theo % doanh thu). '
                'Chỉ hiển thị sổ/biểu mẫu theo phương pháp đã chọn.'
            )
        )
        return {
            'ledger_profile': 'sme_tt58',
            'accounting_regime': regime if 'TT58' in regime.upper() else 'SME_MICRO_TT58',
            'is_tt58_micro': True,
            'form_set': 'tt58_dnsn',
            'tt58_tax_method': tax_code,
            'tt58_tax_method_def': tax_def,
            'tt58_tax_methods': list_tt58_tax_methods(),
            'required_books': list(tax_def.get('required_books') or ()),
            'optional_books': list(tax_def.get('optional_books') or ()),
            'show_vouchers': bool(tax_def.get('show_vouchers')),
            'show_bctc': show_bctc,
            'require_bctc': require_bctc,
            'bctc_forms': ['B01-DNSN', 'B02-DNSN'] if show_bctc else [],
            'default_vat_filing_mode': 'quarterly',
            'label': 'TT58 (doanh nghiệp siêu nhỏ)',
            'bctc_hint': bctc_hint,
            'legal_source': 'TT58/2026/TT-BTC',
        }
    return {
        'ledger_profile': profile or 'sme_tt99',
        'accounting_regime': regime or 'SME_TT99',
        'is_tt58_micro': False,
        'form_set': 'tt99_dn',
        'tt58_tax_method': None,
        'tt58_tax_method_def': None,
        'tt58_tax_methods': [],
        'required_books': [],
        'optional_books': [],
        'show_vouchers': True,
        'show_bctc': True,
        'require_bctc': True,
        'bctc_forms': ['B01-DN', 'B02-DN', 'B03-DN', 'B09-DN'],
        'default_vat_filing_mode': 'quarterly',
        'label': 'TT99 (doanh nghiệp vừa và nhỏ)',
        'bctc_hint': (
            'TT99/2025/TT-BTC: Bộ BCTC đầy đủ B01–B09-DN · '
            'GTGT mặc định theo quý (DT ≤ 50 tỷ); > 50 tỷ kê khai tháng.'
        ),
        'legal_source': 'TT99/2025/TT-BTC',
    }


def default_vat_filing_mode(conn: sqlite3.Connection) -> str:
    return get_ledger_profile(conn)['default_vat_filing_mode']
