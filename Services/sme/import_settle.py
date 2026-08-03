"""Quyết toán công nợ NCC nhập khẩu + tất toán L/C (TT99).

1) Trả phần còn lại 331 bằng ngoại tệ:
   Nợ 331 (giá trị sổ) · Nợ 635 / Có 515 (CLTG) · Có 1122|1112 (tiền theo tỷ giá TT)

2) Tất toán L/C:
   Nợ 331 · Có 244 (+ Có 1122 nếu ký quỹ thiếu) · CLTG nếu có
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    resolve_postable_account,
)

MONEY_Q = Decimal('0.01')
FX_Q = Decimal('0.0001')

DOC_TYPE_SETTLE_AP = 'TTNCC'   # Thanh toán NCC NK
DOC_TYPE_SETTLE_LC = 'TTLC'   # Tất toán L/C


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _fx(val) -> Decimal:
    rate = Decimal(str(val or 1))
    if rate <= 0:
        return Decimal('1')
    return rate.quantize(FX_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def ensure_import_settle_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    from Services.sme.import_payment import ensure_import_payment_schema
    ensure_import_payment_schema(conn, commit=False)

    extras = [
        ('settle_journal_id', 'INTEGER'),
        ('settle_voucher_id', 'INTEGER'),
        ('settle_date', 'TEXT'),
        ('settle_fx_rate', 'REAL'),
        ('settle_amount_fc', 'REAL DEFAULT 0'),
        ('settle_fx_gain', 'REAL DEFAULT 0'),
        ('settle_fx_loss', 'REAL DEFAULT 0'),
    ]
    names = _cols(conn, 'import')
    for col, decl in extras:
        if col not in names:
            try:
                conn.execute(f'ALTER TABLE "import" ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass

    # L/C: lưu bút toán tất toán
    ensure_sme_lc = True
    try:
        from Services.sme.letter_of_credit import ensure_sme_lc_schema
        ensure_sme_lc_schema(conn, commit=False)
    except Exception:
        ensure_sme_lc = False
    if ensure_sme_lc:
        lc_cols = _cols(conn, 'sme_lc_docs')
        for col, decl in (
            ('settle_journal_id', 'INTEGER'),
            ('settle_date', 'TEXT'),
            ('settled_import_id', 'INTEGER'),
        ):
            if col not in lc_cols:
                try:
                    conn.execute(f'ALTER TABLE sme_lc_docs ADD COLUMN {col} {decl}')
                except sqlite3.OperationalError:
                    pass
    if commit:
        conn.commit()


def _supplier_name(conn: sqlite3.Connection, supplier_id) -> str:
    if not supplier_id:
        return 'NCC'
    row = conn.execute(
        'SELECT name FROM suppliers WHERE id = ?', (supplier_id,)
    ).fetchone()
    if not row:
        return f'NCC #{supplier_id}'
    return row[0] if not isinstance(row, sqlite3.Row) else (row['name'] or f'NCC #{supplier_id}')


def get_import_ap_summary(conn: sqlite3.Connection, import_id: int) -> dict[str, Any]:
    """Tóm tắt công nợ 331 còn lại (CIF, không gồm thuế HQ)."""
    ensure_import_settle_schema(conn, commit=False)
    from Services.sme.import_payment import (
        PAYMENT_LC,
        PAYMENT_PREPAID_FULL,
        PAYMENT_PREPAID_PARTIAL,
        PAYMENT_UNPAID,
        compute_split_fx_goods_vnd,
        list_import_advances,
        normalize_payment_mode,
    )

    row = conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone()
    if not row:
        raise ValueError('Không tìm thấy phiếu nhập')
    imp = dict(row)
    itype = str(imp.get('import_type') or 'DOMESTIC').upper()
    mode = normalize_payment_mode(imp.get('payment_mode'), import_type=itype)
    currency = (imp.get('currency') or ('USD' if itype == 'IMPORT' else 'VND')).strip().upper()
    customs_rate = _fx(imp.get('customs_fx_rate') or imp.get('exchange_rate') or 1)

    amount_fc = _money(imp.get('amount_fc'))
    if amount_fc <= 0 and itype == 'IMPORT':
        # Suy từ dòng: qty × buyprice (FC) sau CK
        details = conn.execute(
            """
            SELECT COALESCE(qty,0) AS qty, COALESCE(buyprice,0) AS buyprice,
                   COALESCE(discount_pct,0) AS discount_pct
            FROM import_details WHERE import_id = ?
            """,
            (import_id,),
        ).fetchall()
        for d in details:
            dd = dict(d)
            line = _money(dd.get('qty')) * _money(dd.get('buyprice'))
            disc = Decimal(str(dd.get('discount_pct') or 0))
            amount_fc += line - _money(line * (disc / Decimal('100')))

    advances = list_import_advances(conn, import_id)
    if not advances and _money(imp.get('advance_fc')) > 0:
        advances = [{
            'amount_fc': float(imp.get('advance_fc') or 0),
            'exchange_rate': float(imp.get('exchange_rate') or customs_rate),
            'amount_vnd': float(imp.get('advance_vnd') or 0),
        }]

    split = compute_split_fx_goods_vnd(
        total_fc=amount_fc,
        customs_rate=customs_rate,
        advances=advances if mode in (PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL) else [],
    )
    goods_vnd = _money(split['goods_vnd'])
    advance_fc = _money(split['advance_fc'])
    advance_vnd = _money(split['advance_vnd'])
    remain_fc = _money(split['remain_fc'])
    remain_vnd = _money(split['remain_vnd'])

    # Đã trả thêm phần còn lại (sau tạm ứng) — trừ settle_amount_fc
    settled_fc = _money(imp.get('settle_amount_fc'))
    settled_full = bool(imp.get('settle_journal_id'))
    if mode == PAYMENT_LC:
        remain_fc = amount_fc
        remain_vnd = goods_vnd
        if settled_full:
            remain_fc = Decimal('0.00')
            remain_vnd = Decimal('0.00')
    elif mode == PAYMENT_PREPAID_FULL or settled_full:
        remain_fc = Decimal('0.00')
        remain_vnd = Decimal('0.00')
    elif settled_fc > 0 and remain_fc > 0:
        ratio = min(Decimal('1'), settled_fc / remain_fc) if remain_fc else Decimal('1')
        remain_fc = _money(remain_fc - settled_fc)
        if remain_fc < 0:
            remain_fc = Decimal('0.00')
        remain_vnd = _money(remain_vnd * (Decimal('1') - ratio)) if settled_fc > 0 else remain_vnd
        # chính xác hơn: remain_vnd = remain_fc * customs_rate
        remain_vnd = _money(remain_fc * customs_rate)

    paid_amount = _money(imp.get('paid_amount'))
    linked_lc_id = imp.get('linked_lc_id')
    lc_balance = None
    if linked_lc_id:
        try:
            from Services.sme.letter_of_credit import get_lc_balance
            lc_balance = get_lc_balance(conn, int(linked_lc_id))
        except Exception:
            lc_balance = None

    can_settle_lc = (
        itype == 'IMPORT'
        and mode == PAYMENT_LC
        and bool(linked_lc_id)
        and remain_vnd > 0
        and not settled_full
        and (
            lc_balance is None
            or float(lc_balance.get('remaining_fc') or 0) > 0
            or float(lc_balance.get('remaining_244') or 0) > 0
        )
    )
    return {
        'import_id': import_id,
        'import_no': imp.get('import_no'),
        'import_type': itype,
        'payment_mode': mode,
        'currency': currency,
        'customs_rate': float(customs_rate),
        'amount_fc': float(amount_fc),
        'advance_fc': float(advance_fc),
        'advance_vnd': float(advance_vnd),
        'goods_vnd': float(goods_vnd),
        'remain_fc': float(remain_fc),
        'remain_vnd': float(remain_vnd),
        'paid_amount': float(paid_amount),
        'linked_lc_id': linked_lc_id,
        'lc_balance': lc_balance,
        'settled': settled_full or remain_fc <= 0,
        'settle_journal_id': imp.get('settle_journal_id'),
        'supplier_id': imp.get('supplier_id'),
        'supplier_name': _supplier_name(conn, imp.get('supplier_id')),
        'can_settle_ap': (
            itype == 'IMPORT'
            and mode in (PAYMENT_UNPAID, PAYMENT_PREPAID_PARTIAL)
            and remain_fc > 0
            and not settled_full
        ),
        'can_settle_lc': can_settle_lc,
    }


def _insert_settle_voucher(
    conn: sqlite3.Connection,
    *,
    voucher_date: str,
    party_name: str,
    amount_vnd,
    amount_fc,
    currency: str,
    exchange_rate,
    debit_account: str,
    credit_account: str,
    reason: str,
    import_id: int,
    journal_entry_id: int,
    purpose: str,
    created_by: str | None,
    branch_code: str | None,
) -> int:
    from Services.sme.vouchers import ensure_sme_voucher_schema, _next_voucher_no

    ensure_sme_voucher_schema(conn, commit=False)
    cols = _cols(conn, 'sme_vouchers')
    vno = _next_voucher_no(conn, 'payment')
    base_cols = [
        'voucher_type', 'form_code', 'voucher_no', 'voucher_date',
        'party_name', 'amount', 'debit_account', 'credit_account', 'reason',
        'source_type', 'source_id', 'journal_entry_id', 'status',
        'created_by', 'created_at', 'updated_at', 'branch_code',
    ]
    base_vals: list[Any] = [
        'payment', '02-TT', vno, voucher_date[:10],
        party_name, float(_money(amount_vnd)), debit_account, credit_account, reason,
        'import_settle', import_id, journal_entry_id, 'posted',
        created_by, _now(), _now(), branch_code,
    ]
    if 'currency' in cols:
        base_cols.extend(['currency', 'exchange_rate', 'amount_fc'])
        base_vals.extend([currency, float(_fx(exchange_rate)), float(_money(amount_fc))])
    if 'purpose' in cols:
        base_cols.append('purpose')
        base_vals.append(purpose)
    placeholders = ','.join('?' * len(base_cols))
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO sme_vouchers ({', '.join(base_cols)}) VALUES ({placeholders})",
        base_vals,
    )
    return int(cur.lastrowid)


def settle_import_supplier_ap(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    settle_date: str | None = None,
    amount_fc=None,
    exchange_rate=None,
    payment_method: str = 'bank_fx',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Trả phần còn lại công nợ NCC NK — CLTG 635/515."""
    from Services.sme.branches import resolve_posting_branch
    from Services.sme.vouchers import _cash_account

    ensure_sme_journal_ready(conn, commit=False)
    ensure_import_settle_schema(conn, commit=False)

    summary = get_import_ap_summary(conn, import_id)
    if not summary['can_settle_ap']:
        if summary['settled']:
            raise ValueError('Phiếu này đã quyết toán công nợ NCC')
        if summary['payment_mode'] == 'lc':
            raise ValueError('Phiếu thanh toán bằng L/C — dùng tất toán L/C')
        if summary['payment_mode'] == 'prepaid_full':
            raise ValueError('Đã ứng đủ — không còn phần phải trả')
        raise ValueError('Không còn công nợ NCC cần quyết toán')

    remain_fc = _money(summary['remain_fc'])
    remain_vnd = _money(summary['remain_vnd'])
    pay_fc = _money(amount_fc if amount_fc is not None else remain_fc)
    if pay_fc <= 0:
        raise ValueError('Số ngoại tệ thanh toán phải > 0')
    if pay_fc - remain_fc > Decimal('0.0001'):
        raise ValueError(
            f'Số trả ({float(pay_fc):g}) vượt phần còn lại ({float(remain_fc):g} {summary["currency"]})'
        )

    pay_rate = _fx(exchange_rate if exchange_rate is not None else summary['customs_rate'])
    # Phần sổ tương ứng tỷ lệ FC còn lại
    book_clear = _money(remain_vnd * (pay_fc / remain_fc)) if remain_fc > 0 else Decimal('0.00')
    cash_vnd = _money(pay_fc * pay_rate)
    fx_diff = _money(cash_vnd - book_clear)
    fx_loss = fx_diff if fx_diff > 0 else Decimal('0.00')
    fx_gain = (-fx_diff) if fx_diff < 0 else Decimal('0.00')

    date_s = str(settle_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    currency = summary['currency'] if summary['currency'] != 'VND' else 'USD'
    cash_acc = resolve_postable_account(
        conn, _cash_account(payment_method, currency=currency)
    )
    ap_acc = resolve_postable_account(conn, '331')
    branch = resolve_posting_branch(conn, None)
    supplier_id = summary.get('supplier_id')
    party = summary.get('supplier_name') or 'NCC'
    desc = (
        f'Thanh toán NCC NK {summary.get("import_no") or import_id} '
        f'({float(pay_fc):g} {currency} × {float(pay_rate):g})'
    )

    lines = [
        {
            'sequence': 1,
            'account_code': ap_acc,
            'debit': float(book_clear),
            'credit': 0,
            'debit_fc': float(pay_fc),
            'credit_fc': 0,
            'currency': currency,
            'exchange_rate': float(summary['customs_rate']),
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'description': desc,
        },
    ]
    seq = 2
    if fx_loss > 0:
        lines.append({
            'sequence': seq,
            'account_code': resolve_postable_account(conn, '635'),
            'debit': float(fx_loss),
            'credit': 0,
            'description': f'Lỗ chênh lệch tỷ giá thanh toán NCC — {summary.get("import_no")}',
        })
        seq += 1
    lines.append({
        'sequence': seq,
        'account_code': cash_acc,
        'debit': 0,
        'credit': float(cash_vnd),
        'debit_fc': 0,
        'credit_fc': float(pay_fc),
        'currency': currency,
        'exchange_rate': float(pay_rate),
        'description': desc,
    })
    seq += 1
    if fx_gain > 0:
        lines.append({
            'sequence': seq,
            'account_code': resolve_postable_account(conn, '515'),
            'debit': 0,
            'credit': float(fx_gain),
            'description': f'Lãi chênh lệch tỷ giá thanh toán NCC — {summary.get("import_no")}',
        })

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_type=DOC_TYPE_SETTLE_AP,
        document_no=str(summary.get('import_no') or import_id),
        document_id=import_id,
        business_type='THANH_TOAN_NCC_NK',
        currency=currency,
        exchange_rate=float(pay_rate),
        description=desc,
        lines=lines,
        created_by=created_by,
        branch_code=branch,
    )

    voucher_id = _insert_settle_voucher(
        conn,
        voucher_date=date_s,
        party_name=party,
        amount_vnd=cash_vnd,
        amount_fc=pay_fc,
        currency=currency,
        exchange_rate=pay_rate,
        debit_account=ap_acc,
        credit_account=cash_acc,
        reason=desc,
        import_id=import_id,
        journal_entry_id=entry['id'],
        purpose='settle_import_ap',
        created_by=created_by,
        branch_code=branch,
    )

    fully = pay_fc >= remain_fc - Decimal('0.0001')
    new_paid = _money(summary['paid_amount']) + book_clear
    prev_fc = _money(
        conn.execute(
            'SELECT COALESCE(settle_amount_fc, 0) FROM "import" WHERE id = ?',
            (import_id,),
        ).fetchone()[0]
    )
    prev_g = _money(
        conn.execute(
            'SELECT COALESCE(settle_fx_gain, 0) FROM "import" WHERE id = ?',
            (import_id,),
        ).fetchone()[0]
    )
    prev_l = _money(
        conn.execute(
            'SELECT COALESCE(settle_fx_loss, 0) FROM "import" WHERE id = ?',
            (import_id,),
        ).fetchone()[0]
    )

    cols = _cols(conn, 'import')
    sets = ['paid_amount = ?', 'settle_voucher_id = ?']
    vals: list[Any] = [float(new_paid), voucher_id]
    if 'settle_fx_rate' in cols:
        sets.append('settle_fx_rate = ?')
        vals.append(float(pay_rate))
    if 'settle_amount_fc' in cols:
        sets.append('settle_amount_fc = ?')
        vals.append(float(prev_fc + pay_fc))
    if 'settle_fx_gain' in cols:
        sets.append('settle_fx_gain = ?')
        vals.append(float(prev_g + fx_gain))
    if 'settle_fx_loss' in cols:
        sets.append('settle_fx_loss = ?')
        vals.append(float(prev_l + fx_loss))
    if fully:
        if 'settle_journal_id' in cols:
            sets.append('settle_journal_id = ?')
            vals.append(entry['id'])
        if 'settle_date' in cols:
            sets.append('settle_date = ?')
            vals.append(date_s)
        if 'payment_status' in cols:
            sets.append('payment_status = ?')
            vals.append('Đã thanh toán')
    vals.append(import_id)
    conn.execute(f'UPDATE "import" SET {", ".join(sets)} WHERE id = ?', vals)

    if commit:
        conn.commit()

    return {
        'import_id': import_id,
        'fully_settled': fully,
        'voucher_id': voucher_id,
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'amount_fc': float(pay_fc),
        'book_vnd': float(book_clear),
        'cash_vnd': float(cash_vnd),
        'exchange_rate': float(pay_rate),
        'fx_gain': float(fx_gain),
        'fx_loss': float(fx_loss),
        'currency': currency,
    }


def settle_import_by_lc(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    settle_date: str | None = None,
    shortfall_exchange_rate=None,
    payment_method: str = 'bank_fx',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Tất toán một đợt chứng từ (phiếu nhập) bằng L/C: Nợ 331 / Có 244 (+ Có 1122 thiếu).

    Một L/C có thể chi nhiều đợt. Chỉ giải toả phần còn dư:
      use_fc = min(AP_fc, remaining_fc_LC)
      use_244 = min(AP_vnd, remaining_244, use_fc × tỷ_giá_mở_LC)
    """
    from Services.sme.branches import resolve_posting_branch
    from Services.sme.letter_of_credit import (
        get_lc,
        get_lc_balance,
        record_lc_settlement,
        refresh_lc_status_from_balance,
    )
    from Services.sme.vouchers import _cash_account

    ensure_sme_journal_ready(conn, commit=False)
    ensure_import_settle_schema(conn, commit=False)

    summary = get_import_ap_summary(conn, import_id)
    if not summary['can_settle_lc']:
        if summary['settled']:
            raise ValueError('Đã tất toán L/C cho phiếu này')
        raise ValueError('Phiếu không đủ điều kiện tất toán L/C')

    lc_id = int(summary['linked_lc_id'])
    lc = get_lc(conn, lc_id)
    if not lc:
        raise ValueError('Không tìm thấy L/C')
    if lc.get('status') == 'void':
        raise ValueError('L/C đã hủy')

    bal = get_lc_balance(conn, lc_id)
    remain_fc_lc = _money(bal['remaining_fc'])
    remain_244 = _money(bal['remaining_244'])
    if remain_fc_lc <= 0 and remain_244 <= 0:
        raise ValueError(
            f'L/C {lc.get("lc_no")} đã hết số dư '
            f'(đã dùng {bal["used_fc"]} / {bal["face_fc"]} NT)'
        )

    ap_vnd = _money(summary['remain_vnd'])
    ap_fc = _money(summary['remain_fc'])
    lc_rate = _fx(lc.get('exchange_rate') or summary['customs_rate'])
    margin_acc = resolve_postable_account(conn, lc.get('margin_account') or '244')
    ap_acc = resolve_postable_account(conn, '331')

    # Phần L/C phủ được cho đợt này
    use_fc = min(ap_fc, remain_fc_lc)
    use_244_by_fc = _money(use_fc * lc_rate)
    use_244 = min(remain_244, ap_vnd, use_244_by_fc)
    if use_244 <= 0 and use_fc <= 0:
        raise ValueError('Không còn số dư L/C để chi đợt chứng từ này')

    # Nếu còn AP nhưng L/C hết phần phủ → bù tiền
    shortfall = _money(ap_vnd - use_244)
    fx_gain = Decimal('0.00')
    fx_loss = Decimal('0.00')
    cash_vnd = Decimal('0.00')
    cash_fc = Decimal('0.00')
    pay_rate = _fx(shortfall_exchange_rate or lc.get('exchange_rate') or summary['customs_rate'])
    cash_acc = resolve_postable_account(
        conn, _cash_account(payment_method, currency='USD')
    )

    date_s = str(settle_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    currency = (lc.get('currency') or summary['currency'] or 'USD').strip().upper()
    branch = resolve_posting_branch(conn, None)
    supplier_id = summary.get('supplier_id')
    desc = (
        f'Tất toán L/C {lc.get("lc_no")} — PN {summary.get("import_no") or import_id}'
        f' (đợt {int(bal["settle_count"]) + 1}; còn lại trước: '
        f'{float(remain_fc_lc):g} NT / {float(remain_244):,.0f}₫)'
    )

    lines: list[dict] = [
        {
            'sequence': 1,
            'account_code': ap_acc,
            'debit': float(ap_vnd),
            'credit': 0,
            'debit_fc': float(ap_fc),
            'currency': currency,
            'exchange_rate': float(summary['customs_rate']),
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'description': desc,
        },
    ]
    seq = 2
    if use_244 > 0:
        lines.append({
            'sequence': seq,
            'account_code': margin_acc,
            'debit': 0,
            'credit': float(use_244),
            'credit_fc': float(use_fc),
            'currency': currency,
            'exchange_rate': float(lc_rate),
            'description': (
                f'Giải toả ký quỹ L/C {lc.get("lc_no")} '
                f'({float(use_fc):g} NT × {float(lc_rate):g})'
            ),
        })
        seq += 1
    if shortfall > 0:
        remain_fc_short = _money(max(Decimal('0'), ap_fc - use_fc))
        if remain_fc_short > 0 and shortfall_exchange_rate is not None:
            cash_fc = remain_fc_short
            cash_vnd = _money(cash_fc * pay_rate)
            fx_diff = _money(cash_vnd - shortfall)
            if fx_diff > 0:
                fx_loss = fx_diff
            elif fx_diff < 0:
                fx_gain = -fx_diff
        else:
            cash_vnd = shortfall
        if fx_loss > 0:
            lines.append({
                'sequence': seq,
                'account_code': resolve_postable_account(conn, '635'),
                'debit': float(fx_loss),
                'credit': 0,
                'description': f'Lỗ CLTG tất toán L/C {lc.get("lc_no")}',
            })
            seq += 1
        lines.append({
            'sequence': seq,
            'account_code': cash_acc,
            'debit': 0,
            'credit': float(cash_vnd),
            'credit_fc': float(cash_fc),
            'currency': currency if cash_fc > 0 else 'VND',
            'exchange_rate': float(pay_rate) if cash_fc > 0 else 1,
            'description': f'Bù thiếu tất toán L/C {lc.get("lc_no")}',
        })
        seq += 1
        if fx_gain > 0:
            lines.append({
                'sequence': seq,
                'account_code': resolve_postable_account(conn, '515'),
                'debit': 0,
                'credit': float(fx_gain),
                'description': f'Lãi CLTG tất toán L/C {lc.get("lc_no")}',
            })

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_type=DOC_TYPE_SETTLE_LC,
        document_no=str(lc.get('lc_no') or import_id),
        document_id=import_id,
        business_type='TAT_TOAN_LC',
        currency=currency,
        exchange_rate=float(lc_rate),
        description=desc,
        lines=lines,
        created_by=created_by,
        branch_code=branch,
    )

    voucher_id = _insert_settle_voucher(
        conn,
        voucher_date=date_s,
        party_name=summary.get('supplier_name') or lc.get('beneficiary_name') or 'NCC',
        amount_vnd=ap_vnd,
        amount_fc=ap_fc,
        currency=currency,
        exchange_rate=lc_rate,
        debit_account=ap_acc,
        credit_account=margin_acc if use_244 > 0 else cash_acc,
        reason=desc,
        import_id=import_id,
        journal_entry_id=entry['id'],
        purpose='settle_import_lc',
        created_by=created_by,
        branch_code=branch,
    )

    cols = _cols(conn, 'import')
    sets = ['paid_amount = COALESCE(total_value, paid_amount, 0)']
    vals: list[Any] = []
    if 'payment_status' in cols:
        sets.append("payment_status = 'Đã thanh toán'")
    if 'settle_journal_id' in cols:
        sets.append('settle_journal_id = ?')
        vals.append(entry['id'])
    if 'settle_voucher_id' in cols:
        sets.append('settle_voucher_id = ?')
        vals.append(voucher_id)
    if 'settle_date' in cols:
        sets.append('settle_date = ?')
        vals.append(date_s)
    if 'settle_fx_rate' in cols:
        sets.append('settle_fx_rate = ?')
        vals.append(float(pay_rate))
    if 'settle_amount_fc' in cols:
        sets.append('settle_amount_fc = ?')
        vals.append(float(ap_fc))
    if 'settle_fx_gain' in cols:
        sets.append('settle_fx_gain = ?')
        vals.append(float(fx_gain))
    if 'settle_fx_loss' in cols:
        sets.append('settle_fx_loss = ?')
        vals.append(float(fx_loss))
    vals.append(import_id)
    conn.execute(f'UPDATE "import" SET {", ".join(sets)} WHERE id = ?', vals)

    record_lc_settlement(
        conn,
        lc_id=lc_id,
        import_id=import_id,
        settle_date=date_s,
        amount_fc=use_fc,
        amount_vnd=ap_vnd,
        released_244=use_244,
        cash_shortfall=cash_vnd if cash_vnd > 0 else (shortfall if shortfall > 0 else 0),
        journal_entry_id=entry['id'],
        voucher_id=voucher_id,
        created_by=created_by,
    )

    # Cập nhật meta LC (không đóng nếu còn dư)
    new_status = refresh_lc_status_from_balance(conn, lc_id, commit=False)
    lc_cols = _cols(conn, 'sme_lc_docs')
    lc_sets = ['updated_at = ?']
    lc_vals: list[Any] = [_now()]
    if 'settle_journal_id' in lc_cols:
        lc_sets.append('settle_journal_id = ?')
        lc_vals.append(entry['id'])
    if 'settle_date' in lc_cols:
        lc_sets.append('settle_date = ?')
        lc_vals.append(date_s)
    if 'settled_import_id' in lc_cols:
        lc_sets.append('settled_import_id = ?')
        lc_vals.append(import_id)
    if 'import_id' in lc_cols:
        lc_sets.append('import_id = COALESCE(import_id, ?)')
        lc_vals.append(import_id)
    lc_vals.append(lc_id)
    conn.execute(f"UPDATE sme_lc_docs SET {', '.join(lc_sets)} WHERE id = ?", lc_vals)

    bal_after = get_lc_balance(conn, lc_id)

    if commit:
        conn.commit()

    return {
        'import_id': import_id,
        'lc_id': lc_id,
        'lc_no': lc.get('lc_no'),
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'voucher_id': voucher_id,
        'ap_vnd': float(ap_vnd),
        'ap_fc': float(ap_fc),
        'used_fc': float(use_fc),
        'released_244': float(use_244),
        'cash_vnd': float(cash_vnd if cash_vnd > 0 else shortfall if shortfall > 0 else 0),
        'fx_gain': float(fx_gain),
        'fx_loss': float(fx_loss),
        'lc_status': new_status,
        'lc_remaining_fc': bal_after['remaining_fc'],
        'lc_remaining_244': bal_after['remaining_244'],
        'lc_used_fc': bal_after['used_fc'],
        'lc_face_fc': bal_after['face_fc'],
    }
