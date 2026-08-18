"""Vòng đời TSCĐ SME — thanh lý/nhượng bán (02-TSCĐ) + bảng KH 06-TSCĐ."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.fixed_assets_helpers import (
    FIXED_ASSETS_TABLE,
    STATUS_ACTIVE,
    STATUS_DISPOSED,
    STATUS_IN_STOCK,
    ensure_fixed_assets_schema,
)
from Services.profit_report_helpers import depreciation_for_month
from Services.sme.auto_posting import (
    _depreciable_cost,
    _parse_date,
    _posted_to_date,
    ensure_auto_posting_schema,
)
from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry

MONEY_Q = Decimal('0.01')
FORM_DISPOSAL = '02-TSCD'
FORM_DEP_TABLE = '06-TSCD'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_fa_lifecycle_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    ensure_fixed_assets_schema(conn)
    ensure_auto_posting_schema(conn, commit=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_fa_disposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '02-TSCD',
            doc_no TEXT NOT NULL UNIQUE,
            disposal_date TEXT NOT NULL,
            asset_id INTEGER NOT NULL,
            asset_code TEXT,
            asset_name TEXT,
            disposal_type TEXT NOT NULL DEFAULT 'scrap',
            original_cost REAL NOT NULL DEFAULT 0,
            accum_dep REAL NOT NULL DEFAULT 0,
            net_book REAL NOT NULL DEFAULT 0,
            proceeds REAL NOT NULL DEFAULT 0,
            gain_loss REAL NOT NULL DEFAULT 0,
            payment_method TEXT DEFAULT 'cash',
            counterparty TEXT,
            reason TEXT,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    if commit:
        conn.commit()


def _next_disposal_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_fa_disposals WHERE doc_no LIKE 'TL%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 'TL000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'TL{int(digits) + 1:06d}'


def _cash_account(payment_method: str) -> str:
    method = (payment_method or 'cash').strip().lower()
    if method in ('112', 'bank', 'bank_transfer', 'ck', 'transfer'):
        return '1121'
    if method in ('131', 'credit', 'receivable'):
        return '131'
    return '1111'


def list_active_assets(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    ensure_fixed_assets_schema(conn)
    from Services.sme.branches import DEFAULT_BRANCH_CODE

    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({FIXED_ASSETS_TABLE})').fetchall()}
    has_br = 'branch_code' in cols
    extra = ', warehouse_code' if 'warehouse_code' in cols else ''
    if has_br:
        extra += ', branch_code'
    if 'expense_account' in cols:
        extra += ', expense_account'
    sql = f"""
        SELECT id, ma_tai_san, ten_tai_san, nguyen_gia_tinh_khau_hao, thue_gtgt,
               gia_mua_chua_thue, so_luong, so_thang_khau_hao, ngay_bat_dau_su_dung, tinh_trang
               {extra}
        FROM {FIXED_ASSETS_TABLE}
        WHERE 1=1
    """
    params: list[Any] = []
    st = (status or '').strip()
    if st:
        sql += ' AND tinh_trang = ?'
        params.append(st)
    else:
        sql += ' AND tinh_trang IN (?, ?)'
        params.extend([STATUS_ACTIVE, STATUS_IN_STOCK])
    code = (branch_code or '').strip().upper()
    if has_br and code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
        else:
            sql += ' AND branch_code = ?'
        params.append(code)
    sql += ' ORDER BY id DESC, ma_tai_san'
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        cost = _depreciable_cost(r)
        d['original_cost'] = _f(cost)
        if has_br and not d.get('branch_code'):
            d['branch_code'] = DEFAULT_BRANCH_CODE
        out.append(d)
    return out


def update_asset_depreciation_period(
    conn: sqlite3.Connection,
    asset_id: int,
    *,
    so_thang_khau_hao: int,
    start_date: str | None = None,
    expense_account: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Thiết lập số tháng khấu hao TSCĐ (và tùy chọn ngày bắt đầu sử dụng / TK CP)."""
    ensure_fixed_assets_schema(conn)
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, FIXED_ASSETS_TABLE, asset_id, label='TSCĐ')
    row = conn.execute(
        f'SELECT * FROM {FIXED_ASSETS_TABLE} WHERE id = ?', (asset_id,)
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy TSCĐ')
    d = dict(row)
    if str(d.get('tinh_trang') or '') == STATUS_DISPOSED:
        raise ValueError('TSCĐ đã thanh lý — không đổi thời hạn khấu hao')
    months = int(so_thang_khau_hao or 0)
    if months <= 0:
        raise ValueError('Số tháng khấu hao phải > 0')
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({FIXED_ASSETS_TABLE})').fetchall()}
    sets = ['so_thang_khau_hao = ?']
    params: list[Any] = [months]
    if start_date and 'ngay_bat_dau_su_dung' in cols:
        sets.append('ngay_bat_dau_su_dung = ?')
        params.append(str(start_date)[:10])
    exp = (expense_account or '').strip()
    if exp and 'expense_account' in cols:
        sets.append('expense_account = ?')
        params.append(exp)
    if 'updated_at' in cols:
        sets.append('updated_at = ?')
        params.append(_now())
    params.append(asset_id)
    conn.execute(
        f"UPDATE {FIXED_ASSETS_TABLE} SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    if commit:
        conn.commit()
    row2 = conn.execute(
        f'SELECT * FROM {FIXED_ASSETS_TABLE} WHERE id = ?', (asset_id,)
    ).fetchone()
    return dict(row2) if row2 else d


def asset_book_values(
    conn: sqlite3.Connection,
    asset_id: int,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    ensure_sme_fa_lifecycle_schema(conn, commit=False)
    row = conn.execute(
        f'SELECT * FROM {FIXED_ASSETS_TABLE} WHERE id = ?', (asset_id,)
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy TSCĐ')
    cost = _money(_depreciable_cost(row))
    date_s = (as_of or datetime.now().strftime('%Y-%m-%d'))[:10]
    year, month = int(date_s[:4]), int(date_s[5:7])
    # Hao mòn đã ghi đến hết kỳ hiện tại (bao gồm kỳ as_of)
    accum = _money(_posted_to_date(
        conn, kind='DEPRECIATION', asset_table=FIXED_ASSETS_TABLE,
        asset_id=int(asset_id), before_year=year, before_period=month + 1,
    ))
    # _posted_to_date with before_period=month+1 includes month when month<12;
    # for December use year+1 period 1 — handle:
    if month == 12:
        accum = _money(_posted_to_date(
            conn, kind='DEPRECIATION', asset_table=FIXED_ASSETS_TABLE,
            asset_id=int(asset_id), before_year=year + 1, before_period=1,
        ))
    if accum > cost:
        accum = cost
    net = cost - accum
    d = dict(row)
    return {
        'asset': d,
        'original_cost': _f(cost),
        'accum_dep': _f(accum),
        'net_book': _f(net),
    }


def dispose_fixed_asset(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    disposal_date: str,
    disposal_type: str = 'scrap',
    proceeds=0,
    payment_method: str = 'cash',
    counterparty: str = '',
    reason: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Biên bản thanh lý/nhượng bán TSCĐ + bút toán xóa sổ 211/214 + lãi/lỗ."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_fa_lifecycle_schema(conn, commit=False)

    date_s = str(disposal_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày thanh lý')
    vals = asset_book_values(conn, asset_id, as_of=date_s)
    asset = vals['asset']
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, FIXED_ASSETS_TABLE, asset_id, label='TSCĐ')
    status = asset.get('tinh_trang')
    if status == STATUS_DISPOSED:
        raise ValueError('TSCĐ đã thanh lý trước đó')

    cost = _money(vals['original_cost'])
    accum = _money(vals['accum_dep'])
    net = _money(vals['net_book'])
    proc = _money(proceeds)
    if proc < 0:
        raise ValueError('Giá thanh lý không được âm')

    # Lãi/lỗ = thu - GTCL
    gain_loss = proc - net
    dtype = (disposal_type or 'scrap').strip().lower()
    if dtype not in ('scrap', 'sale'):
        dtype = 'scrap'
    if dtype == 'scrap':
        proc = Decimal('0.00')
        gain_loss = -net

    doc_no = _next_disposal_no(conn)
    desc = reason or f'Thanh lý TSCĐ {asset.get("ma_tai_san")}'
    cash_acc = _cash_account(payment_method)

    lines: list[dict] = []
    seq = 1
    # Nợ 214 hao mòn
    if accum > 0:
        lines.append({
            'sequence': seq, 'account_code': '2141',
            'debit': float(accum), 'credit': 0, 'description': desc,
        })
        seq += 1
    # Nợ 811 (lỗ) hoặc sẽ có Có 711 (lãi) — ghi phần GTCL còn lại / điều chỉnh
    if gain_loss < 0:
        loss = abs(gain_loss)
        if loss > 0:
            lines.append({
                'sequence': seq, 'account_code': '811',
                'debit': float(loss), 'credit': 0, 'description': desc,
            })
            seq += 1
    # Nợ tiền/phải thu nếu có thu
    if proc > 0:
        lines.append({
            'sequence': seq, 'account_code': cash_acc,
            'debit': float(proc), 'credit': 0, 'description': desc,
        })
        seq += 1
    # Có 211 nguyên giá
    if cost > 0:
        lines.append({
            'sequence': seq, 'account_code': '2111',
            'debit': 0, 'credit': float(cost), 'description': desc,
        })
        seq += 1
    # Có 711 lãi
    if gain_loss > 0:
        lines.append({
            'sequence': seq, 'account_code': '711',
            'debit': 0, 'credit': float(gain_loss), 'description': desc,
        })
        seq += 1

    if not lines:
        raise ValueError('Không có số liệu để ghi sổ thanh lý')

    asset_branch = (asset.get('branch_code') or '').strip().upper() or None

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='TLTS',
        document_no=doc_no,
        document_id=int(asset_id),
        business_type='THANH_LY_TSCD',
        description=desc,
        created_by=created_by,
        branch_code=asset_branch,
        lines=lines,
    )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_fa_disposals (
            form_code, doc_no, disposal_date, asset_id, asset_code, asset_name,
            disposal_type, original_cost, accum_dep, net_book, proceeds, gain_loss,
            payment_method, counterparty, reason, journal_entry_id, status,
            created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'posted',?,?,?)
        """,
        (
            FORM_DISPOSAL, doc_no, date_s, int(asset_id),
            asset.get('ma_tai_san'), asset.get('ten_tai_san'),
            dtype, float(cost), float(accum), float(net), float(proc), float(gain_loss),
            payment_method or 'cash', counterparty or '', desc, entry['id'],
            created_by, _now(), _now(),
        ),
    )
    disposal_id = cur.lastrowid
    conn.execute(
        f"UPDATE {FIXED_ASSETS_TABLE} SET tinh_trang = ? WHERE id = ?",
        (STATUS_DISPOSED, int(asset_id)),
    )
    if commit:
        conn.commit()
    return get_disposal(conn, disposal_id)


def get_disposal(conn: sqlite3.Connection, disposal_id: int) -> dict[str, Any] | None:
    ensure_sme_fa_lifecycle_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_fa_disposals WHERE id = ?', (disposal_id,)).fetchone()
    return dict(row) if row else None


def list_disposals(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_fa_lifecycle_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE

    sql = f"""
        SELECT d.* FROM sme_fa_disposals d
        LEFT JOIN {FIXED_ASSETS_TABLE} fa ON fa.id = d.asset_id
        WHERE d.status != 'void'
    """
    params: list[Any] = []
    if date_from:
        sql += ' AND date(d.disposal_date) >= date(?)'
        params.append(date_from[:10])
    if date_to:
        sql += ' AND date(d.disposal_date) <= date(?)'
        params.append(date_to[:10])
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += (
                " AND (fa.branch_code IS NULL OR fa.branch_code = '' OR fa.branch_code = ?"
                " OR d.asset_id IS NULL)"
            )
        else:
            sql += ' AND fa.branch_code = ?'
        params.append(code)
    sql += ' ORDER BY d.disposal_date DESC, d.id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_disposal(
    conn: sqlite3.Connection,
    disposal_id: int,
    *,
    reason: str = 'Hủy thanh lý TSCĐ',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_fa_disposals', disposal_id, label='Biên bản thanh lý TSCĐ')
    doc = get_disposal(conn, disposal_id)
    if not doc:
        raise ValueError('Không tìm thấy biên bản thanh lý')
    if doc['status'] == 'void':
        raise ValueError('Đã hủy')
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    conn.execute(
        f"UPDATE {FIXED_ASSETS_TABLE} SET tinh_trang = ? WHERE id = ?",
        (STATUS_ACTIVE, int(doc['asset_id'])),
    )
    conn.execute(
        "UPDATE sme_fa_disposals SET status = 'void', reason = ?, updated_at = ? WHERE id = ?",
        ((doc.get('reason') or '') + f' | {reason}', _now(), disposal_id),
    )
    if commit:
        conn.commit()
    return get_disposal(conn, disposal_id)


def depreciation_schedule(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Bảng tính và phân bổ khấu hao TSCĐ — mẫu 06-TSCĐ (theo kỳ hoặc cả năm)."""
    ensure_sme_fa_lifecycle_schema(conn, commit=False)
    periods = [int(period)] if period else list(range(1, 13))
    from Services.sme.branches import DEFAULT_BRANCH_CODE

    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({FIXED_ASSETS_TABLE})').fetchall()}
    has_br = 'branch_code' in cols
    br_sql = ''
    br_params: list[Any] = []
    code = (branch_code or '').strip().upper()
    if has_br and code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            br_sql = " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
            br_params.append(DEFAULT_BRANCH_CODE)
        else:
            br_sql = ' AND branch_code = ?'
            br_params.append(code)

    assets = conn.execute(
        f"""
        SELECT id, ma_tai_san, ten_tai_san, nguyen_gia_tinh_khau_hao, thue_gtgt,
               gia_mua_chua_thue, so_luong, so_thang_khau_hao, ngay_bat_dau_su_dung, tinh_trang
        FROM {FIXED_ASSETS_TABLE}
        WHERE tinh_trang IN (?, ?, ?)
        {br_sql}
        ORDER BY ma_tai_san
        """,
        (STATUS_ACTIVE, STATUS_IN_STOCK, STATUS_DISPOSED, *br_params),
    ).fetchall()

    lines = []
    total_month = {p: Decimal('0.00') for p in periods}
    grand = Decimal('0.00')
    for row in assets:
        cost = _money(_depreciable_cost(row))
        months = int(row['so_thang_khau_hao'] or 0)
        start = _parse_date(row['ngay_bat_dau_su_dung'])
        if cost <= 0 or months <= 0 or not start:
            continue
        monthly_amt = _money(cost / months) if months else Decimal('0.00')
        period_amounts = {}
        row_total = Decimal('0.00')
        for p in periods:
            amt = _money(depreciation_for_month(float(cost), months, start, fiscal_year, p))
            # Không vượt phần còn lại đến trước kỳ
            prior = _money(_posted_to_date(
                conn, kind='DEPRECIATION', asset_table=FIXED_ASSETS_TABLE,
                asset_id=int(row['id']), before_year=fiscal_year, before_period=p,
            ))
            # Ước tính theo lịch nếu chưa post
            theoretical_prior = Decimal('0.00')
            for mp in range(1, p):
                theoretical_prior += _money(
                    depreciation_for_month(float(cost), months, start, fiscal_year, mp)
                )
            # Dùng max prior đã post vs lý thuyết trong năm — đơn giản: min(amt, remain)
            remain = cost - prior
            if remain < 0:
                remain = Decimal('0.00')
            amt = min(amt, remain)
            # Nếu chưa Active có thể vẫn hiện theo lịch theoretically
            if row['tinh_trang'] == STATUS_IN_STOCK and amt == 0:
                amt = min(
                    _money(depreciation_for_month(float(cost), months, start, fiscal_year, p)),
                    cost - theoretical_prior,
                )
            period_amounts[p] = _f(amt)
            row_total += amt
            total_month[p] += amt
        grand += row_total
        lines.append({
            'asset_id': int(row['id']),
            'code': row['ma_tai_san'],
            'name': row['ten_tai_san'],
            'original_cost': _f(cost),
            'months': months,
            'start_date': str(row['ngay_bat_dau_su_dung'] or '')[:10],
            'monthly_rate': _f(monthly_amt),
            'status': row['tinh_trang'],
            'periods': period_amounts,
            'total': _f(row_total),
        })

    return {
        'form_code': FORM_DEP_TABLE,
        'fiscal_year': fiscal_year,
        'period': period,
        'periods': periods,
        'lines': lines,
        'totals_by_period': {p: _f(total_month[p]) for p in periods},
        'grand_total': _f(grand),
        'branch_code': (branch_code or 'ALL'),
        'period_label': (
            f'Tháng {period}/{fiscal_year}' if period
            else f'Năm {fiscal_year}'
        ),
    }


# ── Biên bản TSCĐ 01 / 03 / 04 / 05 ──────────────────────────────────

def _stamp_fa_doc_branch(conn, doc_id, asset):
    br = (asset or {}).get('branch_code') or None
    if not br:
        from Services.sme.branches import resolve_posting_branch
        br = resolve_posting_branch(conn, None)
    try:
        conn.execute('UPDATE sme_fa_docs SET branch_code = ? WHERE id = ?', (br, doc_id))
    except sqlite3.Error:
        pass


def ensure_sme_fa_docs_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    ensure_sme_fa_lifecycle_schema(conn, commit=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_fa_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT NOT NULL,
            form_code TEXT NOT NULL,
            doc_no TEXT NOT NULL UNIQUE,
            doc_date TEXT NOT NULL,
            asset_id INTEGER,
            asset_code TEXT,
            asset_name TEXT,
            from_dept TEXT,
            to_dept TEXT,
            partner_name TEXT,
            amount REAL DEFAULT 0,
            old_cost REAL DEFAULT 0,
            new_cost REAL DEFAULT 0,
            journal_entry_id INTEGER,
            content TEXT,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_fa_inventory_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            asset_id INTEGER,
            asset_code TEXT,
            asset_name TEXT,
            book_cost REAL DEFAULT 0,
            actual_ok INTEGER DEFAULT 1,
            note TEXT,
            FOREIGN KEY(doc_id) REFERENCES sme_fa_docs(id)
        )
        """
    )
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_fa_docs)').fetchall()}
    if 'branch_code' not in cols:
        try:
            conn.execute('ALTER TABLE sme_fa_docs ADD COLUMN branch_code TEXT')
        except sqlite3.OperationalError:
            pass
    if commit:
        conn.commit()


def _next_fa_doc_no(conn: sqlite3.Connection, prefix: str) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_fa_docs WHERE doc_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def create_fa_handover(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    doc_date: str,
    from_dept: str = '',
    to_dept: str = '',
    partner_name: str = '',
    content: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Biên bản giao nhận TSCĐ — 01-TSCĐ (không ghi GL; kích hoạt Active nếu InStock)."""
    ensure_sme_fa_docs_schema(conn, commit=False)
    vals = asset_book_values(conn, asset_id, as_of=doc_date)
    asset = vals['asset']
    date_s = str(doc_date or '')[:10]
    doc_no = _next_fa_doc_no(conn, 'BBGN')
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_fa_docs (
            doc_type, form_code, doc_no, doc_date, asset_id, asset_code, asset_name,
            from_dept, to_dept, partner_name, amount, content, status, created_by, created_at
        ) VALUES ('handover','01-TSCD',?,?,?,?,?,?,?,?,?,?,'posted',?,?)
        """,
        (
            doc_no, date_s, asset_id, asset.get('ma_tai_san'), asset.get('ten_tai_san'),
            from_dept or '', to_dept or '', partner_name or '',
            vals['original_cost'], content or 'Giao nhận TSCĐ đưa vào sử dụng',
            created_by, _now(),
        ),
    )
    if asset.get('tinh_trang') == STATUS_IN_STOCK:
        conn.execute(
            f"UPDATE {FIXED_ASSETS_TABLE} SET tinh_trang = ?, ngay_bat_dau_su_dung = COALESCE(ngay_bat_dau_su_dung, ?) WHERE id = ?",
            (STATUS_ACTIVE, date_s, asset_id),
        )
    if commit:
        conn.commit()
    _stamp_fa_doc_branch(conn, cur.lastrowid, asset)
    return get_fa_doc(conn, cur.lastrowid)


def create_fa_upgrade(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    doc_date: str,
    amount,
    content: str = '',
    cash_account: str = '1121',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Biên bản bàn giao SC/nâng cấp hoàn thành — 03-TSCĐ + tăng nguyên giá 211."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_fa_docs_schema(conn, commit=False)
    vals = asset_book_values(conn, asset_id, as_of=doc_date)
    asset = vals['asset']
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Chi phí nâng cấp phải > 0')
    date_s = str(doc_date or '')[:10]
    doc_no = _next_fa_doc_no(conn, 'BBSC')
    desc = content or f'Nâng cấp TSCĐ {asset.get("ma_tai_san")}'
    cash = (cash_account or '1121').strip() or '1121'
    entry = post_journal_entry(
        conn,
        posting_date=date_s, document_date=date_s,
        document_type='TSSC', document_no=doc_no, document_id=asset_id,
        business_type='NANG_CAP_TSCD', description=desc, created_by=created_by,
        branch_code=(asset.get('branch_code') or None),
        lines=[
            {'sequence': 1, 'account_code': '2111', 'debit': float(amt), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': cash, 'debit': 0, 'credit': float(amt), 'description': desc},
        ],
    )
    # Tăng nguyên giá trên thẻ TS
    try:
        conn.execute(
            f"""
            UPDATE {FIXED_ASSETS_TABLE}
            SET nguyen_gia_tinh_khau_hao = COALESCE(nguyen_gia_tinh_khau_hao,0) + ?
            WHERE id = ?
            """,
            (float(amt), asset_id),
        )
    except sqlite3.Error:
        pass
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_fa_docs (
            doc_type, form_code, doc_no, doc_date, asset_id, asset_code, asset_name,
            amount, old_cost, new_cost, journal_entry_id, content, status, created_by, created_at
        ) VALUES ('upgrade','03-TSCD',?,?,?,?,?,?,?,?,?,?,'posted',?,?)
        """,
        (
            doc_no, date_s, asset_id, asset.get('ma_tai_san'), asset.get('ten_tai_san'),
            float(amt), vals['original_cost'], vals['original_cost'] + float(amt),
            entry['id'], desc, created_by, _now(),
        ),
    )
    if commit:
        conn.commit()
    _stamp_fa_doc_branch(conn, cur.lastrowid, asset)
    return get_fa_doc(conn, cur.lastrowid)


def create_fa_revaluation(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    doc_date: str,
    new_cost,
    content: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Biên bản đánh giá lại TSCĐ — 04-TSCĐ + điều chỉnh 211 / 412 (hoặc 711/811)."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_fa_docs_schema(conn, commit=False)
    vals = asset_book_values(conn, asset_id, as_of=doc_date)
    asset = vals['asset']
    old = _money(vals['original_cost'])
    new = _money(new_cost)
    diff = new - old
    if diff == 0:
        raise ValueError('Giá trị đánh giá lại không đổi')
    date_s = str(doc_date or '')[:10]
    doc_no = _next_fa_doc_no(conn, 'BBDG')
    desc = content or f'Đánh giá lại TSCĐ {asset.get("ma_tai_san")}'
    if diff > 0:
        lines = [
            {'sequence': 1, 'account_code': '2111', 'debit': float(diff), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': '412', 'debit': 0, 'credit': float(diff), 'description': desc},
        ]
    else:
        loss = abs(diff)
        lines = [
            {'sequence': 1, 'account_code': '412', 'debit': float(loss), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': '2111', 'debit': 0, 'credit': float(loss), 'description': desc},
        ]
    # 412 có thể không postable — fallback 711/811
    try:
        entry = post_journal_entry(
            conn, posting_date=date_s, document_date=date_s,
            document_type='TSDG', document_no=doc_no, document_id=asset_id,
            business_type='DANH_GIA_LAI_TSCD', description=desc,
            created_by=created_by,
            branch_code=(asset.get('branch_code') or None),
            lines=lines,
        )
    except ValueError:
        if diff > 0:
            lines[1]['account_code'] = '711'
        else:
            lines[0]['account_code'] = '811'
        entry = post_journal_entry(
            conn, posting_date=date_s, document_date=date_s,
            document_type='TSDG', document_no=doc_no, document_id=asset_id,
            business_type='DANH_GIA_LAI_TSCD', description=desc,
            created_by=created_by,
            branch_code=(asset.get('branch_code') or None),
            lines=lines,
        )
    try:
        conn.execute(
            f"UPDATE {FIXED_ASSETS_TABLE} SET nguyen_gia_tinh_khau_hao = ? WHERE id = ?",
            (float(new), asset_id),
        )
    except sqlite3.Error:
        pass
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_fa_docs (
            doc_type, form_code, doc_no, doc_date, asset_id, asset_code, asset_name,
            amount, old_cost, new_cost, journal_entry_id, content, status, created_by, created_at
        ) VALUES ('revaluation','04-TSCD',?,?,?,?,?,?,?,?,?,?,'posted',?,?)
        """,
        (
            doc_no, date_s, asset_id, asset.get('ma_tai_san'), asset.get('ten_tai_san'),
            float(diff), float(old), float(new), entry['id'], desc, created_by, _now(),
        ),
    )
    if commit:
        conn.commit()
    _stamp_fa_doc_branch(conn, cur.lastrowid, asset)
    return get_fa_doc(conn, cur.lastrowid)


def create_fa_inventory(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    lines: list[dict] | None = None,
    content: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Biên bản tổng hợp kiểm kê TSCĐ — 05-TSCĐ."""
    ensure_sme_fa_docs_schema(conn, commit=False)
    date_s = str(doc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày kiểm kê')
    assets = list_active_assets(conn)
    # include disposed for inventory completeness
    try:
        extra = conn.execute(
            f"""
            SELECT id, ma_tai_san, ten_tai_san, nguyen_gia_tinh_khau_hao, thue_gtgt,
                   gia_mua_chua_thue, so_luong, so_thang_khau_hao, ngay_bat_dau_su_dung, tinh_trang
            FROM {FIXED_ASSETS_TABLE} WHERE tinh_trang = ?
            """,
            (STATUS_DISPOSED,),
        ).fetchall()
        for r in extra:
            d = dict(r)
            d['original_cost'] = _f(_depreciable_cost(r))
            assets.append(d)
    except sqlite3.Error:
        pass

    override = {int(x['asset_id']): x for x in (lines or []) if x.get('asset_id')}
    doc_no = _next_fa_doc_no(conn, 'BBKK')
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_fa_docs (
            doc_type, form_code, doc_no, doc_date, content, status, created_by, created_at
        ) VALUES ('inventory','05-TSCD',?,?,?,'posted',?,?)
        """,
        (doc_no, date_s, content or 'Kiểm kê TSCĐ định kỳ', created_by, _now()),
    )
    doc_id = cur.lastrowid
    for a in assets:
        aid = int(a['id'])
        ov = override.get(aid) or {}
        cur.execute(
            """
            INSERT INTO sme_fa_inventory_lines (
                doc_id, asset_id, asset_code, asset_name, book_cost, actual_ok, note
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                doc_id, aid, a.get('ma_tai_san'), a.get('ten_tai_san'),
                float(a.get('original_cost') or 0),
                0 if ov.get('actual_ok') in (0, False, '0') else 1,
                ov.get('note') or '',
            ),
        )
    if commit:
        conn.commit()
    _stamp_fa_doc_branch(conn, doc_id, None)
    return get_fa_doc(conn, doc_id)


def get_fa_doc(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    ensure_sme_fa_docs_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_fa_docs WHERE id = ?', (doc_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get('doc_type') == 'inventory':
        d['lines'] = [dict(x) for x in conn.execute(
            'SELECT * FROM sme_fa_inventory_lines WHERE doc_id = ? ORDER BY id', (doc_id,)
        ).fetchall()]
    return d


def void_fa_doc(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy biên bản TSCĐ',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Hủy 01/03/04/05-TSCĐ — đảo GL (nếu có) và hoàn nguyên giá khi cần."""
    ensure_sme_fa_docs_schema(conn, commit=False)
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_fa_docs', doc_id, label='Biên bản TSCĐ')
    doc = get_fa_doc(conn, doc_id)
    if not doc:
        raise ValueError('Không tìm thấy biên bản TSCĐ')
    if doc.get('status') == 'void':
        raise ValueError('Biên bản đã hủy')

    dtype = doc.get('doc_type') or ''
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )

    asset_id = doc.get('asset_id')
    if asset_id and dtype == 'upgrade':
        amt = _money(doc.get('amount'))
        conn.execute(
            f"""
            UPDATE {FIXED_ASSETS_TABLE}
            SET nguyen_gia_tinh_khau_hao = CASE
                WHEN COALESCE(nguyen_gia_tinh_khau_hao,0) > ? THEN COALESCE(nguyen_gia_tinh_khau_hao,0) - ?
                ELSE 0 END
            WHERE id = ?
            """,
            (float(amt), float(amt), int(asset_id)),
        )
    elif asset_id and dtype == 'revaluation' and doc.get('old_cost') is not None:
        try:
            conn.execute(
                f"UPDATE {FIXED_ASSETS_TABLE} SET nguyen_gia_tinh_khau_hao = ? WHERE id = ?",
                (float(doc['old_cost']), int(asset_id)),
            )
        except sqlite3.Error:
            pass
    elif asset_id and dtype == 'handover':
        # Hoàn về InStock nếu đang Active (giao nhận đã kích hoạt)
        try:
            conn.execute(
                f"""
                UPDATE {FIXED_ASSETS_TABLE}
                SET tinh_trang = ?
                WHERE id = ? AND tinh_trang = ?
                """,
                (STATUS_IN_STOCK, int(asset_id), STATUS_ACTIVE),
            )
        except sqlite3.Error:
            pass

    conn.execute(
        "UPDATE sme_fa_docs SET status = 'void', content = ? WHERE id = ?",
        ((doc.get('content') or '') + f' | {reason}', doc_id),
    )
    if commit:
        conn.commit()
    return get_fa_doc(conn, doc_id)


def list_fa_docs(
    conn: sqlite3.Connection,
    *,
    doc_type: str | None = None,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_fa_docs_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE
    sql = "SELECT * FROM sme_fa_docs WHERE status != 'void'"
    params: list[Any] = []
    if doc_type:
        sql += ' AND doc_type = ?'
        params.append(doc_type)
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
        else:
            sql += ' AND branch_code = ?'
        params.append(code)
    sql += ' ORDER BY doc_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
