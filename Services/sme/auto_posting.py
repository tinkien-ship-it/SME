"""Tự động khấu hao TSCĐ + phân bổ CCDC → bút toán SME theo kỳ."""
from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from Services.profit_report_helpers import depreciation_for_month
from Services.sme.journal_engine import (
    get_posting_rule,
    post_journal_entry,
    resolve_postable_account,
    reverse_journal_entry,
)

DOC_DEP = 'KHTS'
DOC_TOOL = 'PBCC'


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _parse_date(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(text[:19] if ' ' in text and fmt.startswith('%Y-%m-%d %') else text[:10], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def ensure_auto_posting_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_auto_asset_postings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            asset_table TEXT NOT NULL,
            asset_id INTEGER NOT NULL,
            fiscal_year INTEGER NOT NULL,
            period INTEGER NOT NULL,
            amount REAL NOT NULL,
            journal_entry_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(kind, asset_table, asset_id, fiscal_year, period)
        )
        """
    )
    if commit:
        conn.commit()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _depreciable_cost(row: sqlite3.Row) -> float:
    keys = set(row.keys())
    gross = float(row['nguyen_gia_tinh_khau_hao'] or 0) if 'nguyen_gia_tinh_khau_hao' in keys else 0.0
    vat = float(row['thue_gtgt'] or 0) if 'thue_gtgt' in keys else 0.0
    if vat > 0 and gross >= vat:
        return max(0.0, gross - vat)
    qty = float(row['so_luong'] or 1) if 'so_luong' in keys else 1.0
    unit = float(row['gia_mua_chua_thue'] or 0) if 'gia_mua_chua_thue' in keys else 0.0
    if unit > 0:
        return max(0.0, unit * (qty or 1.0))
    return max(0.0, gross)


def _tool_cost(row: sqlite3.Row) -> float:
    keys = set(row.keys())
    gross = float(row['nguyen_gia'] or 0) if 'nguyen_gia' in keys else 0.0
    vat = float(row['thue_gtgt'] or 0) if 'thue_gtgt' in keys else 0.0
    if vat > 0 and gross >= vat:
        return max(0.0, gross - vat)
    qty = float(row['so_luong'] or 1) if 'so_luong' in keys else 1.0
    unit = float(row['gia_mua_chua_thue'] or 0) if 'gia_mua_chua_thue' in keys else 0.0
    if unit > 0:
        return max(0.0, unit * (qty or 1.0))
    return max(0.0, gross)


def _posted_to_date(
    conn: sqlite3.Connection,
    *,
    kind: str,
    asset_table: str,
    asset_id: int,
    before_year: int,
    before_period: int,
) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) FROM sme_auto_asset_postings
        WHERE kind = ? AND asset_table = ? AND asset_id = ?
          AND (fiscal_year < ? OR (fiscal_year = ? AND period < ?))
        """,
        (kind, asset_table, asset_id, before_year, before_year, before_period),
    ).fetchone()
    return float(row[0] or 0)


def _active_period_entry(conn: sqlite3.Connection, document_type: str, document_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE document_type = ? AND document_id = ?
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (document_type, document_id),
    ).fetchone()
    return int(row[0]) if row else None


def auto_activate_idle_assets(
    conn: sqlite3.Connection,
    *,
    default_fa_months: int = 36,
    default_tool_months: int = 12,
) -> dict[str, int]:
    """InStock → Active để tự động trích KH/PB (SME)."""
    from Services.fixed_assets_helpers import (
        FIXED_ASSETS_TABLE,
        STATUS_ACTIVE,
        STATUS_IN_STOCK,
        TOOLS_TABLE,
        ensure_fixed_assets_schema,
    )

    ensure_fixed_assets_schema(conn)
    fa_n = tool_n = 0
    if _table_exists(conn, FIXED_ASSETS_TABLE):
        rows = conn.execute(
            f"SELECT id, ngay_chung_tu, so_thang_khau_hao FROM {FIXED_ASSETS_TABLE} WHERE tinh_trang = ?",
            (STATUS_IN_STOCK,),
        ).fetchall()
        for row in rows:
            start = (row['ngay_chung_tu'] or datetime.now().strftime('%Y-%m-%d'))[:10]
            months = int(row['so_thang_khau_hao'] or 0) or default_fa_months
            conn.execute(
                f"""
                UPDATE {FIXED_ASSETS_TABLE}
                SET tinh_trang = ?, ngay_bat_dau_su_dung = COALESCE(NULLIF(ngay_bat_dau_su_dung,''), ?),
                    so_thang_khau_hao = ?
                WHERE id = ?
                """,
                (STATUS_ACTIVE, start, months, row['id']),
            )
            fa_n += 1
    if _table_exists(conn, TOOLS_TABLE):
        rows = conn.execute(
            f"SELECT id, ngay_nhap, so_thang_phan_bo FROM {TOOLS_TABLE} WHERE tinh_trang = ?",
            (STATUS_IN_STOCK,),
        ).fetchall()
        for row in rows:
            start = (row['ngay_nhap'] or datetime.now().strftime('%Y-%m-%d'))[:10]
            months = int(row['so_thang_phan_bo'] or 0) or default_tool_months
            conn.execute(
                f"""
                UPDATE {TOOLS_TABLE}
                SET tinh_trang = ?, ngay_bat_dau_su_dung = COALESCE(NULLIF(ngay_bat_dau_su_dung,''), ?),
                    so_thang_phan_bo = ?
                WHERE id = ?
                """,
                (STATUS_ACTIVE, start, months, row['id']),
            )
            tool_n += 1
    return {'fixed_assets_activated': fa_n, 'tools_activated': tool_n}


def _build_simple_lines(
    conn: sqlite3.Connection,
    *,
    business_type: str,
    amount: Decimal,
    description: str,
) -> list[dict]:
    rule = get_posting_rule(conn, business_type, 'INTERNAL', commit=False)
    if not rule:
        raise ValueError(f'Chưa có quy tắc {business_type}/INTERNAL')
    debit = resolve_postable_account(conn, rule['debit_account_code'])
    credit = resolve_postable_account(conn, rule['credit_account_code'])
    amt = _money(amount)
    if amt <= 0:
        return []
    return [
        {
            'sequence': 1,
            'account_code': debit,
            'debit': amt,
            'credit': 0,
            'description': description,
        },
        {
            'sequence': 2,
            'account_code': credit,
            'debit': 0,
            'credit': amt,
            'description': description,
        },
    ]


def _collect_fa_amounts(conn: sqlite3.Connection, year: int, month: int) -> list[dict]:
    from Services.fixed_assets_helpers import FIXED_ASSETS_TABLE, STATUS_ACTIVE

    if not _table_exists(conn, FIXED_ASSETS_TABLE):
        return []
    rows = conn.execute(
        f"""
        SELECT id, ma_tai_san, ten_tai_san, nguyen_gia_tinh_khau_hao, thue_gtgt,
               gia_mua_chua_thue, so_luong, so_thang_khau_hao, ngay_bat_dau_su_dung
        FROM {FIXED_ASSETS_TABLE} WHERE tinh_trang = ?
        """,
        (STATUS_ACTIVE,),
    ).fetchall()
    out = []
    for row in rows:
        start = _parse_date(row['ngay_bat_dau_su_dung'])
        cost = _depreciable_cost(row)
        months = int(row['so_thang_khau_hao'] or 0)
        if not start or cost <= 0 or months <= 0:
            continue
        amount = depreciation_for_month(cost, months, start, year, month)
        prior = _posted_to_date(
            conn, kind='DEPRECIATION', asset_table=FIXED_ASSETS_TABLE,
            asset_id=int(row['id']), before_year=year, before_period=month,
        )
        remain = max(0.0, cost - prior)
        amount = min(float(amount), remain)
        if amount <= 0:
            continue
        out.append({
            'asset_id': int(row['id']),
            'code': row['ma_tai_san'],
            'name': row['ten_tai_san'],
            'amount': _money(amount),
        })
    return out


def _tool_alloc_for_month(cost: float, so_thang: int, start: datetime, year: int, month: int) -> float:
    """Phân bổ CCDC đường thẳng theo tháng (giống KH, dùng chung công thức)."""
    return depreciation_for_month(cost, so_thang, start, year, month)


def _collect_tool_amounts(conn: sqlite3.Connection, year: int, month: int) -> list[dict]:
    from Services.fixed_assets_helpers import STATUS_ACTIVE, TOOLS_TABLE

    if not _table_exists(conn, TOOLS_TABLE):
        return []
    rows = conn.execute(
        f"""
        SELECT id, ma_ccdc, ten_ccdc, nguyen_gia, thue_gtgt, gia_mua_chua_thue,
               so_luong, so_thang_phan_bo, ngay_bat_dau_su_dung
        FROM {TOOLS_TABLE} WHERE tinh_trang = ?
        """,
        (STATUS_ACTIVE,),
    ).fetchall()
    out = []
    for row in rows:
        start = _parse_date(row['ngay_bat_dau_su_dung'])
        cost = _tool_cost(row)
        months = int(row['so_thang_phan_bo'] or 0)
        if not start or cost <= 0 or months <= 0:
            continue
        amount = _tool_alloc_for_month(cost, months, start, year, month)
        prior = _posted_to_date(
            conn, kind='TOOLS_ALLOC', asset_table=TOOLS_TABLE,
            asset_id=int(row['id']), before_year=year, before_period=month,
        )
        remain = max(0.0, cost - prior)
        amount = min(float(amount), remain)
        if amount <= 0:
            continue
        out.append({
            'asset_id': int(row['id']),
            'code': row['ma_ccdc'],
            'name': row['ten_ccdc'],
            'amount': _money(amount),
        })
    return out


def run_period_automation(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    accounting_regime: str | None = None,
    features: dict | None = None,
    created_by: str | None = None,
    replace_existing: bool = False,
    auto_activate: bool = True,
) -> dict[str, Any]:
    """
    Chạy tự động 1 kỳ: KH TSCĐ + PB CCDC + kết chuyển KQKD + quyết toán GTGT + khóa sổ.
    Không commit.
    """
    regime = str(accounting_regime or '').upper()
    if features is not None:
        if not features.get('journal_posting'):
            return {'posted': False, 'reason': 'journal_posting_disabled'}
        if features.get('auto_depreciation') is False:
            return {'posted': False, 'reason': 'auto_depreciation_disabled'}
    elif not regime.startswith('SME'):
        return {'posted': False, 'reason': 'not_sme'}

    if period < 1 or period > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    from Services.fixed_assets_helpers import FIXED_ASSETS_TABLE, TOOLS_TABLE
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.period_lock import get_period_lock, is_period_locked, lock_period, unlock_period

    ensure_sme_accounting_ready(conn, commit=False)
    ensure_auto_posting_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row

    if is_period_locked(conn, fiscal_year, period):
        if not replace_existing:
            return {
                'posted': False,
                'reason': 'period_locked',
                'fiscal_year': fiscal_year,
                'period': period,
                'period_close': {},
                'vat_settlement': {},
                'period_lock': get_period_lock(conn, fiscal_year, period),
            }
        unlock_period(conn, fiscal_year=fiscal_year, period=period)

    activated = {'fixed_assets_activated': 0, 'tools_activated': 0}
    if auto_activate:
        activated = auto_activate_idle_assets(conn)

    doc_id = fiscal_year * 100 + period
    last_day = calendar.monthrange(fiscal_year, period)[1]
    posting_date = f'{fiscal_year:04d}-{period:02d}-{last_day:02d}'
    reversed_ids: list[int] = []

    def _clear_kind(kind: str, doc_type: str, asset_table: str):
        entry_id = _active_period_entry(conn, doc_type, doc_id)
        if entry_id and replace_existing:
            rev = reverse_journal_entry(
                conn,
                entry_id,
                posting_date=posting_date,
                created_by=created_by,
                reason=f'Thay thế bút toán tự động {doc_type} {fiscal_year}/{period:02d}',
            )
            reversed_ids.append(int(rev['id']))
            conn.execute(
                """
                DELETE FROM sme_auto_asset_postings
                WHERE kind = ? AND asset_table = ? AND fiscal_year = ? AND period = ?
                """,
                (kind, asset_table, fiscal_year, period),
            )
            return None
        return entry_id

    result = {
        'posted': False,
        'fiscal_year': fiscal_year,
        'period': period,
        'posting_date': posting_date,
        'activated': activated,
        'depreciation': {'entry_id': None, 'amount': 0.0, 'assets': 0},
        'tools': {'entry_id': None, 'amount': 0.0, 'assets': 0},
        'period_close': {},
        'reversed_entry_ids': reversed_ids,
        'entry_ids': [],
    }

    # --- Khấu hao TSCĐ ---
    existing_dep = _clear_kind('DEPRECIATION', DOC_DEP, FIXED_ASSETS_TABLE)
    if existing_dep and not replace_existing:
        result['depreciation'] = {
            'entry_id': existing_dep,
            'amount': 0.0,
            'assets': 0,
            'reason': 'already_posted',
        }
    else:
        fa_items = _collect_fa_amounts(conn, fiscal_year, period)
        total_fa = sum((x['amount'] for x in fa_items), Decimal('0.00'))
        if total_fa > 0:
            lines = _build_simple_lines(
                conn,
                business_type='KHAU_HAO_TSCD',
                amount=total_fa,
                description=f'Khấu hao TSCĐ tháng {period:02d}/{fiscal_year}',
            )
            entry = post_journal_entry(
                conn,
                posting_date=posting_date,
                document_date=posting_date,
                document_type=DOC_DEP,
                document_no=f'KH{fiscal_year}{period:02d}',
                document_id=doc_id,
                business_type='KHAU_HAO_TSCD',
                description=f'Khấu hao TSCĐ tự động {period:02d}/{fiscal_year} ({len(fa_items)} tài sản)',
                created_by=created_by,
                branch_code='HQ',
                lines=lines,
            )
            for item in fa_items:
                conn.execute(
                    """
                    INSERT INTO sme_auto_asset_postings
                    (kind, asset_table, asset_id, fiscal_year, period, amount, journal_entry_id)
                    VALUES ('DEPRECIATION', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        FIXED_ASSETS_TABLE, item['asset_id'], fiscal_year, period,
                        float(item['amount']), entry['id'],
                    ),
                )
            result['depreciation'] = {
                'entry_id': entry['id'],
                'amount': float(total_fa),
                'assets': len(fa_items),
                'details': [
                    {'id': i['asset_id'], 'code': i['code'], 'name': i['name'], 'amount': float(i['amount'])}
                    for i in fa_items
                ],
            }
            result['entry_ids'].append(entry['id'])
            result['posted'] = True

    # --- Phân bổ CCDC ---
    existing_tool = _clear_kind('TOOLS_ALLOC', DOC_TOOL, TOOLS_TABLE)
    if existing_tool and not replace_existing:
        result['tools'] = {
            'entry_id': existing_tool,
            'amount': 0.0,
            'assets': 0,
            'reason': 'already_posted',
        }
    else:
        tool_items = _collect_tool_amounts(conn, fiscal_year, period)
        total_tools = sum((x['amount'] for x in tool_items), Decimal('0.00'))
        if total_tools > 0:
            lines = _build_simple_lines(
                conn,
                business_type='PHAN_BO_CCDC',
                amount=total_tools,
                description=f'Phân bổ CCDC tháng {period:02d}/{fiscal_year}',
            )
            entry = post_journal_entry(
                conn,
                posting_date=posting_date,
                document_date=posting_date,
                document_type=DOC_TOOL,
                document_no=f'PB{fiscal_year}{period:02d}',
                document_id=doc_id,
                business_type='PHAN_BO_CCDC',
                description=f'Phân bổ CCDC tự động {period:02d}/{fiscal_year} ({len(tool_items)} món)',
                created_by=created_by,
                branch_code='HQ',
                lines=lines,
            )
            for item in tool_items:
                conn.execute(
                    """
                    INSERT INTO sme_auto_asset_postings
                    (kind, asset_table, asset_id, fiscal_year, period, amount, journal_entry_id)
                    VALUES ('TOOLS_ALLOC', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        TOOLS_TABLE, item['asset_id'], fiscal_year, period,
                        float(item['amount']), entry['id'],
                    ),
                )
            result['tools'] = {
                'entry_id': entry['id'],
                'amount': float(total_tools),
                'assets': len(tool_items),
                'details': [
                    {'id': i['asset_id'], 'code': i['code'], 'name': i['name'], 'amount': float(i['amount'])}
                    for i in tool_items
                ],
            }
            result['entry_ids'].append(entry['id'])
            result['posted'] = True

    # --- Kết chuyển KQKD (sau KH/PB để gồm chi phí kỳ) ---
    from Services.sme.period_close import run_period_close

    close = run_period_close(
        conn,
        fiscal_year=fiscal_year,
        period=period,
        accounting_regime=accounting_regime,
        features=features,
        created_by=created_by,
        replace_existing=replace_existing,
    )
    result['period_close'] = close
    if close.get('entry_ids'):
        result['entry_ids'].extend(close['entry_ids'])
    if close.get('reversed_entry_ids'):
        result['reversed_entry_ids'].extend(close['reversed_entry_ids'])
    if close.get('posted'):
        result['posted'] = True

    # --- Quyết toán GTGT ---
    from Services.sme.vat_settlement import run_vat_settlement

    vat = run_vat_settlement(
        conn,
        fiscal_year=fiscal_year,
        period=period,
        accounting_regime=accounting_regime,
        features=features,
        created_by=created_by,
        replace_existing=replace_existing,
    )
    result['vat_settlement'] = vat
    if vat.get('entry_ids'):
        result['entry_ids'].extend(vat['entry_ids'])
    if vat.get('reversed_entry_ids'):
        result['reversed_entry_ids'].extend(vat['reversed_entry_ids'])
    if vat.get('posted'):
        result['posted'] = True

    # --- Cuối năm (T12): 4212 → 4211 trước khi khóa sổ ---
    result['year_end'] = {}
    if int(period) == 12:
        from Services.sme.period_close import run_year_end_close
        ye = run_year_end_close(
            conn,
            fiscal_year=fiscal_year,
            created_by=created_by,
            replace_existing=replace_existing,
            lock_after=False,
        )
        result['year_end'] = ye
        if ye.get('entry_ids'):
            result['entry_ids'].extend(ye['entry_ids'])
        if ye.get('reversed_entry_ids'):
            result['reversed_entry_ids'].extend(ye['reversed_entry_ids'])
        if ye.get('posted'):
            result['posted'] = True

    # --- Khóa sổ kỳ ---
    lock_info = None
    do_lock = True
    if features is not None and features.get('auto_lock_period') is False:
        do_lock = False
    if do_lock:
        reason = 'Chốt kỳ tự động (KH/PB/KQKD/GTGT'
        if int(period) == 12:
            reason += '/KCN'
        reason += ')'
        lock_info = lock_period(
            conn,
            fiscal_year=fiscal_year,
            period=period,
            locked_by=created_by,
            reason=reason,
        )
    result['period_lock'] = lock_info

    if (
        not result['posted']
        and not result['depreciation'].get('reason')
        and not result['tools'].get('reason')
        and not (result.get('period_close') or {}).get('reason')
        and not (result.get('vat_settlement') or {}).get('reason')
        and not (result.get('year_end') or {}).get('reason')
    ):
        result['reason'] = 'nothing_to_post'
    return result


def run_sme_automation_for_all_tenants(
    *,
    fiscal_year: int | None = None,
    period: int | None = None,
) -> dict[str, Any]:
    """Job lịch: chạy kỳ trước cho mọi tenant SME đang active."""
    from db_utils import get_main_db_connection, get_tenant_db_connection
    from Services.subscription_service import parse_tenant_settings
    from Services.tenant_profile import normalize_accounting_regime, resolve_features

    today = datetime.now()
    first = datetime(today.year, today.month, 1)
    prev = first - timedelta(days=1)
    fiscal_year = fiscal_year or prev.year
    period = period or prev.month

    main = get_main_db_connection()
    try:
        tenants = main.execute(
            "SELECT tenant_id, settings FROM tenants WHERE is_active = 1"
        ).fetchall()
    finally:
        main.close()

    results = []
    for row in tenants:
        tid = row['tenant_id']
        settings = parse_tenant_settings(row['settings'])
        if not isinstance(settings, dict):
            settings = {}
        regime = normalize_accounting_regime(settings.get('accounting_regime'))
        if not str(regime).startswith('SME'):
            continue
        features = resolve_features(regime, settings.get('revenue_tier') or 'DT1', settings)
        if not features.get('journal_posting') or features.get('auto_depreciation') is False:
            continue
        conn = get_tenant_db_connection(tid)
        if not conn:
            continue
        try:
            out = run_period_automation(
                conn,
                fiscal_year=fiscal_year,
                period=period,
                accounting_regime=regime,
                features=features,
                created_by='scheduler',
                replace_existing=False,
                auto_activate=True,
            )
            conn.commit()
            results.append({'tenant_id': tid, **{k: out[k] for k in out if k != 'depreciation' and k != 'tools'},
                            'depreciation_amount': (out.get('depreciation') or {}).get('amount'),
                            'tools_amount': (out.get('tools') or {}).get('amount'),
                            'entry_ids': out.get('entry_ids')})
        except Exception as exc:
            conn.rollback()
            results.append({'tenant_id': tid, 'posted': False, 'error': str(exc)})
        finally:
            conn.close()

    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'tenants': len(results),
        'results': results,
    }
