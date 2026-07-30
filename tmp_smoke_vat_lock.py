"""Smoke: VAT settlement + period lock for SME."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Services.sme.bootstrap import ensure_sme_accounting_ready
from Services.sme.journal_engine import post_journal_entry
from Services.sme.period_lock import is_period_locked, unlock_period
from Services.sme.vat_settlement import run_vat_settlement
from Services.sme.auto_posting import run_period_automation


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_sme_accounting_ready(conn, commit=True)
    return conn


def _post(conn, *, date, lines, doc_type='TEST', doc_no='T1', doc_id=1, biz='TEST'):
    return post_journal_entry(
        conn,
        posting_date=date,
        document_date=date,
        document_type=doc_type,
        document_no=doc_no,
        document_id=doc_id,
        business_type=biz,
        description='smoke',
        created_by='smoke',
        lines=lines,
    )


def main():
    conn = _conn()
    year, period = 2026, 7
    date = '2026-07-15'

    # Input VAT 13311 = 100; output VAT 33311 = 150 → offset 100, payable 50
    _post(conn, date=date, doc_type='MUA', doc_no='M1', doc_id=1, biz='MUA', lines=[
        {'sequence': 1, 'account_code': '13311', 'debit': 100, 'credit': 0, 'description': 'VAT in'},
        {'sequence': 2, 'account_code': '1111', 'debit': 0, 'credit': 100, 'description': 'cash'},
    ])
    _post(conn, date=date, doc_type='BAN', doc_no='B1', doc_id=2, biz='BAN', lines=[
        {'sequence': 1, 'account_code': '1111', 'debit': 150, 'credit': 0, 'description': 'cash'},
        {'sequence': 2, 'account_code': '33311', 'debit': 0, 'credit': 150, 'description': 'VAT out'},
    ])
    conn.commit()

    features = {
        'journal_posting': True,
        'auto_depreciation': True,
        'auto_period_close': True,
        'auto_vat_settlement': True,
        'auto_lock_period': True,
    }

    vat = run_vat_settlement(
        conn,
        fiscal_year=year,
        period=period,
        accounting_regime='SME_TT99',
        features=features,
        created_by='smoke',
    )
    assert vat.get('posted'), vat
    assert abs(float(vat['offset_amount']) - 100) < 0.01, vat
    assert abs(float(vat['vat_payable']) - 50) < 0.01, vat
    print('OK vat settle', vat['entry_id'], 'offset', vat['offset_amount'], 'payable', vat['vat_payable'])

    # Full period automation should lock (KH/PB may be empty)
    auto = run_period_automation(
        conn,
        fiscal_year=year,
        period=period,
        accounting_regime='SME_TT99',
        features=features,
        created_by='smoke',
        replace_existing=True,
        auto_activate=False,
    )
    assert is_period_locked(conn, year, period), auto
    assert auto.get('period_lock'), auto
    print('OK auto locked', auto['period_lock']['locked_at'])

    # Posting must be blocked
    blocked = False
    try:
        _post(conn, date='2026-07-20', doc_type='X', doc_no='X1', doc_id=99, lines=[
            {'sequence': 1, 'account_code': '1111', 'debit': 1, 'credit': 0, 'description': 'x'},
            {'sequence': 2, 'account_code': '5111', 'debit': 0, 'credit': 1, 'description': 'x'},
        ])
    except ValueError as e:
        msg = str(e)
        blocked = '07/2026' in msg and ('kh' in msg.lower() or 'SME_auto_posting' in msg)
        print('OK blocked period lock')
    assert blocked, 'expected period lock error'

    # Replace unlocks and re-runs
    auto2 = run_period_automation(
        conn,
        fiscal_year=year,
        period=period,
        accounting_regime='SME_TT99',
        features=features,
        created_by='smoke',
        replace_existing=True,
        auto_activate=False,
    )
    assert is_period_locked(conn, year, period), auto2
    print('OK replace+relock', auto2.get('vat_settlement', {}).get('reason') or auto2.get('vat_settlement', {}).get('entry_id'))

    unlock_period(conn, fiscal_year=year, period=period)
    assert not is_period_locked(conn, year, period)
    print('OK unlock')

    # COA class on 33311
    row = conn.execute(
        "SELECT account_class, normal_balance FROM sme_chart_of_accounts WHERE code='33311'"
    ).fetchone()
    assert row['account_class'] == 'liability' and row['normal_balance'] == 'credit', dict(row)
    print('OK COA 33311', dict(row))
    print('ALL PASSED')


if __name__ == '__main__':
    main()
