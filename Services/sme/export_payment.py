"""Thanh toán xuất khẩu — unpaid / prepaid / L/C / doc discount.

Doanh thu luôn Nợ 131 / Có 5111 (VAT 0%). Tiền đã/đang thu qua:
  - PT tạm ứng KH (Nợ 1122 / Có 131) — gắn vào phiếu
  - thu sau T/T·L/C sight — settle + CLTG 515/635
  - L/C xuất (direction=export)
  - chiết khấu bộ CT → vay 341
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY_Q = Decimal('0.01')
FX_Q = Decimal('0.0001')

PAYMENT_UNPAID = 'unpaid'
PAYMENT_PREPAID_FULL = 'prepaid_full'
PAYMENT_PREPAID_PARTIAL = 'prepaid_partial'
PAYMENT_LC = 'lc'
PAYMENT_LC_USANCE = 'lc_usance'
PAYMENT_DOC_DISCOUNT = 'doc_discount'

PAYMENT_MODE_LABELS = {
    PAYMENT_UNPAID: 'Thanh toán sau (T/T)',
    PAYMENT_PREPAID_FULL: 'Khách thanh toán trước đủ',
    PAYMENT_PREPAID_PARTIAL: 'Khách tạm ứng một phần',
    PAYMENT_LC: 'L/C trả ngay (sight)',
    PAYMENT_LC_USANCE: 'L/C trả chậm / nhờ thu (Usance · D/A · D/P)',
    PAYMENT_DOC_DISCOUNT: 'Chiết khấu bộ chứng từ (vay 341)',
}

REVENUE_ACCOUNT_DEFAULT = '5111'  # DT XK — cấu hình; không dùng 5113 (dịch vụ)


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _fx(val) -> Decimal:
    rate = Decimal(str(val or 1))
    if rate <= 0:
        return Decimal('1')
    return rate.quantize(FX_Q, rounding=ROUND_HALF_UP)


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except sqlite3.OperationalError:
        return set()


# Cache schema XK theo đường dẫn DB — tránh PRAGMA/CREATE INDEX mỗi API call
_EXPORT_SCHEMA_VERSION = '2026-08-03f'
_export_schema_ready: dict[str, str] = {}


def _db_file_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
        if row:
            path = row[2] if not isinstance(row, sqlite3.Row) else row['file']
            if path:
                return str(path)
    except sqlite3.Error:
        pass
    return f'conn:{id(conn)}'


def ensure_export_sale_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Cột sale + bảng liên kết tạm ứng / chi phí / chiết khấu XK."""
    db_key = _db_file_key(conn)
    if _export_schema_ready.get(db_key) == _EXPORT_SCHEMA_VERSION:
        return

    if 'sale' not in {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }:
        _export_schema_ready[db_key] = _EXPORT_SCHEMA_VERSION
        return

    extras = [
        ('sale_type', "TEXT DEFAULT 'DOMESTIC'"),
        ('payment_mode', "TEXT DEFAULT 'unpaid'"),
        ('currency', "TEXT DEFAULT 'VND'"),
        ('exchange_rate', 'REAL DEFAULT 1'),
        ('customs_fx_rate', 'REAL DEFAULT 1'),
        ('amount_fc', 'REAL DEFAULT 0'),
        ('advance_fc', 'REAL DEFAULT 0'),
        ('advance_vnd', 'REAL DEFAULT 0'),
        ('export_tax_fc', 'REAL DEFAULT 0'),
        ('export_tax_vnd', 'REAL DEFAULT 0'),
        ('linked_lc_id', 'INTEGER'),
        ('incoterms', 'TEXT'),
        ('bl_no', 'TEXT'),
        ('customs_decl_no', 'TEXT'),
        ('risk_transfer_date', 'TEXT'),
        ('ar_status', "TEXT DEFAULT 'open'"),
        ('settle_journal_id', 'INTEGER'),
        ('settle_amount_fc', 'REAL DEFAULT 0'),
        ('discount_loan_id', 'INTEGER'),
        ('warehouse_code', 'TEXT'),
        ('branch_code', 'TEXT'),
    ]
    names = _cols(conn, 'sale')
    for col, decl in extras:
        if col not in names:
            try:
                conn.execute(f'ALTER TABLE sale ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass

    item_extras = [
        ('warehouse_code', 'TEXT'),
        ('line_type', "TEXT DEFAULT 'goods'"),
    ]
    icols = _cols(conn, 'sale_items')
    for col, decl in item_extras:
        if col not in icols:
            try:
                conn.execute(f'ALTER TABLE sale_items ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_sale_advances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            voucher_id INTEGER NOT NULL,
            amount_fc REAL NOT NULL DEFAULT 0,
            exchange_rate REAL NOT NULL DEFAULT 1,
            amount_vnd REAL NOT NULL DEFAULT 0,
            UNIQUE(sale_id, voucher_id)
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_sale_advances_sale '
        'ON sme_sale_advances(sale_id)'
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_export_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            cost_date TEXT NOT NULL,
            description TEXT,
            amount_vnd REAL NOT NULL DEFAULT 0,
            vat_vnd REAL NOT NULL DEFAULT 0,
            credit_account TEXT NOT NULL DEFAULT '1121',
            payment_method TEXT DEFAULT 'bank',
            journal_entry_id INTEGER,
            voucher_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_export_costs_sale '
        'ON sme_export_costs(sale_id)'
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_export_doc_discounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            discount_date TEXT NOT NULL,
            amount_fc REAL NOT NULL DEFAULT 0,
            exchange_rate REAL NOT NULL DEFAULT 1,
            amount_vnd REAL NOT NULL DEFAULT 0,
            fee_vnd REAL NOT NULL DEFAULT 0,
            cash_account TEXT NOT NULL DEFAULT '1122',
            loan_account TEXT NOT NULL DEFAULT '3411',
            journal_entry_id INTEGER,
            settle_journal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # L/C: chiều xuất khẩu
    try:
        from Services.sme.letter_of_credit import ensure_sme_lc_schema
        ensure_sme_lc_schema(conn, commit=False)
        lc_cols = _cols(conn, 'sme_lc_docs')
        if 'direction' not in lc_cols:
            conn.execute(
                "ALTER TABLE sme_lc_docs ADD COLUMN direction TEXT DEFAULT 'import'"
            )
        if 'sale_id' not in lc_cols:
            conn.execute('ALTER TABLE sme_lc_docs ADD COLUMN sale_id INTEGER')
    except Exception:
        pass

    if commit:
        conn.commit()

    _export_schema_ready[db_key] = _EXPORT_SCHEMA_VERSION


def normalize_payment_mode(raw, *, sale_type: str = 'DOMESTIC') -> str:
    code = (raw or '').strip().lower().replace('-', '_').replace(' ', '_')
    aliases = {
        'unpaid': PAYMENT_UNPAID,
        'credit': PAYMENT_UNPAID,
        'tt': PAYMENT_UNPAID,
        'tt_sight': PAYMENT_UNPAID,
        'prepaid': PAYMENT_PREPAID_FULL,
        'prepaid_full': PAYMENT_PREPAID_FULL,
        'full_advance': PAYMENT_PREPAID_FULL,
        'prepaid_partial': PAYMENT_PREPAID_PARTIAL,
        'partial_advance': PAYMENT_PREPAID_PARTIAL,
        'advance': PAYMENT_PREPAID_PARTIAL,
        'lc': PAYMENT_LC,
        'lc_sight': PAYMENT_LC,
        'letter_of_credit': PAYMENT_LC,
        'lc_usance': PAYMENT_LC_USANCE,
        'usance': PAYMENT_LC_USANCE,
        'da': PAYMENT_LC_USANCE,
        'dp': PAYMENT_LC_USANCE,
        'documentary': PAYMENT_LC_USANCE,
        'doc_discount': PAYMENT_DOC_DISCOUNT,
        'discount': PAYMENT_DOC_DISCOUNT,
        'paid': 'paid',
    }
    mode = aliases.get(code, '')
    t = (sale_type or 'DOMESTIC').strip().upper()
    if t != 'EXPORT':
        if mode == 'paid':
            return 'paid'
        return mode if mode in ('paid', PAYMENT_UNPAID) else PAYMENT_UNPAID
    if mode in PAYMENT_MODE_LABELS:
        return mode
    return PAYMENT_UNPAID


def payment_status_label(mode: str) -> str:
    return PAYMENT_MODE_LABELS.get(mode, 'Chưa thanh toán')


def get_customer_advance_voucher(conn: sqlite3.Connection, voucher_id: int) -> dict | None:
    ensure_export_sale_schema(conn, commit=False)
    row = conn.execute(
        """
        SELECT id, voucher_no, voucher_date, party_name, amount,
               COALESCE(amount_fc, 0) AS amount_fc,
               COALESCE(exchange_rate, 1) AS exchange_rate,
               COALESCE(currency, 'VND') AS currency,
               purpose, status, credit_account, debit_account
        FROM sme_vouchers WHERE id = ?
        """,
        (voucher_id,),
    ).fetchone()
    if not row:
        return None
    return normalize_advance_amounts(dict(row))


def normalize_advance_amounts(raw: dict[str, Any], *, fallback_rate=None) -> dict[str, Any]:
    out = dict(raw or {})
    rate = _fx(out.get('exchange_rate') or fallback_rate or 1)
    fc = _money(out.get('amount_fc') or 0)
    vnd = _money(out.get('amount_vnd') or out.get('amount') or 0)
    if fc <= 0 and vnd > 0 and rate > 0:
        fc = _money(vnd / rate)
    if vnd <= 0 and fc > 0:
        vnd = _money(fc * rate)
    out['amount_fc'] = float(fc)
    out['exchange_rate'] = float(rate)
    out['amount_vnd'] = float(vnd)
    out['amount'] = float(vnd)
    return out


def get_advance_voucher_balance(
    conn: sqlite3.Connection,
    voucher_id: int,
    *,
    exclude_sale_id: int | None = None,
) -> dict[str, Any]:
    """Số dư PT tạm ứng KH còn dùng cho các đợt xuất tiếp theo."""
    ensure_export_sale_schema(conn, commit=False)
    vrow = get_customer_advance_voucher(conn, voucher_id)
    if not vrow:
        raise ValueError(f'Không tìm thấy phiếu thu tạm ứng #{voucher_id}')
    face_fc = _money(vrow.get('amount_fc') or 0)
    face_vnd = _money(vrow.get('amount_vnd') or vrow.get('amount') or 0)
    rate = _fx(vrow.get('exchange_rate') or 1)

    sql = """
        SELECT COALESCE(SUM(amount_fc), 0), COALESCE(SUM(amount_vnd), 0), COUNT(*)
        FROM sme_sale_advances WHERE voucher_id = ?
    """
    params: list[Any] = [int(voucher_id)]
    if exclude_sale_id:
        sql += ' AND sale_id != ?'
        params.append(int(exclude_sale_id))
    used = conn.execute(sql, params).fetchone()
    used_fc = _money(used[0])
    used_vnd = _money(used[1])
    remain_fc = max(_money(face_fc - used_fc), Decimal('0.00'))
    remain_vnd = max(_money(face_vnd - used_vnd), Decimal('0.00'))
    if remain_vnd <= 0 and remain_fc > 0:
        remain_vnd = _money(remain_fc * rate)

    return {
        'voucher_id': int(voucher_id),
        'voucher_no': vrow.get('voucher_no'),
        'voucher_date': vrow.get('voucher_date'),
        'party_name': vrow.get('party_name'),
        'currency': vrow.get('currency') or 'USD',
        'exchange_rate': float(rate),
        'face_fc': float(face_fc),
        'face_vnd': float(face_vnd),
        'used_fc': float(used_fc),
        'used_vnd': float(used_vnd),
        'remaining_fc': float(remain_fc),
        'remaining_vnd': float(remain_vnd),
        'can_link_sale': remain_fc > Decimal('0.00005'),
        'amount_fc': float(remain_fc),
        'amount_vnd': float(remain_vnd),
        'amount': float(remain_vnd),
        **{k: vrow.get(k) for k in ('id', 'purpose', 'status', 'credit_account')},
    }


def list_customer_advances(
    conn: sqlite3.Connection,
    *,
    customer_name: str | None = None,
    currency: str | None = None,
    unused_only: bool = True,
    include_sale_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_export_sale_schema(conn, commit=False)
    from Services.sme.vouchers import ensure_sme_voucher_schema
    ensure_sme_voucher_schema(conn, commit=False)
    if 'purpose' not in _cols(conn, 'sme_vouchers'):
        return []

    sql = """
        SELECT v.id, v.voucher_no, v.voucher_date, v.party_name, v.amount,
               COALESCE(v.amount_fc, 0) AS amount_fc,
               COALESCE(v.exchange_rate, 1) AS exchange_rate,
               COALESCE(v.currency, 'VND') AS currency,
               v.debit_account, v.credit_account, v.reason, v.status, v.purpose
        FROM sme_vouchers v
        WHERE v.voucher_type = 'receipt'
          AND v.status = 'posted'
          AND COALESCE(v.purpose, '') = 'customer_advance'
          AND v.credit_account LIKE '131%'
        ORDER BY date(v.voucher_date) DESC, v.id DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (int(limit) * 3,)).fetchall()
    name = (customer_name or '').strip().lower()
    cur = (currency or '').strip().upper()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if name and name not in (d.get('party_name') or '').lower():
            continue
        if cur and cur != 'VND' and (d.get('currency') or 'VND').upper() != cur:
            continue
        bal = get_advance_voucher_balance(
            conn, int(d['id']), exclude_sale_id=include_sale_id,
        )
        on_this = False
        if include_sale_id:
            link = conn.execute(
                'SELECT amount_fc, amount_vnd FROM sme_sale_advances '
                'WHERE voucher_id = ? AND sale_id = ?',
                (int(d['id']), int(include_sale_id)),
            ).fetchone()
            on_this = bool(link)
            if on_this:
                avail_fc = _money(bal['remaining_fc']) + _money(link[0])
                avail_vnd = _money(bal['remaining_vnd']) + _money(link[1])
                bal['remaining_fc'] = float(avail_fc)
                bal['remaining_vnd'] = float(avail_vnd)
                bal['amount_fc'] = float(avail_fc)
                bal['amount_vnd'] = float(avail_vnd)
                bal['can_link_sale'] = avail_fc > Decimal('0.00005')
        if unused_only and not bal.get('can_link_sale') and not on_this:
            continue
        merged = {**d, **bal}
        out.append(merged)
        if len(out) >= int(limit):
            break
    return out


def build_advance_payloads_from_request(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    exchange_rate=None,
    exclude_sale_id: int | None = None,
) -> list[dict[str, Any]]:
    if exclude_sale_id is None:
        for key in ('sale_id', 'edit_id', 'id'):
            raw = data.get(key)
            if raw not in (None, '', 0, '0'):
                try:
                    exclude_sale_id = int(raw)
                    break
                except (TypeError, ValueError):
                    pass

    payloads: list[dict[str, Any]] = []
    seen: set[int] = set()
    raw_list = data.get('advances') or data.get('linked_advances') or []
    if isinstance(raw_list, dict):
        raw_list = [raw_list]
    if not isinstance(raw_list, list):
        raw_list = []

    for item in raw_list:
        if not isinstance(item, dict):
            continue
        try:
            vid = int(item.get('voucher_id') or item.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if not vid or vid in seen:
            continue
        vrow = get_customer_advance_voucher(conn, vid)
        if not vrow:
            raise ValueError(f'Không tìm thấy PT tạm ứng #{vid}')
        if (vrow.get('purpose') or '') != 'customer_advance':
            raise ValueError(f'PT #{vid} không phải tạm ứng khách hàng')
        bal = get_advance_voucher_balance(
            conn, vid, exclude_sale_id=exclude_sale_id,
        )
        max_fc = _money(bal.get('remaining_fc') or 0)
        merged = normalize_advance_amounts(
            {
                **vrow,
                'amount_fc': item.get('amount_fc', bal.get('remaining_fc')),
                'exchange_rate': item.get('exchange_rate', vrow.get('exchange_rate')),
                'amount_vnd': item.get('amount_vnd'),
                'amount': item.get('amount_vnd') or item.get('amount') or bal.get('remaining_vnd'),
            },
            fallback_rate=exchange_rate,
        )
        use_fc = _money(merged.get('amount_fc') or 0)
        if use_fc <= 0:
            raise ValueError(
                f'Tạm ứng PT {vrow.get("voucher_no") or vid} hết số dư'
            )
        if use_fc > max_fc + Decimal('0.0001'):
            raise ValueError(
                f'Tạm ứng PT {vrow.get("voucher_no") or vid} '
                f'({float(use_fc):g} NT) vượt số dư còn lại ({float(max_fc):g} NT)'
            )
        payloads.append({
            'voucher_id': vid,
            'amount_fc': float(use_fc),
            'exchange_rate': float(merged['exchange_rate']),
            'amount_vnd': float(merged['amount_vnd']),
        })
        seen.add(vid)

    ids_raw = data.get('advance_voucher_ids') or data.get('advance_ids') or []
    if not isinstance(ids_raw, list):
        ids_raw = [ids_raw]
    for raw_vid in ids_raw:
        try:
            vid = int(raw_vid)
        except (TypeError, ValueError):
            continue
        if not vid or vid in seen:
            continue
        bal = get_advance_voucher_balance(
            conn, vid, exclude_sale_id=exclude_sale_id,
        )
        use_fc = _money(bal.get('remaining_fc') or 0)
        if use_fc <= 0:
            raise ValueError(f'PT #{vid} không còn số dư tạm ứng')
        payloads.append({
            'voucher_id': vid,
            'amount_fc': float(use_fc),
            'exchange_rate': float(bal.get('exchange_rate') or 1),
            'amount_vnd': float(bal.get('remaining_vnd') or 0),
        })
        seen.add(vid)
    return payloads


def compute_split_fx_revenue_vnd(
    *,
    total_fc,
    revenue_rate,
    advances: list[dict],
) -> dict[str, Any]:
    """Nguyên tắc TH1: phần ứng cố định theo TG ngày ứng; phần còn theo TG ngày DT."""
    total = _money(total_fc)
    rate = _fx(revenue_rate)
    if total < 0:
        total = Decimal('0.00')

    adv_fc = Decimal('0.00')
    adv_vnd = Decimal('0.00')
    detail = []
    for a in advances or []:
        ar = _fx(a.get('exchange_rate') or rate)
        fc = _money(a.get('amount_fc') or 0)
        vnd = _money(a.get('amount_vnd') or 0)
        if fc <= 0 and vnd > 0 and ar > 0:
            fc = _money(vnd / ar)
        if vnd <= 0 and fc > 0:
            vnd = _money(fc * ar)
        adv_fc += fc
        adv_vnd += vnd
        detail.append({
            'voucher_id': a.get('voucher_id') or a.get('id'),
            'amount_fc': float(fc),
            'exchange_rate': float(ar),
            'amount_vnd': float(vnd),
        })

    if adv_fc > total and adv_fc > 0:
        scale = total / adv_fc
        adv_vnd = Decimal('0.00')
        for d in detail:
            d['amount_fc'] = float(_money(Decimal(str(d['amount_fc'])) * scale))
            d['amount_vnd'] = float(
                _money(Decimal(str(d['amount_fc'])) * Decimal(str(d['exchange_rate'])))
            )
            adv_vnd += _money(d['amount_vnd'])
        adv_fc = total

    remain_fc = _money(total - adv_fc)
    remain_vnd = _money(remain_fc * rate)
    revenue_vnd = _money(adv_vnd + remain_vnd)
    bank_only_vnd = _money(total * rate)

    return {
        'total_fc': float(total),
        'revenue_rate': float(rate),
        'advance_fc': float(adv_fc),
        'advance_vnd': float(adv_vnd),
        'remain_fc': float(remain_fc),
        'remain_vnd': float(remain_vnd),
        'revenue_vnd': float(revenue_vnd),
        'bank_only_vnd': float(bank_only_vnd),
        'fx_adjustment': float(_money(revenue_vnd - bank_only_vnd)),
        'advances': detail,
    }


def replace_sale_advances(
    conn: sqlite3.Connection,
    sale_id: int,
    advances: list[dict],
    *,
    commit: bool = False,
) -> list[dict[str, Any]]:
    ensure_export_sale_schema(conn, commit=False)
    conn.execute('DELETE FROM sme_sale_advances WHERE sale_id = ?', (sale_id,))
    saved = []
    for a in advances or []:
        vid = a.get('voucher_id') or a.get('id')
        if not vid:
            continue
        fc = _money(a.get('amount_fc') or 0)
        rate = _fx(a.get('exchange_rate') or 1)
        vnd = _money(a.get('amount_vnd') or (fc * rate))
        conn.execute(
            """
            INSERT INTO sme_sale_advances
            (sale_id, voucher_id, amount_fc, exchange_rate, amount_vnd)
            VALUES (?,?,?,?,?)
            """,
            (sale_id, int(vid), float(fc), float(rate), float(vnd)),
        )
        saved.append({
            'voucher_id': int(vid),
            'amount_fc': float(fc),
            'exchange_rate': float(rate),
            'amount_vnd': float(vnd),
        })
    if commit:
        conn.commit()
    return saved


def list_sale_advances(conn: sqlite3.Connection, sale_id: int) -> list[dict]:
    ensure_export_sale_schema(conn, commit=False)
    rows = conn.execute(
        """
        SELECT a.*, v.voucher_no, v.voucher_date, v.party_name
        FROM sme_sale_advances a
        LEFT JOIN sme_vouchers v ON v.id = a.voucher_id
        WHERE a.sale_id = ?
        ORDER BY a.id
        """,
        (sale_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def validate_export_payment(
    *,
    payment_mode: str,
    total_fc,
    advances: list[dict],
    lc_id: int | None,
) -> None:
    mode = normalize_payment_mode(payment_mode, sale_type='EXPORT')
    total = _money(total_fc)
    adv_fc = sum((_money(a.get('amount_fc') or 0) for a in (advances or [])), Decimal('0.00'))

    if mode == PAYMENT_UNPAID:
        return
    if mode in (PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL):
        if not advances:
            raise ValueError('Chọn ít nhất một phiếu thu tạm ứng khách hàng')
        if mode == PAYMENT_PREPAID_FULL and adv_fc + Decimal('0.0001') < total:
            raise ValueError(
                f'Tạm ứng ({float(adv_fc):g} NT) chưa đủ giá trị hàng ({float(total):g} NT). '
                f'Chọn thêm PT hoặc đổi sang «ứng một phần».'
            )
        if mode == PAYMENT_PREPAID_PARTIAL and adv_fc <= 0:
            raise ValueError('Số tạm ứng phải > 0')
        if mode == PAYMENT_PREPAID_PARTIAL and adv_fc >= total:
            raise ValueError('Ứng đủ 100% — hãy chọn «thanh toán trước đủ»')
        return
    if mode in (PAYMENT_LC, PAYMENT_LC_USANCE):
        if not lc_id:
            raise ValueError('Chọn thư tín dụng (L/C) xuất khẩu')
        return
    if mode == PAYMENT_DOC_DISCOUNT:
        return
