"""Phương thức thanh toán nhập khẩu — unpaid / prepaid / advance / L/C.

G1 luôn Có 331 (không ghi tiền trên phiếu nhập). Tiền đã đi qua:
  - PC tạm ứng NCC (Nợ 331 / Có 1122) — gắn vào phiếu
  - hoặc L/C ký quỹ 244 — gắn lc_id
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')
FX_Q = Decimal('0.0001')

PAYMENT_UNPAID = 'unpaid'
PAYMENT_PREPAID_FULL = 'prepaid_full'
PAYMENT_PREPAID_PARTIAL = 'prepaid_partial'
PAYMENT_LC = 'lc'

PAYMENT_MODE_LABELS = {
    PAYMENT_UNPAID: 'Chưa thanh toán',
    PAYMENT_PREPAID_FULL: 'Đã thanh toán trước đủ',
    PAYMENT_PREPAID_PARTIAL: 'Đã ứng một phần',
    PAYMENT_LC: 'Thanh toán bằng L/C',
}


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
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def ensure_import_payment_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    extras = [
        ('payment_mode', "TEXT DEFAULT 'unpaid'"),
        ('amount_fc', 'REAL DEFAULT 0'),
        ('advance_fc', 'REAL DEFAULT 0'),
        ('advance_vnd', 'REAL DEFAULT 0'),
        ('linked_lc_id', 'INTEGER'),
    ]
    names = _cols(conn, 'import')
    for col, decl in extras:
        if col not in names:
            try:
                conn.execute(f'ALTER TABLE "import" ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_import_advances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            voucher_id INTEGER NOT NULL,
            amount_fc REAL NOT NULL DEFAULT 0,
            exchange_rate REAL NOT NULL DEFAULT 1,
            amount_vnd REAL NOT NULL DEFAULT 0,
            UNIQUE(import_id, voucher_id)
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_import_advances_import '
        'ON sme_import_advances(import_id)'
    )
    if commit:
        sqlite_commit(conn, label='import_payment')


def normalize_payment_mode(raw, *, import_type: str = 'DOMESTIC') -> str:
    code = (raw or '').strip().lower().replace('-', '_').replace(' ', '_')
    aliases = {
        'unpaid': PAYMENT_UNPAID,
        'credit': PAYMENT_UNPAID,
        'chua_thanh_toan': PAYMENT_UNPAID,
        'prepaid': PAYMENT_PREPAID_FULL,
        'prepaid_full': PAYMENT_PREPAID_FULL,
        'full_advance': PAYMENT_PREPAID_FULL,
        'full_prepaid': PAYMENT_PREPAID_FULL,
        'da_thanh_toan_truoc_du': PAYMENT_PREPAID_FULL,
        'prepaid_partial': PAYMENT_PREPAID_PARTIAL,
        'partial': PAYMENT_PREPAID_PARTIAL,
        'partial_advance': PAYMENT_PREPAID_PARTIAL,
        'advance': PAYMENT_PREPAID_PARTIAL,
        'da_ung_mot_phan': PAYMENT_PREPAID_PARTIAL,
        'lc': PAYMENT_LC,
        'l_c': PAYMENT_LC,
        'letter_of_credit': PAYMENT_LC,
    }
    mode = aliases.get(code, '')
    t = (import_type or 'DOMESTIC').strip().upper()
    if t != 'IMPORT':
        return mode if mode in PAYMENT_MODE_LABELS else PAYMENT_UNPAID
    if mode in PAYMENT_MODE_LABELS:
        return mode
    return PAYMENT_UNPAID


def payment_status_label(mode: str) -> str:
    return PAYMENT_MODE_LABELS.get(mode, 'Chưa thanh toán')


def get_advance_voucher_balance(
    conn: sqlite3.Connection,
    voucher_id: int,
    *,
    exclude_import_id: int | None = None,
) -> dict[str, Any]:
    """Số dư tạm ứng NCC còn dùng cho các đợt chứng từ tiếp theo.

    Công thức (giống L/C):
      face = amount_fc trên PC tạm ứng
      đã dùng = Σ sme_import_advances.amount_fc (trừ phiếu đang sửa)
      còn lại = face − đã dùng
    """
    ensure_import_payment_schema(conn, commit=False)
    vrow = get_voucher_advance(conn, voucher_id)
    if not vrow:
        raise ValueError(f'Không tìm thấy phiếu chi tạm ứng #{voucher_id}')
    face_fc = _money(vrow.get('amount_fc') or 0)
    face_vnd = _money(vrow.get('amount_vnd') or vrow.get('amount') or 0)
    rate = _fx(vrow.get('exchange_rate') or 1)

    sql = """
        SELECT
            COALESCE(SUM(amount_fc), 0) AS used_fc,
            COALESCE(SUM(amount_vnd), 0) AS used_vnd,
            COUNT(*) AS link_count
        FROM sme_import_advances
        WHERE voucher_id = ?
    """
    params: list[Any] = [int(voucher_id)]
    if exclude_import_id:
        sql += ' AND import_id != ?'
        params.append(int(exclude_import_id))
    used = conn.execute(sql, params).fetchone()
    used_fc = _money(used[0] if not isinstance(used, sqlite3.Row) else used['used_fc'])
    used_vnd = _money(used[1] if not isinstance(used, sqlite3.Row) else used['used_vnd'])
    link_count = int(used[2] if not isinstance(used, sqlite3.Row) else used['link_count'] or 0)

    remain_fc = _money(face_fc - used_fc)
    remain_vnd = _money(face_vnd - used_vnd)
    if remain_fc < 0:
        remain_fc = Decimal('0.00')
    if remain_vnd < 0:
        # suy từ FC còn lại nếu VND lệch
        remain_vnd = _money(remain_fc * rate) if remain_fc > 0 else Decimal('0.00')

    shipments = []
    try:
        ship_sql = """
            SELECT a.import_id, a.amount_fc, a.amount_vnd, a.exchange_rate,
                   i.import_no, i.date AS import_date
            FROM sme_import_advances a
            LEFT JOIN "import" i ON i.id = a.import_id
            WHERE a.voucher_id = ?
        """
        ship_params: list[Any] = [int(voucher_id)]
        if exclude_import_id:
            ship_sql += ' AND a.import_id != ?'
            ship_params.append(int(exclude_import_id))
        ship_sql += ' ORDER BY a.id'
        shipments = [dict(r) for r in conn.execute(ship_sql, ship_params).fetchall()]
    except sqlite3.OperationalError:
        shipments = []

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
        'link_count': link_count,
        'shipments': shipments,
        'can_link_import': remain_fc > Decimal('0.00005'),
        **{k: vrow.get(k) for k in (
            'id', 'amount_fc', 'amount_vnd', 'amount', 'purpose', 'status', 'debit_account',
        )},
        # amount_* trên response = số còn lại để form mặc định dùng đúng
        'amount_fc': float(remain_fc if remain_fc > 0 else face_fc),
        'amount_vnd': float(remain_vnd if remain_vnd > 0 else face_vnd),
        'amount': float(remain_vnd if remain_vnd > 0 else face_vnd),
    }


def enrich_advance_with_balance(
    conn: sqlite3.Connection,
    raw: dict[str, Any],
    *,
    exclude_import_id: int | None = None,
) -> dict[str, Any]:
    vid = int(raw.get('id') or raw.get('voucher_id') or 0)
    if not vid:
        return normalize_advance_amounts(raw)
    try:
        bal = get_advance_voucher_balance(
            conn, vid, exclude_import_id=exclude_import_id,
        )
    except ValueError:
        return normalize_advance_amounts(raw)
    out = normalize_advance_amounts(raw)
    out.update({
        'face_fc': bal['face_fc'],
        'face_vnd': bal['face_vnd'],
        'used_fc': bal['used_fc'],
        'used_vnd': bal['used_vnd'],
        'remaining_fc': bal['remaining_fc'],
        'remaining_vnd': bal['remaining_vnd'],
        'link_count': bal['link_count'],
        'can_link_import': bal['can_link_import'],
        'shipments': bal.get('shipments') or [],
        # mặc định form lấy số còn lại
        'amount_fc': bal['remaining_fc'],
        'amount_vnd': bal['remaining_vnd'],
        'amount': bal['remaining_vnd'],
        'exchange_rate': bal['exchange_rate'],
    })
    return out


def list_supplier_advances(
    conn: sqlite3.Connection,
    *,
    supplier_name: str | None = None,
    currency: str | None = None,
    unused_only: bool = True,
    include_import_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """PC tạm ứng NCC còn số dư (hoặc đang gắn phiếu đang sửa)."""
    ensure_import_payment_schema(conn, commit=False)
    from Services.sme.vouchers import ensure_sme_voucher_schema
    ensure_sme_voucher_schema(conn, commit=False)

    cols = _cols(conn, 'sme_vouchers')
    if 'purpose' not in cols:
        return []

    sql = """
        SELECT v.id, v.voucher_no, v.voucher_date, v.party_name, v.amount,
               COALESCE(v.amount_fc, 0) AS amount_fc,
               COALESCE(v.exchange_rate, 1) AS exchange_rate,
               COALESCE(v.currency, 'VND') AS currency,
               v.debit_account, v.credit_account, v.reason, v.status, v.purpose
        FROM sme_vouchers v
        WHERE v.voucher_type = 'payment'
          AND v.status = 'posted'
          AND COALESCE(v.purpose, '') = 'supplier_advance'
          AND v.debit_account LIKE '331%'
    """
    params: list[Any] = []
    name = (supplier_name or '').strip()
    if name:
        sql += ' AND COALESCE(v.party_name, "") LIKE ?'
        params.append(f'%{name}%')
    cur = (currency or '').strip().upper()
    if cur and 'currency' in cols:
        sql += ' AND UPPER(COALESCE(v.currency, "VND")) = ?'
        params.append(cur)
    sql += ' ORDER BY date(v.voucher_date) DESC, v.id DESC LIMIT ?'
    params.append(int(limit) * 3 if unused_only else int(limit))
    rows = conn.execute(sql, params).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        enriched = enrich_advance_with_balance(
            conn, dict(r), exclude_import_id=include_import_id,
        )
        if unused_only:
            on_this = False
            if include_import_id:
                link = conn.execute(
                    'SELECT amount_fc, amount_vnd, exchange_rate '
                    'FROM sme_import_advances WHERE voucher_id = ? AND import_id = ?',
                    (int(enriched['id']), int(include_import_id)),
                ).fetchone()
                on_this = bool(link)
                if on_this:
                    tr = dict(link)
                    # Số khả dụng khi sửa = còn lại (đã exclude phiếu này) + phần đang gắn
                    avail_fc = _money(enriched.get('remaining_fc')) + _money(tr.get('amount_fc'))
                    avail_vnd = _money(enriched.get('remaining_vnd')) + _money(tr.get('amount_vnd'))
                    enriched['amount_fc'] = float(avail_fc)
                    enriched['amount_vnd'] = float(avail_vnd)
                    enriched['amount'] = float(avail_vnd)
                    enriched['remaining_fc'] = float(avail_fc)
                    enriched['remaining_vnd'] = float(avail_vnd)
                    enriched['can_link_import'] = avail_fc > Decimal('0.00005')
            if not enriched.get('can_link_import') and not on_this:
                continue
        out.append(enriched)
        if len(out) >= int(limit):
            break
    return out


def get_voucher_advance(conn: sqlite3.Connection, voucher_id: int) -> dict[str, Any] | None:
    ensure_import_payment_schema(conn, commit=False)
    row = conn.execute(
        """
        SELECT id, voucher_no, voucher_date, party_name, amount,
               COALESCE(amount_fc, 0) AS amount_fc,
               COALESCE(exchange_rate, 1) AS exchange_rate,
               COALESCE(currency, 'VND') AS currency,
               purpose, status, debit_account
        FROM sme_vouchers WHERE id = ?
        """,
        (voucher_id,),
    ).fetchone()
    if not row:
        return None
    return normalize_advance_amounts(dict(row))


def normalize_advance_amounts(raw: dict[str, Any], *, fallback_rate=None) -> dict[str, Any]:
    """Chuẩn hoá amount_fc / exchange_rate / amount_vnd từ PC tạm ứng."""
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


def build_advance_payloads_from_request(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    exchange_rate,
    exclude_import_id: int | None = None,
) -> list[dict[str, Any]]:
    """Ưu tiên mảng ``advances`` (có số tiền) từ form; fallback lookup PC theo id.

    Kiểm tra không vượt **số dư còn lại** của PC (đã trừ các đợt chứng từ khác).
    """
    customs_rate = _fx(exchange_rate)
    payloads: list[dict[str, Any]] = []
    seen: set[int] = set()

    # edit_id từ payload nếu chưa truyền
    if exclude_import_id is None:
        for key in ('import_id', 'edit_id', 'id'):
            raw = data.get(key)
            if raw not in (None, '', 0, '0'):
                try:
                    exclude_import_id = int(raw)
                    break
                except (TypeError, ValueError):
                    pass

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
        vrow = get_voucher_advance(conn, vid)
        if not vrow:
            raise ValueError(f'Không tìm thấy phiếu chi tạm ứng #{vid}')
        if (vrow.get('purpose') or '') != 'supplier_advance':
            raise ValueError(f'PC #{vid} không phải tạm ứng NCC')
        bal = get_advance_voucher_balance(
            conn, vid, exclude_import_id=exclude_import_id,
        )
        max_fc = _money(bal.get('remaining_fc') or 0)
        # Client gửi số dùng để hạch toán (có thể < số dư khi ứng một phần / tách đợt)
        merged = normalize_advance_amounts(
            {
                **vrow,
                'amount_fc': item.get('amount_fc', bal.get('remaining_fc')),
                'exchange_rate': item.get('exchange_rate', vrow.get('exchange_rate')),
                'amount_vnd': item.get('amount_vnd'),
                'amount': item.get('amount_vnd') or item.get('amount') or bal.get('remaining_vnd'),
            },
            fallback_rate=customs_rate,
        )
        use_fc = _money(merged.get('amount_fc') or 0)
        if use_fc <= 0:
            raise ValueError(
                f'Tạm ứng PC {vrow.get("voucher_no") or vid} hết số dư '
                f'(đã dùng {bal["used_fc"]} / {bal["face_fc"]} NT)'
            )
        if max_fc >= 0 and use_fc > max_fc + Decimal('0.0001'):
            raise ValueError(
                f'Tạm ứng PC {vrow.get("voucher_no") or vid} '
                f'({float(use_fc):g} NT) vượt số dư còn lại ({float(max_fc):g} NT). '
                f'Mệnh giá PC {bal["face_fc"]} — đã dùng cho đợt khác {bal["used_fc"]}.'
            )
        payloads.append({
            'voucher_id': vid,
            'amount_fc': float(use_fc),
            'exchange_rate': float(merged['exchange_rate']),
            'amount_vnd': float(merged['amount_vnd']),
            'amount': float(merged['amount_vnd']),
        })
        seen.add(vid)

    # Fallback: chỉ gửi danh sách id → lấy toàn bộ số dư còn lại
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
        vrow = get_voucher_advance(conn, vid)
        if not vrow:
            raise ValueError(f'Không tìm thấy phiếu chi tạm ứng #{vid}')
        if (vrow.get('purpose') or '') != 'supplier_advance':
            raise ValueError(f'PC #{vid} không phải tạm ứng NCC')
        bal = get_advance_voucher_balance(
            conn, vid, exclude_import_id=exclude_import_id,
        )
        use_fc = _money(bal.get('remaining_fc') or 0)
        if use_fc <= 0:
            raise ValueError(
                f'PC {vrow.get("voucher_no") or vid} không còn số dư tạm ứng'
            )
        use_vnd = _money(bal.get('remaining_vnd') or 0)
        rate = _fx(bal.get('exchange_rate') or vrow.get('exchange_rate') or 1)
        if use_vnd <= 0:
            use_vnd = _money(use_fc * rate)
        payloads.append({
            'voucher_id': vid,
            'amount_fc': float(use_fc),
            'exchange_rate': float(rate),
            'amount_vnd': float(use_vnd),
            'amount': float(use_vnd),
        })
        seen.add(vid)

    return payloads


def compute_split_fx_goods_vnd(
    *,
    total_fc,
    customs_rate,
    advances: list[dict],
) -> dict[str, Any]:
    """Tính nguyên giá hàng (chưa thuế HQ) theo tỷ giá tách phần ứng / còn lại."""
    total = _money(total_fc)
    rate = _fx(customs_rate)
    if total < 0:
        total = Decimal('0.00')

    adv_fc = Decimal('0.00')
    adv_vnd = Decimal('0.00')
    detail = []
    for a in advances or []:
        ar = _fx(a.get('exchange_rate') or rate)
        fc = _money(a.get('amount_fc') or 0)
        vnd = _money(a.get('amount_vnd') or 0)
        if fc <= 0 and a.get('amount'):
            fc = _money(_money(a.get('amount')) / ar) if ar > 0 else Decimal('0.00')
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
    goods_vnd = _money(adv_vnd + remain_vnd)
    customs_only_vnd = _money(total * rate)

    return {
        'total_fc': float(total),
        'customs_rate': float(rate),
        'advance_fc': float(adv_fc),
        'advance_vnd': float(adv_vnd),
        'remain_fc': float(remain_fc),
        'remain_vnd': float(remain_vnd),
        'goods_vnd': float(goods_vnd),
        'customs_only_vnd': float(customs_only_vnd),
        'fx_adjustment': float(_money(goods_vnd - customs_only_vnd)),
        'advances': detail,
    }


def replace_import_advances(
    conn: sqlite3.Connection,
    import_id: int,
    advances: list[dict],
    *,
    commit: bool = False,
) -> list[dict[str, Any]]:
    ensure_import_payment_schema(conn, commit=False)
    conn.execute('DELETE FROM sme_import_advances WHERE import_id = ?', (import_id,))
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
            INSERT INTO sme_import_advances
            (import_id, voucher_id, amount_fc, exchange_rate, amount_vnd)
            VALUES (?,?,?,?,?)
            """,
            (import_id, int(vid), float(fc), float(rate), float(vnd)),
        )
        saved.append({
            'voucher_id': int(vid),
            'amount_fc': float(fc),
            'exchange_rate': float(rate),
            'amount_vnd': float(vnd),
        })
    if commit:
        sqlite_commit(conn, label='import_payment')
    return saved


def list_import_advances(conn: sqlite3.Connection, import_id: int) -> list[dict[str, Any]]:
    ensure_import_payment_schema(conn, commit=False)
    rows = conn.execute(
        """
        SELECT a.*, v.voucher_no, v.voucher_date, v.party_name
        FROM sme_import_advances a
        LEFT JOIN sme_vouchers v ON v.id = a.voucher_id
        WHERE a.import_id = ?
        ORDER BY a.id
        """,
        (import_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def validate_import_payment(
    *,
    payment_mode: str,
    total_fc,
    advances: list[dict],
    lc_id: int | None,
) -> None:
    mode = normalize_payment_mode(payment_mode, import_type='IMPORT')
    total = _money(total_fc)
    adv_fc = sum((_money(a.get('amount_fc') or 0) for a in (advances or [])), Decimal('0.00'))

    if mode == PAYMENT_UNPAID:
        return
    if mode in (PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL):
        if not advances:
            raise ValueError('Chọn ít nhất một phiếu chi tạm ứng NCC')
        if mode == PAYMENT_PREPAID_FULL and adv_fc + Decimal('0.0001') < total:
            raise ValueError(
                f'Tạm ứng ({float(adv_fc):g} NT) chưa đủ giá trị hàng đợt này ({float(total):g} NT). '
                f'Chọn thêm PC / tăng số dùng, hoặc đổi sang «Đã ứng một phần».'
            )
        if mode == PAYMENT_PREPAID_PARTIAL and adv_fc <= 0:
            raise ValueError('Số tạm ứng phải > 0')
        if mode == PAYMENT_PREPAID_PARTIAL and adv_fc >= total:
            raise ValueError('Ứng đủ 100% đợt này — hãy chọn «Đã thanh toán trước đủ»')
        return
    if mode == PAYMENT_LC:
        if not lc_id:
            raise ValueError('Chọn thư tín dụng (L/C) đã mở')
        return
