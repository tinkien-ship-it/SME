"""Tài khoản ngân hàng kế toán SME — mặc định = STK VietQR; cảnh báo khi có nhiều TK."""
from __future__ import annotations

import sqlite3
from typing import Any
from db_utils import sqlite_commit

DEFAULT_BANK_SETTING_KEY = 'sme_default_bank_account'
DEFAULT_FX_BANK_SETTING_KEY = 'sme_default_fx_bank_account'

# Khi mở TK con đầu tiên dưới 1121/1122: giữ …1 cho TK mặc định + chuyển số liệu cũ.
# Quy định: cấp 2 (4 số) → cấp 3 (5 số), ví dụ 1121 → 11211, 11212.
BANK_DETAIL_SPLIT = {
    '1121': {
        'default_code': '11211',
        'setting_key': DEFAULT_BANK_SETTING_KEY,
        'kind': 'vnd',
        'label_vi': 'tiền Việt (VietQR / giao dịch chính)',
    },
    '1122': {
        'default_code': '11221',
        'setting_key': DEFAULT_FX_BANK_SETTING_KEY,
        'kind': 'fx',
        'label_vi': 'ngoại tệ mặc định',
    },
}


def _setting_get(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    if not row:
        return default
    val = row[0] if not isinstance(row, sqlite3.Row) else row['value']
    return (val or default) or default


def _setting_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
        (key, str(value or '')),
    )


def _list_postable_under(conn: sqlite3.Connection, prefix: str) -> list[dict[str, Any]]:
    """Các TK postable thuộc nhóm prefix (vd 1121, 1122)."""
    from Services.sme.coa_service import ensure_sme_coa_ready

    ensure_sme_coa_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT code, name, parent_code, level, is_postable, is_custom
        FROM sme_chart_of_accounts
        WHERE is_active = 1
          AND is_postable = 1
          AND (code = ? OR code LIKE ?)
        ORDER BY code
        """,
        (prefix, f'{prefix}%'),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_first_postable(conn: sqlite3.Connection, prefix: str) -> str:
    """TK postable đầu tiên dưới prefix; nếu không có thì trả prefix."""
    rows = _list_postable_under(conn, prefix)
    if rows:
        return str(rows[0]['code'])
    return prefix


def is_postable_code(conn: sqlite3.Connection, code: str) -> bool:
    from Services.sme.coa_service import get_account

    acc = get_account(conn, code, commit=False)
    return bool(acc and acc.get('is_active') and acc.get('is_postable'))


def get_default_bank_account(conn: sqlite3.Connection) -> str:
    """TK TGNH VND mặc định (= STK VietQR / giao dịch chính)."""
    preferred = (_setting_get(conn, DEFAULT_BANK_SETTING_KEY) or '').strip()
    if preferred and is_postable_code(conn, preferred) and preferred.startswith('1121'):
        return preferred
    # Ưu tiên đúng mã 1121 nếu còn postable; không thì TK con đầu tiên
    if is_postable_code(conn, '1121'):
        return '1121'
    return resolve_first_postable(conn, '1121')


def get_default_fx_bank_account(conn: sqlite3.Connection) -> str:
    """TK TGNH ngoại tệ mặc định (1122*)."""
    preferred = (_setting_get(conn, DEFAULT_FX_BANK_SETTING_KEY) or '').strip()
    if preferred and is_postable_code(conn, preferred) and preferred.startswith('1122'):
        return preferred
    if is_postable_code(conn, '1122'):
        return '1122'
    return resolve_first_postable(conn, '1122')


def set_default_bank_account(conn: sqlite3.Connection, code: str, *, commit: bool = False) -> str:
    code_s = (code or '').strip()
    if not code_s.startswith('1121'):
        raise ValueError('Tài khoản mặc định VietQR phải thuộc nhóm 1121 (tiền Việt)')
    if not is_postable_code(conn, code_s):
        raise ValueError(f'Tài khoản {code_s} không ghi sổ được — chọn TK chi tiết')
    _setting_set(conn, DEFAULT_BANK_SETTING_KEY, code_s)
    if commit:
        sqlite_commit(conn, label='bank_accounts')
    return code_s


def sync_default_bank_from_qr(conn: sqlite3.Connection, *, commit: bool = False) -> dict[str, Any]:
    """Gắn STK VietQR (business_info) làm TK giao dịch chính mặc định trên chứng từ.

    Không tự mở TK con — dùng 1121 (hoặc TK con postable đang có).
    Giữ nguyên mặc định cũ nếu vẫn hợp lệ.
    """
    from Services.sme.coa_service import ensure_sme_coa_ready

    ensure_sme_coa_ready(conn, commit=False)
    row = conn.execute(
        'SELECT bank_name, bank_account, bank_code, account_holder FROM business_info LIMIT 1'
    ).fetchone()
    biz = dict(row) if row else {}

    current = (_setting_get(conn, DEFAULT_BANK_SETTING_KEY) or '').strip()
    if current and is_postable_code(conn, current) and current.startswith('1121'):
        default_code = current
    else:
        default_code = get_default_bank_account(conn)
        _setting_set(conn, DEFAULT_BANK_SETTING_KEY, default_code)

    # FX mặc định (không gắn QR)
    fx_cur = (_setting_get(conn, DEFAULT_FX_BANK_SETTING_KEY) or '').strip()
    if not (fx_cur and is_postable_code(conn, fx_cur) and fx_cur.startswith('1122')):
        fx_code = get_default_fx_bank_account(conn)
        _setting_set(conn, DEFAULT_FX_BANK_SETTING_KEY, fx_code)
    else:
        fx_code = fx_cur

    if commit:
        sqlite_commit(conn, label='bank_accounts')

    vnd_list = _list_postable_under(conn, '1121')
    fx_list = _list_postable_under(conn, '1122')
    return {
        'default_bank_account': default_code,
        'default_fx_bank_account': fx_code,
        'qr_bank_account': (biz.get('bank_account') or '').strip(),
        'qr_bank_name': (biz.get('bank_name') or '').strip(),
        'vnd_count': len(vnd_list),
        'fx_count': len(fx_list),
        'needs_choice': len(vnd_list) > 1 or len(fx_list) > 1,
    }


def list_bank_payment_accounts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Payload UI: danh sách TK NH + mặc định + cờ cần chọn khi có nhiều TK."""
    sync = sync_default_bank_from_qr(conn, commit=False)
    default_vnd = sync['default_bank_account']
    default_fx = sync['default_fx_bank_account']
    vnd = _list_postable_under(conn, '1121')
    fx = _list_postable_under(conn, '1122')

    def _pack(rows: list[dict], default_code: str, kind: str) -> list[dict]:
        out = []
        for r in rows:
            code = str(r['code'])
            is_def = code == default_code
            label = f"{code} — {r.get('name') or ''}".strip(' —')
            if is_def and kind == 'vnd':
                label += ' (QR / mặc định)'
            elif is_def and kind == 'fx':
                label += ' (mặc định NT)'
            out.append({
                'code': code,
                'name': r.get('name') or '',
                'kind': kind,
                'currency_group': 'VND' if kind == 'vnd' else 'FX',
                'is_default': is_def,
                'is_custom': int(r.get('is_custom') or 0),
                'label': label,
            })
        return out

    accounts = _pack(vnd, default_vnd, 'vnd') + _pack(fx, default_fx, 'fx')
    return {
        'accounts': accounts,
        'vnd_accounts': [a for a in accounts if a['kind'] == 'vnd'],
        'fx_accounts': [a for a in accounts if a['kind'] == 'fx'],
        'default_bank_account': default_vnd,
        'default_fx_bank_account': default_fx,
        'qr_bank_account': sync.get('qr_bank_account') or '',
        'qr_bank_name': sync.get('qr_bank_name') or '',
        'needs_choice': bool(sync.get('needs_choice')),
        'warning': (
            'Doanh nghiệp có nhiều tài khoản ngân hàng (tiền Việt / ngoại tệ). '
            'Hãy chọn đúng tài khoản trên chứng từ — mặc định là STK VietQR.'
            if sync.get('needs_choice') else ''
        ),
    }


def resolve_explicit_cash_account(conn: sqlite3.Connection, payment_method: str) -> str | None:
    """Nếu payment_method là mã TK 111*/112* hợp lệ (postable) thì trả về."""
    raw = (payment_method or '').strip()
    if not raw or not raw[0].isdigit():
        return None
    code = raw.strip()
    if not all(ch.isdigit() for ch in code):
        return None
    if not (code.startswith('111') or code.startswith('112')):
        return None
    if is_postable_code(conn, code):
        return code
    return None


def _map_payment_keyword(payment_method: str, *, currency: str = 'VND') -> str:
    method = (payment_method or 'cash').strip().lower()
    cur = (currency or 'VND').strip().upper() or 'VND'
    fx = cur != 'VND'
    if method in ('1122', 'bank_fx', 'fx_bank'):
        return '1122'
    if method in ('1112', 'cash_fx', 'fx_cash'):
        return '1112'
    if method in ('112', 'bank', 'bank_transfer', 'ck', 'transfer', '1121'):
        return '1122' if fx else '1121'
    if method in ('111', 'cash', '1111'):
        return '1112' if fx else '1111'
    if method.startswith('111') or method.startswith('112'):
        return method if method[:4].isdigit() else method.upper()
    return '1122' if fx else '1111'


def resolve_cash_gl_account(
    conn: sqlite3.Connection,
    payment_method: str,
    *,
    currency: str = 'VND',
) -> str:
    """Map hình thức thanh toán → mã TK tiền ghi sổ (ưu tiên mặc định QR)."""
    explicit = resolve_explicit_cash_account(conn, payment_method)
    if explicit:
        return explicit

    mapped = _map_payment_keyword(payment_method, currency=currency)
    if mapped.startswith('1121') or mapped == '112':
        return get_default_bank_account(conn)
    if mapped.startswith('1122'):
        return get_default_fx_bank_account(conn)
    if mapped.startswith('1111') or mapped == '111':
        return '1111' if is_postable_code(conn, '1111') else resolve_first_postable(conn, '1111')
    if mapped.startswith('1112'):
        return '1112' if is_postable_code(conn, '1112') else resolve_first_postable(conn, '1112')
    return mapped


def count_active_children(conn: sqlite3.Connection, parent_code: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM sme_chart_of_accounts
        WHERE parent_code = ? AND is_active = 1
        """,
        (parent_code,),
    ).fetchone()
    return int(row[0] if row else 0)


def _default_detail_name(conn: sqlite3.Connection, parent_code: str) -> str:
    row = conn.execute(
        'SELECT bank_name, bank_account, account_holder FROM business_info LIMIT 1'
    ).fetchone()
    biz = dict(row) if row else {}
    bank = (biz.get('bank_name') or '').strip()
    stk = (biz.get('bank_account') or '').strip()
    holder = (biz.get('account_holder') or '').strip()
    if parent_code == '1121':
        parts = ['VND']
        if bank:
            parts.append(bank)
        if stk:
            parts.append(f'STK {stk}')
        elif holder:
            parts.append(holder)
        parts.append('mặc định QR')
        return ' — '.join(parts)
    return 'Ngoại tệ — tài khoản mặc định'


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except sqlite3.Error:
        return set()


def _migrate_exact_account_refs(
    conn: sqlite3.Connection,
    old_code: str,
    new_code: str,
) -> dict[str, int]:
    """Gán lại mọi tham chiếu đúng mã ``old_code`` sang ``new_code`` (không đụng TK con)."""
    stats: dict[str, int] = {}

    def _upd(table: str, col: str) -> None:
        cols = _table_columns(conn, table)
        if col not in cols:
            return
        cur = conn.execute(
            f'UPDATE {table} SET {col} = ? WHERE {col} = ?',
            (new_code, old_code),
        )
        stats[f'{table}.{col}'] = int(cur.rowcount or 0)

    _upd('sme_journal_lines', 'account_code')
    _upd('sme_vouchers', 'debit_account')
    _upd('sme_vouchers', 'credit_account')
    _upd('sme_loans', 'cash_account')
    _upd('sme_deposits', 'cash_account')
    _upd('sme_capital_docs', 'cash_account')
    _upd('sme_lc_docs', 'cash_account')
    _upd('sme_export_doc_discounts', 'cash_account')
    _upd('sme_fa_docs', 'cash_account')
    _upd('sme_cash_counts', 'account_code')
    return stats


def preview_bank_split(conn: sqlite3.Connection, parent_code: str) -> dict[str, Any] | None:
    """Thông tin hướng dẫn UI khi mở TK thêm dưới 1121/1122."""
    parent = (parent_code or '').strip()
    cfg = BANK_DETAIL_SPLIT.get(parent)
    if not cfg:
        return None
    kids = count_active_children(conn, parent)
    default_code = cfg['default_code']
    next_new = f'{parent}2'
    if kids == 0:
        return {
            'parent_code': parent,
            'will_auto_split': True,
            'default_code': default_code,
            'new_account_code': next_new,
            'kind': cfg['kind'],
            'message': (
                f'Lần đầu mở thêm tài khoản dưới <b>{parent}</b>, hệ thống sẽ tự động:<br>'
                f'1) Tạo <b>{default_code}</b> làm tài khoản <b>{cfg["label_vi"]}</b>.<br>'
                f'2) Chuyển toàn bộ số liệu / bút toán đã ghi trên <b>{parent}</b> sang <b>{default_code}</b>.<br>'
                f'3) Tài khoản mới bạn đang tạo sẽ là <b>{next_new}</b> (hoặc mã bạn nhập, khác {default_code}).<br>'
                f'<span class="text-muted">Sau bước này, chứng từ mặc định dùng {default_code}; '
                f'khi có nhiều TK sẽ cảnh báo chọn đúng VND / ngoại tệ.</span>'
            ),
        }
    # Đã tách — chỉ nhắc quy ước
    return {
        'parent_code': parent,
        'will_auto_split': False,
        'default_code': default_code,
        'new_account_code': None,
        'kind': cfg['kind'],
        'message': (
            f'Tài khoản mặc định ({cfg["label_vi"]}) là <b>{default_code}</b>. '
            f'Tài khoản mới bạn tạo thêm dùng để theo dõi STK khác — '
            f'trên chứng từ hãy chọn đúng mã TK.'
        ),
    }


def ensure_bank_default_child_on_first_split(
    conn: sqlite3.Connection,
    parent_code: str,
    *,
    insert_child_fn,
) -> dict[str, Any] | None:
    """Khi mở TK con đầu tiên dưới 1121/1122: tạo …1 (cấp 3), chuyển số liệu, set mặc định.

    ``insert_child_fn(code, name, ...)`` — hàm nội bộ tạo dòng COA (không commit).
    Trả None nếu không cần tách; ngược lại dict thống kê.
    """
    parent = (parent_code or '').strip()
    cfg = BANK_DETAIL_SPLIT.get(parent)
    if not cfg:
        return None
    if count_active_children(conn, parent) > 0:
        return None

    default_code = cfg['default_code']
    if get_account_safe(conn, default_code):
        # Đã có mã mặc định (…1) — chỉ set mặc định + migrate nếu còn dòng đúng parent
        stats = _migrate_exact_account_refs(conn, parent, default_code)
        _setting_set(conn, cfg['setting_key'], default_code)
        return {
            'created_default': False,
            'default_code': default_code,
            'migrated': stats,
            'parent_code': parent,
        }

    name = _default_detail_name(conn, parent)
    insert_child_fn(
        code=default_code,
        name=name,
        parent_code=parent,
        custom_reason='Tự động: tài khoản mặc định (STK VietQR / giao dịch chính)',
        description=(
            f'Tách từ {parent}: giữ số dư và lịch sử chứng từ cũ. '
            f'Không xóa — dùng làm TK mặc định trên chứng từ.'
        ),
    )
    stats = _migrate_exact_account_refs(conn, parent, default_code)
    _setting_set(conn, cfg['setting_key'], default_code)
    return {
        'created_default': True,
        'default_code': default_code,
        'default_name': name,
        'migrated': stats,
        'parent_code': parent,
        'message': (
            f'Đã tạo {default_code} ({name}) và chuyển số liệu từ {parent} sang {default_code}.'
        ),
    }


def get_account_safe(conn: sqlite3.Connection, code: str):
    from Services.sme.coa_service import get_account
    return get_account(conn, code, commit=False)
