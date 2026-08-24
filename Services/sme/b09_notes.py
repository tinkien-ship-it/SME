"""Bản thuyết minh BCTC (Mẫu B09-DN) — khung TT99 + số liệu bổ sung từ sổ nhật ký."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _closing_balances, _period_activity
from Services.sme.general_ledger import period_bounds
from Services.sme.journal_engine import ensure_sme_journal_ready
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val: Decimal | float | int | None) -> float:
    return float(_money(val))


def _load_entity(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(business_info)').fetchall()}
    except sqlite3.Error:
        return {}
    if not cols:
        return {}
    row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    if not row:
        return {}
    if not isinstance(row, sqlite3.Row):
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    data = dict(row) if row else {}
    return {
        'business_name': data.get('business_name') or data.get('name') or '',
        'address': data.get('address') or '',
        'tax_code': data.get('tax_code') or '',
        'phone': data.get('phone') or '',
        'email': data.get('email') or '',
        'representative_name': data.get('representative_name') or '',
        'accounting_regime': data.get('accounting_regime') or 'SME_TT99',
    }


def _coa_meta(conn: sqlite3.Connection) -> dict[str, dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT code, name, parent_code, normal_balance, account_class, is_postable
        FROM sme_chart_of_accounts WHERE is_active = 1
        """
    ).fetchall()
    return {r['code']: dict(r) for r in rows}


def _net_balance(
    bal: dict[str, Decimal] | None,
    *,
    normal: str,
) -> Decimal:
    if not bal:
        return Decimal('0.00')
    debit = _money(bal.get('debit'))
    credit = _money(bal.get('credit'))
    if (normal or 'debit') == 'credit':
        return credit - debit
    return debit - credit


def _sum_prefix(
    bal_map: dict[str, dict[str, Decimal]],
    coa: dict[str, dict],
    prefixes: tuple[str, ...],
    *,
    postable_only: bool = True,
) -> Decimal:
    total = Decimal('0.00')
    for code, meta in coa.items():
        if postable_only and not meta.get('is_postable'):
            continue
        if not any(code == p or code.startswith(p) for p in prefixes):
            continue
        # tránh cộng cả cha lẫn con: chỉ lá postable
        total += _net_balance(bal_map.get(code), normal=meta.get('normal_balance') or 'debit')
    return _money(total)


def _lines_for_prefixes(
    bal_open: dict[str, dict[str, Decimal]],
    bal_close: dict[str, dict[str, Decimal]],
    coa: dict[str, dict],
    prefixes: tuple[str, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in sorted(coa.keys()):
        meta = coa[code]
        if not meta.get('is_postable'):
            continue
        if not any(code == p or code.startswith(p) for p in prefixes):
            continue
        opening = _net_balance(bal_open.get(code), normal=meta.get('normal_balance') or 'debit')
        closing = _net_balance(bal_close.get(code), normal=meta.get('normal_balance') or 'debit')
        if opening == 0 and closing == 0:
            continue
        rows.append({
            'account_code': code,
            'name': meta.get('name') or code,
            'opening': _f(opening),
            'closing': _f(closing),
        })
    return rows


def _activity_lines(
    activity: dict[str, dict[str, Decimal]],
    coa: dict[str, dict],
    prefixes: tuple[str, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in sorted(coa.keys()):
        meta = coa[code]
        if not meta.get('is_postable'):
            continue
        if not any(code == p or code.startswith(p) for p in prefixes):
            continue
        bal = activity.get(code)
        if not bal:
            continue
        cls = (meta.get('account_class') or '').strip()
        # Seed TT99: một số TK con chưa kế thừa class — suy từ mã TK
        if cls == 'revenue' or code.startswith(('511', '512', '515', '711')):
            amount = _money(bal.get('credit')) - _money(bal.get('debit'))
        elif cls == 'expense' or code.startswith(('632', '635', '641', '642', '811', '821')):
            amount = _money(bal.get('debit')) - _money(bal.get('credit'))
        else:
            amount = _net_balance(bal, normal=meta.get('normal_balance') or 'debit')
        if amount == 0:
            continue
        rows.append({
            'account_code': code,
            'name': meta.get('name') or code,
            'amount': _f(amount),
        })
    return rows


DEFAULT_POLICIES = {
    'cash': (
        'Tiền và tương đương tiền gồm tiền mặt, tiền gửi không kỳ hạn và các khoản đầu tư '
        'ngắn hạn có thời hạn thu hồi không quá 3 tháng, dễ chuyển đổi thành tiền và ít rủi ro.'
    ),
    'receivables': (
        'Các khoản phải thu được ghi nhận theo giá gốc trừ dự phòng phải thu khó đòi '
        '(nếu có). Dự phòng được trích theo ước tính tổn thất dựa trên khả năng thu hồi.'
    ),
    'inventory': (
        'Hàng tồn kho ghi nhận theo giá gốc. Giá xuất kho tính theo phương pháp bình quân '
        'gia quyền liên hoàn (WAC). Cuối kỳ đánh giá theo giá gốc và giá trị thuần có thể thực hiện được, '
        'lấy giá trị thấp hơn.'
    ),
    'fixed_assets': (
        'TSCĐ hữu hình ghi nhận theo nguyên giá và khấu hao theo đường thẳng trong suốt '
        'thời gian sử dụng hữu ích ước tính. Chi phí sửa chữa lớn được vốn hóa khi đủ điều kiện.'
    ),
    'tools': (
        'Công cụ, dụng cụ ghi nhận trên TK 153 và phân bổ vào chi phí theo thời gian sử dụng '
        'hoặc xuất dùng một lần khi giá trị nhỏ.'
    ),
    'payables': (
        'Phải trả người bán và phải trả khác ghi nhận theo giá gốc của nghĩa vụ phải thanh toán.'
    ),
    'revenue': (
        'Doanh thu bán hàng và cung cấp dịch vụ được ghi nhận khi đã chuyển giao phần lớn '
        'rủi ro và lợi ích, doanh thu được xác định tương đối chắc chắn và có khả năng thu được lợi ích kinh tế.'
    ),
    'cogs': (
        'Giá vốn hàng bán được ghi nhận đồng thời với doanh thu tương ứng, theo giá xuất kho WAC.'
    ),
    'expenses': (
        'Chi phí bán hàng và chi phí quản lý doanh nghiệp được ghi nhận trong kỳ phát sinh, '
        'phù hợp với doanh thu.'
    ),
}


def ensure_b09_narrative_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_b09_narratives (
            item_code TEXT PRIMARY KEY,
            section TEXT NOT NULL,
            label TEXT,
            value TEXT NOT NULL,
            updated_at TEXT,
            updated_by TEXT
        )
        """
    )
    if commit:
        sqlite_commit(conn, label='b09_notes')


def list_b09_narrative_overrides(conn: sqlite3.Connection) -> dict[str, str]:
    ensure_b09_narrative_schema(conn, commit=False)
    rows = conn.execute(
        "SELECT item_code, value FROM sme_b09_narratives"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def save_b09_narrative_items(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
    *,
    updated_by: str | None = None,
) -> int:
    """Lưu/ghi đè các mục thuyết minh I–IV. items: [{code, section?, label?, value}]"""
    from datetime import datetime
    ensure_b09_narrative_schema(conn, commit=False)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    n = 0
    for raw in items or []:
        code = str(raw.get('code') or raw.get('item_code') or '').strip()
        value = str(raw.get('value') or '').strip()
        if not code or not value:
            continue
        section = str(raw.get('section') or (code.split('.')[0] if '.' in code else code[:1])).strip()
        label = str(raw.get('label') or '').strip() or None
        conn.execute(
            """
            INSERT INTO sme_b09_narratives (item_code, section, label, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_code) DO UPDATE SET
                section = excluded.section,
                label = COALESCE(excluded.label, sme_b09_narratives.label),
                value = excluded.value,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (code, section, label, value, now, updated_by),
        )
        n += 1
    return n


def _narrative_sections(
    entity: dict[str, Any],
    fiscal_year: int,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    name = entity.get('business_name') or 'Doanh nghiệp'
    regime = str(entity.get('accounting_regime') or 'SME_TT99').upper()
    regime_label = (
        'Thông tư 58/2026/TT-BTC (doanh nghiệp siêu nhỏ)'
        if 'TT58' in regime
        else 'Thông tư 99/2025/TT-BTC (doanh nghiệp)'
    )
    overrides = overrides or {}

    def _v(code: str, default: str) -> str:
        return overrides.get(code) or default

    return {
        'I': {
            'title': 'Đặc điểm hoạt động của doanh nghiệp',
            'items': [
                {'code': 'I.1', 'label': 'Hình thức sở hữu vốn', 'value': _v('I.1', 'Doanh nghiệp tư nhân / TNHH / cổ phần (cập nhật theo giấy phép).')},
                {'code': 'I.2', 'label': 'Lĩnh vực kinh doanh', 'value': _v('I.2', 'Thương mại – dịch vụ / sản xuất (theo ngành đăng ký).')},
                {'code': 'I.3', 'label': 'Ngành nghề kinh doanh', 'value': _v('I.3', name)},
                {'code': 'I.4', 'label': 'Chu kỳ sản xuất, kinh doanh thông thường', 'value': _v('I.4', '12 tháng')},
                {
                    'code': 'I.5',
                    'label': 'Đặc điểm hoạt động trong năm có ảnh hưởng đến BCTC',
                    'value': _v('I.5', 'Không có sự kiện trọng yếu ngoài hoạt động kinh doanh thông thường (cập nhật nếu có).'),
                },
                {'code': 'I.6', 'label': 'Cấu trúc doanh nghiệp', 'value': _v('I.6', 'Không có công ty con / liên kết trong phạm vi báo cáo này.')},
                {'code': 'I.7', 'label': 'Số lượng người lao động', 'value': _v('I.7', '— (cập nhật khi có dữ liệu HR).')},
                {
                    'code': 'I.8',
                    'label': 'Khả năng so sánh thông tin',
                    'value': _v('I.8', 'Số liệu được trình bày nhất quán theo cùng chế độ kế toán trong kỳ.'),
                },
            ],
        },
        'II': {
            'title': 'Kỳ kế toán, đơn vị tiền tệ sử dụng trong kế toán',
            'items': [
                {
                    'code': 'II.1',
                    'label': 'Kỳ kế toán năm',
                    'value': _v('II.1', f'Bắt đầu 01/01/{fiscal_year} — kết thúc 31/12/{fiscal_year}'),
                },
                {'code': 'II.2', 'label': 'Đơn vị tiền tệ', 'value': _v('II.2', 'Đồng Việt Nam (VND)')},
            ],
        },
        'III': {
            'title': 'Chuẩn mực và Chế độ kế toán áp dụng',
            'items': [
                {'code': 'III.1', 'label': 'Chế độ kế toán áp dụng', 'value': _v('III.1', regime_label)},
                {
                    'code': 'III.2',
                    'label': 'Tuyên bố tuân thủ',
                    'value': _v(
                        'III.2',
                        (
                            'Báo cáo tài chính được lập và trình bày phù hợp với Chuẩn mực kế toán Việt Nam '
                            f'và {regime_label} trong các khía cạnh trọng yếu.'
                        ),
                    ),
                },
            ],
        },
        'IV': {
            'title': 'Các chính sách kế toán áp dụng',
            'items': [
                {'code': 'IV.4', 'label': 'Tiền và tương đương tiền', 'value': _v('IV.4', DEFAULT_POLICIES['cash'])},
                {'code': 'IV.6', 'label': 'Nợ phải thu', 'value': _v('IV.6', DEFAULT_POLICIES['receivables'])},
                {'code': 'IV.7', 'label': 'Hàng tồn kho', 'value': _v('IV.7', DEFAULT_POLICIES['inventory'])},
                {'code': 'IV.8', 'label': 'TSCĐ và khấu hao', 'value': _v('IV.8', DEFAULT_POLICIES['fixed_assets'])},
                {'code': 'IV.11', 'label': 'Công cụ dụng cụ / chi phí chờ phân bổ', 'value': _v('IV.11', DEFAULT_POLICIES['tools'])},
                {'code': 'IV.12', 'label': 'Phải trả người bán', 'value': _v('IV.12', DEFAULT_POLICIES['payables'])},
                {'code': 'IV.22', 'label': 'Doanh thu', 'value': _v('IV.22', DEFAULT_POLICIES['revenue'])},
                {'code': 'IV.24', 'label': 'Giá vốn hàng bán', 'value': _v('IV.24', DEFAULT_POLICIES['cogs'])},
                {'code': 'IV.26', 'label': 'Chi phí bán hàng / quản lý', 'value': _v('IV.26', DEFAULT_POLICIES['expenses'])},
            ],
        },
    }


def notes_to_financial_statements(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int = 12,
) -> dict[str, Any]:
    """
    Lập khung B09-DN:
    - Phần I–IV: thuyết minh định tính (mặc định SME, có thể chỉnh sau).
    - Phần V–VI: bảng số liệu bổ sung từ sổ cái / phát sinh kỳ.
    """
    ensure_sme_journal_ready(conn, commit=False)
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    entity = _load_entity(conn)
    overrides = list_b09_narrative_overrides(conn)
    coa = _coa_meta(conn)
    bal_close = _closing_balances(conn, fiscal_year, period_to)
    # Đầu năm = số dư cuối năm trước
    bal_open = _closing_balances(conn, fiscal_year - 1, 12) if fiscal_year > 1900 else {}
    activity = _period_activity(conn, fiscal_year, 1, period_to)
    _, as_of = period_bounds(fiscal_year, period_to)

    cash_rows = _lines_for_prefixes(bal_open, bal_close, coa, ('111', '112', '113'))
    inv_rows = _lines_for_prefixes(bal_open, bal_close, coa, ('151', '152', '153', '154', '155', '156', '157', '158'))
    ar_rows = _lines_for_prefixes(bal_open, bal_close, coa, ('131',))
    ap_rows = _lines_for_prefixes(bal_open, bal_close, coa, ('331',))
    fa_rows = _lines_for_prefixes(bal_open, bal_close, coa, ('211', '213', '214'))
    vat_in_rows = _lines_for_prefixes(bal_open, bal_close, coa, ('133',))
    vat_out_rows = _lines_for_prefixes(bal_open, bal_close, coa, ('3331',))

    revenue_rows = _activity_lines(activity, coa, ('511', '515', '711'))
    cogs_rows = _activity_lines(activity, coa, ('632',))
    expense_rows = _activity_lines(activity, coa, ('641', '642', '635', '811'))

    def _total_oc(rows: list[dict]) -> dict[str, float]:
        return {
            'opening': _f(sum((_money(r['opening']) for r in rows), Decimal('0'))),
            'closing': _f(sum((_money(r['closing']) for r in rows), Decimal('0'))),
        }

    def _total_amt(rows: list[dict]) -> float:
        return _f(sum((_money(r['amount']) for r in rows), Decimal('0')))

    return {
        'report': 'B09-DN',
        'title': 'Bản thuyết minh Báo cáo tài chính',
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'as_of_date': as_of,
        'entity': entity,
        'narrative': _narrative_sections(entity, fiscal_year, overrides),
        'narrative_editable': True,
        'narrative_overrides': list(overrides.keys()),
        'supplementary': {
            'V': {
                'title': 'Thông tin bổ sung cho các khoản mục trên Báo cáo tình hình tài chính',
                'notes': [
                    {
                        'code': 'V.1',
                        'title': 'Tiền và các khoản tương đương tiền',
                        'columns': ['opening', 'closing'],
                        'rows': cash_rows,
                        'totals': _total_oc(cash_rows),
                        'hint': 'Chi tiết theo TK 111 / 112 / 113. Tương đương tiền (< 3 tháng) bổ sung khi phát sinh TK 128 ngắn hạn.',
                    },
                    {
                        'code': 'V.2',
                        'title': 'Phải thu khách hàng',
                        'columns': ['opening', 'closing'],
                        'rows': ar_rows,
                        'totals': _total_oc(ar_rows),
                    },
                    {
                        'code': 'V.3',
                        'title': 'Hàng tồn kho',
                        'columns': ['opening', 'closing'],
                        'rows': inv_rows,
                        'totals': _total_oc(inv_rows),
                    },
                    {
                        'code': 'V.4',
                        'title': 'TSCĐ và hao mòn',
                        'columns': ['opening', 'closing'],
                        'rows': fa_rows,
                        'totals': _total_oc(fa_rows),
                    },
                    {
                        'code': 'V.5',
                        'title': 'Thuế GTGT được khấu trừ',
                        'columns': ['opening', 'closing'],
                        'rows': vat_in_rows,
                        'totals': _total_oc(vat_in_rows),
                    },
                    {
                        'code': 'V.6',
                        'title': 'Phải trả người bán',
                        'columns': ['opening', 'closing'],
                        'rows': ap_rows,
                        'totals': _total_oc(ap_rows),
                    },
                    {
                        'code': 'V.7',
                        'title': 'Thuế và các khoản phải nộp NSNN (GTGT đầu ra)',
                        'columns': ['opening', 'closing'],
                        'rows': vat_out_rows,
                        'totals': _total_oc(vat_out_rows),
                    },
                ],
            },
            'VI': {
                'title': 'Thông tin bổ sung cho các khoản mục trên Báo cáo kết quả hoạt động kinh doanh',
                'notes': [
                    {
                        'code': 'VI.1',
                        'title': 'Doanh thu bán hàng và cung cấp dịch vụ / thu nhập',
                        'columns': ['amount'],
                        'rows': revenue_rows,
                        'totals': {'amount': _total_amt(revenue_rows)},
                    },
                    {
                        'code': 'VI.2',
                        'title': 'Giá vốn hàng bán',
                        'columns': ['amount'],
                        'rows': cogs_rows,
                        'totals': {'amount': _total_amt(cogs_rows)},
                    },
                    {
                        'code': 'VI.3',
                        'title': 'Chi phí bán hàng, quản lý và khác',
                        'columns': ['amount'],
                        'rows': expense_rows,
                        'totals': {'amount': _total_amt(expense_rows)},
                    },
                ],
            },
        },
        'summary': {
            'cash_closing': _f(_sum_prefix(bal_close, coa, ('111', '112', '113'))),
            'inventory_closing': _f(_sum_prefix(bal_close, coa, ('151', '152', '153', '154', '155', '156', '157', '158'))),
            'ar_closing': _f(_sum_prefix(bal_close, coa, ('131',))),
            'ap_closing': _f(_sum_prefix(bal_close, coa, ('331',))),
        },
    }
