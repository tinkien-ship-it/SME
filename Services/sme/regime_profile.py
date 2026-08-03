"""Hồ sơ chế độ kế toán SME (TT99 vs TT58 Micro) — mặc định kê khai / UI."""
from __future__ import annotations

import sqlite3
from typing import Any


def get_ledger_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    profile = 'sme_tt99'
    regime = 'SME_TT99'
    try:
        rows = conn.execute(
            "SELECT key, value FROM sme_coa_seed_meta WHERE key IN ('ledger_profile','accounting_regime')"
        ).fetchall()
        meta = {
            (r[0] if not isinstance(r, sqlite3.Row) else r['key']):
            (r[1] if not isinstance(r, sqlite3.Row) else r['value'])
            for r in rows
        }
        if meta.get('ledger_profile'):
            profile = str(meta['ledger_profile'])
        if meta.get('accounting_regime'):
            regime = str(meta['accounting_regime'])
    except sqlite3.Error:
        pass

    is_tt58 = 'tt58' in profile.lower() or 'TT58' in regime.upper()
    return {
        'ledger_profile': profile,
        'accounting_regime': regime,
        'is_tt58_micro': is_tt58,
        # Cả TT58/TT99: mặc định quý khi DT ≤ 50 tỷ; > 50 tỷ → tháng
        'default_vat_filing_mode': 'quarterly',
        'label': 'TT58 (siêu nhỏ)' if is_tt58 else 'TT99 (vừa và nhỏ)',
        'bctc_hint': (
            'TT58: sổ kép · GTGT mặc định theo quý (DT ≤ 50 tỷ); > 50 tỷ kê khai tháng.'
            if is_tt58 else
            'TT99: sổ kép đầy đủ · GTGT mặc định theo quý (DT ≤ 50 tỷ); > 50 tỷ kê khai tháng.'
        ),
    }


def default_vat_filing_mode(conn: sqlite3.Connection) -> str:
    return get_ledger_profile(conn)['default_vat_filing_mode']
