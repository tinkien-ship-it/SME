"""Thu hồi công nợ XK, chiết khấu bộ CT, chi phí xuất khẩu (641)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.export_payment import ensure_export_sale_schema, _money, _fx
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    resolve_postable_account,
)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _journal_posted(conn: sqlite3.Connection, entry_id) -> bool:
    try:
        eid = int(entry_id)
    except (TypeError, ValueError):
        return False
    if eid <= 0:
        return False
    row = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE id = ? AND status = 'posted' AND reverses_id IS NULL
        """,
        (eid,),
    ).fetchone()
    return bool(row)


def settle_export_ar(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    settle_date: str,
    amount_fc=None,
    exchange_rate,
    payment_method: str = 'bank',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Thu tiền XK: tự lập phiếu thu 01-TT (PTxxxxxx) + Nợ 1122/Có 131 + CLTG.

    ``exchange_rate`` = tỷ giá mua NH ngày tiền về.
    ``amount_fc`` mặc định = phần còn phải thu (amount_fc − advance_fc − settled).
    Sổ tiền gửi NH lấy số chứng từ = số phiếu thu (PT000001…).
    """
    from Services.sme.vouchers import (
        VOUCHER_FORM_RECEIPT,
        _next_voucher_no,
        _resolve_cash_gl,
        ensure_sme_voucher_schema,
    )

    ensure_sme_journal_ready(conn, commit=False)
    ensure_export_sale_schema(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)

    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError('Không tìm thấy phiếu bán')
    s = dict(sale)
    if str(s.get('sale_type') or '').upper() != 'EXPORT':
        raise ValueError('Chỉ áp dụng phiếu xuất khẩu')

    # Stale: đã gắn settle_journal_id nhưng bút toán không còn → cho thu lại
    settle_jid = s.get('settle_journal_id')
    if str(s.get('ar_status') or '') == 'settled' and _journal_posted(conn, settle_jid):
        raise ValueError('Phiếu đã tất toán công nợ')
    if settle_jid and not _journal_posted(conn, settle_jid):
        conn.execute(
            """
            UPDATE sale
            SET settle_journal_id = NULL,
                settle_amount_fc = 0,
                ar_status = CASE
                    WHEN COALESCE(ar_status,'') = 'settled' THEN 'open'
                    ELSE ar_status
                END
            WHERE id = ?
            """,
            (sale_id,),
        )
        s = dict(conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone())

    # Phải có bút toán DT (Nợ 131) trước khi thu
    has_ar = conn.execute(
        """
        SELECT 1 FROM sme_journal_entries
        WHERE document_id = ? AND document_type = 'EXPORT_REVENUE'
          AND status = 'posted' AND reverses_id IS NULL
        LIMIT 1
        """,
        (sale_id,),
    ).fetchone()
    if not has_ar:
        # Tự bổ sung DT nếu thiếu (trường hợp thông quan chỉ ghi GV)
        from Services.sme.export_clearance import sync_export_clearance_journals
        try:
            sync_export_clearance_journals(conn, sale_id, created_by=created_by)
        except ValueError as exc:
            raise ValueError(
                'Chưa có bút toán doanh thu (Nợ 131). '
                'Hãy xác nhận thông quan trước khi thu tiền. '
                f'({exc})'
            ) from exc
        has_ar = conn.execute(
            """
            SELECT 1 FROM sme_journal_entries
            WHERE document_id = ? AND document_type = 'EXPORT_REVENUE'
              AND status = 'posted' AND reverses_id IS NULL
            LIMIT 1
            """,
            (sale_id,),
        ).fetchone()
        if not has_ar:
            raise ValueError(
                'Chưa có bút toán doanh thu (Nợ 131). Hãy xác nhận thông quan trước khi thu tiền.'
            )

    currency = (s.get('currency') or 'USD').upper()
    book_rate = _fx(s.get('exchange_rate') or 1)
    total_fc = _money(s.get('amount_fc') or 0)
    adv_fc = _money(s.get('advance_fc') or 0)
    settled_fc = _money(s.get('settle_amount_fc') or 0)
    remain_fc = _money(total_fc - adv_fc - settled_fc)
    if remain_fc <= 0:
        raise ValueError('Không còn số phải thu')

    use_fc = _money(amount_fc if amount_fc is not None else remain_fc)
    if use_fc <= 0:
        raise ValueError('Số NT thu phải > 0')
    if use_fc > remain_fc + Decimal('0.0001'):
        raise ValueError(f'Số thu ({float(use_fc):g}) vượt số còn phải thu ({float(remain_fc):g})')

    bank_rate = _fx(exchange_rate)
    if bank_rate <= 0:
        raise ValueError('Tỷ giá ngày thu phải > 0')

    from Services.sme.export_payment import compute_split_fx_revenue_vnd, list_sale_advances
    advances = list_sale_advances(conn, sale_id)
    split = compute_split_fx_revenue_vnd(
        total_fc=total_fc, revenue_rate=book_rate, advances=advances,
    )
    book_remain_vnd_full = _money(split['remain_vnd'])
    ratio = (use_fc / remain_fc) if remain_fc else Decimal('1')
    book_vnd = _money(book_remain_vnd_full * ratio)
    bank_vnd = _money(use_fc * bank_rate)
    if bank_vnd <= 0 or book_vnd <= 0:
        raise ValueError(
            f'Số tiền thu không hợp lệ (NH={float(bank_vnd)}, sổ={float(book_vnd)}). '
            'Kiểm tra tỷ giá và số NT.'
        )
    diff = _money(bank_vnd - book_vnd)

    pm = str(payment_method or 'bank').lower()
    if currency != 'VND':
        cash_pm = 'bank_fx' if ('cash' not in pm and pm != '111') else 'cash_fx'
    else:
        cash_pm = 'bank' if ('bank' in pm or '112' in pm or 'ck' in pm or 'transfer' in pm) else 'cash'
    cash_acct = _resolve_cash_gl(conn, cash_pm, currency=currency)
    cash_acct = resolve_postable_account(conn, cash_acct)
    ar_acct = resolve_postable_account(conn, '131')
    # Buộc nhóm 1122 khi thu NT qua NH
    if currency != 'VND' and cash_pm == 'bank_fx' and not str(cash_acct).startswith('1122'):
        cash_acct = resolve_postable_account(conn, '1122')

    date_s = str(settle_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày thu tiền')
    sale_no = s.get('sale_no') or f'#{sale_id}'
    customer = (s.get('customer_name') or '').strip()
    desc = f'Thu công nợ {sale_no}'
    if currency != 'VND':
        desc = f'{desc} ({float(use_fc):g} {currency} × {float(bank_rate):g})'

    voucher_no = _next_voucher_no(conn, 'receipt')

    lines = [
        {
            'sequence': 1,
            'account_code': cash_acct,
            'debit': float(bank_vnd),
            'credit': 0,
            'debit_fc': float(use_fc) if currency != 'VND' else 0,
            'credit_fc': 0,
            'description': desc,
        },
        {
            'sequence': 2,
            'account_code': ar_acct,
            'debit': 0,
            'credit': float(book_vnd),
            'debit_fc': 0,
            'credit_fc': float(use_fc) if currency != 'VND' else 0,
            'partner_type': 'customer',
            'description': desc,
        },
    ]
    if diff > Decimal('0.009'):
        lines.append({
            'sequence': 3,
            'account_code': resolve_postable_account(conn, '515'),
            'debit': 0,
            'credit': float(diff),
            'description': f'Lãi CLTG {sale_no}',
        })
    elif diff < Decimal('-0.009'):
        lines.append({
            'sequence': 3,
            'account_code': resolve_postable_account(conn, '635'),
            'debit': float(abs(diff)),
            'credit': 0,
            'description': f'Lỗ CLTG {sale_no}',
        })

    from Services.sme.branch_filter import warehouse_branch_or_session
    branch = warehouse_branch_or_session(conn, s.get('warehouse_code'))

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='PT',
        document_no=voucher_no,
        document_id=sale_id,
        business_type='THU_TIEN',
        currency=currency,
        exchange_rate=float(bank_rate),
        description=desc,
        reference_document=sale_no,
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )
    if not entry or not entry.get('id'):
        raise ValueError('Không ghi được bút toán Nợ 1122 / Có 131')

    # Lưu chứng từ phiếu thu 01-TT — sổ quỹ/NH đọc document_no = PTxxxxxx
    cur_db = conn.cursor()
    vcols = {r[1] for r in cur_db.execute('PRAGMA table_info(sme_vouchers)').fetchall()}
    base_cols = [
        'voucher_type', 'form_code', 'voucher_no', 'voucher_date',
        'party_name', 'party_address', 'party_tax_code', 'amount',
        'debit_account', 'credit_account', 'reason', 'reference_document',
        'source_type', 'source_id', 'journal_entry_id', 'status', 'created_by',
        'created_at', 'updated_at', 'branch_code',
    ]
    base_vals: list[Any] = [
        'receipt', VOUCHER_FORM_RECEIPT, voucher_no, date_s,
        customer, s.get('address') or '', s.get('tax_code') or '', float(bank_vnd),
        cash_acct, ar_acct, desc, sale_no,
        'export_ar_settle', sale_id, entry['id'], 'posted', created_by,
        _now(), _now(), branch,
    ]
    if 'currency' in vcols:
        base_cols.extend(['currency', 'exchange_rate', 'amount_fc'])
        base_vals.extend([currency, float(bank_rate), float(use_fc)])
    if 'purpose' in vcols:
        base_cols.append('purpose')
        base_vals.append('export_ar_settle')
    placeholders = ','.join('?' * len(base_cols))
    cur_db.execute(
        f"INSERT INTO sme_vouchers ({', '.join(base_cols)}) VALUES ({placeholders})",
        base_vals,
    )
    voucher_id = cur_db.lastrowid

    new_settled = settled_fc + use_fc
    still = _money(total_fc - adv_fc - new_settled)
    ar_status = 'settled' if still <= Decimal('0.00005') else (s.get('ar_status') or 'open')
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sale)').fetchall()}
    sets = []
    vals: list[Any] = []
    if 'settle_amount_fc' in cols:
        sets.append('settle_amount_fc = ?')
        vals.append(float(new_settled))
    if 'settle_journal_id' in cols:
        sets.append('settle_journal_id = ?')
        vals.append(entry['id'])
    if 'ar_status' in cols:
        sets.append('ar_status = ?')
        vals.append(ar_status)
    if sets:
        vals.append(sale_id)
        conn.execute(f"UPDATE sale SET {', '.join(sets)} WHERE id = ?", vals)

    try:
        from Services.sme.cong_no_ops import apply_ar_receipt
        apply_ar_receipt(conn, int(sale_id), float(book_vnd))
    except sqlite3.OperationalError:
        pass

    # Xác nhận dòng Nợ 1122 / Có 131 đã có trong DB trước khi commit
    chk = conn.execute(
        """
        SELECT
            SUM(CASE WHEN account_code LIKE '1122%' AND debit > 0 THEN debit ELSE 0 END) AS d1122,
            SUM(CASE WHEN account_code LIKE '131%' AND credit > 0 THEN credit ELSE 0 END) AS c131
        FROM sme_journal_lines WHERE entry_id = ?
        """,
        (entry['id'],),
    ).fetchone()
    if not chk or float(chk[0] or 0) <= 0 or float(chk[1] or 0) <= 0:
        raise ValueError(
            f'Bút toán {entry.get("entry_no")} thiếu Nợ 1122 / Có 131 — hủy giao dịch'
        )

    if commit:
        conn.commit()
    return {
        'success': True,
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'voucher_id': voucher_id,
        'voucher_no': voucher_no,
        'form_code': VOUCHER_FORM_RECEIPT,
        'document_no': voucher_no,
        'debit_account': cash_acct,
        'credit_account': ar_acct,
        'amount_fc': float(use_fc),
        'bank_vnd': float(bank_vnd),
        'book_vnd': float(book_vnd),
        'fx_diff': float(diff),
        'ar_status': ar_status,
        'message': (
            f'Đã lập phiếu thu {voucher_no}: Nợ {cash_acct} / Có {ar_acct} '
            f'{float(bank_vnd):,.0f}₫ — {desc}'
        ),
    }


def create_doc_discount(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    discount_date: str,
    amount_fc,
    exchange_rate,
    fee_vnd=0,
    cash_account: str = '1122',
    loan_account: str = '3411',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """TH4: Chiết khấu bộ CT — Nợ 112 (+ 635 phí) / Có 341."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_export_sale_schema(conn, commit=False)
    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError('Không tìm thấy phiếu bán')
    s = dict(sale)
    fc = _money(amount_fc)
    rate = _fx(exchange_rate)
    fee = _money(fee_vnd)
    if fc <= 0:
        raise ValueError('Số NT chiết khấu phải > 0')
    gross_vnd = _money(fc * rate)
    net_vnd = _money(gross_vnd - fee)
    if net_vnd <= 0:
        raise ValueError('Số thực nhận sau phí phải > 0')

    date_s = str(discount_date or '')[:10]
    sale_no = s.get('sale_no') or f'#{sale_id}'
    cash = resolve_postable_account(conn, cash_account or '1122')
    loan = resolve_postable_account(conn, loan_account or '3411')
    desc = f'Chiết khấu bộ CT XK {sale_no}'

    lines = [
        {
            'sequence': 1,
            'account_code': cash,
            'debit': float(net_vnd),
            'credit': 0,
            'debit_fc': float(fc) if str(s.get('currency') or 'USD') != 'VND' else 0,
            'description': desc,
        },
    ]
    seq = 2
    if fee > 0:
        lines.append({
            'sequence': seq,
            'account_code': resolve_postable_account(conn, '635'),
            'debit': float(fee),
            'credit': 0,
            'description': f'Phí chiết khấu XK {sale_no}',
        })
        seq += 1
    lines.append({
        'sequence': seq,
        'account_code': loan,
        'debit': 0,
        'credit': float(gross_vnd),
        'credit_fc': float(fc) if str(s.get('currency') or 'USD') != 'VND' else 0,
        'description': desc,
    })

    from Services.sme.branch_filter import warehouse_branch_or_session
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='EXPORT_DOC_DISCOUNT',
        document_no=sale_no,
        document_id=sale_id,
        business_type='CHIET_KHAU_XK',
        currency=s.get('currency') or 'USD',
        exchange_rate=float(rate),
        description=desc,
        created_by=created_by,
        branch_code=warehouse_branch_or_session(conn, s.get('warehouse_code')),
        lines=lines,
    )
    cur = conn.execute(
        """
        INSERT INTO sme_export_doc_discounts
        (sale_id, discount_date, amount_fc, exchange_rate, amount_vnd, fee_vnd,
         cash_account, loan_account, journal_entry_id, status, created_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)
        """,
        (
            sale_id, date_s, float(fc), float(rate), float(gross_vnd), float(fee),
            cash, loan, entry['id'], created_by, _now(),
        ),
    )
    disc_id = cur.lastrowid
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sale)').fetchall()}
    if 'discount_loan_id' in cols:
        conn.execute(
            'UPDATE sale SET discount_loan_id = ?, payment_mode = COALESCE(payment_mode, ?) WHERE id = ?',
            (disc_id, 'doc_discount', sale_id),
        )
    if commit:
        conn.commit()
    return {
        'success': True,
        'id': disc_id,
        'journal_entry_id': entry['id'],
        'amount_vnd': float(gross_vnd),
        'net_vnd': float(net_vnd),
        'fee_vnd': float(fee),
    }


def settle_doc_discount(
    conn: sqlite3.Connection,
    discount_id: int,
    *,
    settle_date: str,
    ar_book_rate=None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Khi KH trả — NH thu hồi vay: Nợ 341 / Có 131 + CLTG."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_export_sale_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_export_doc_discounts WHERE id = ?', (discount_id,),
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy khoản chiết khấu')
    d = dict(row)
    if d.get('status') == 'settled':
        raise ValueError('Đã tất toán chiết khấu')
    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (d['sale_id'],)).fetchone()
    if not sale:
        raise ValueError('Không tìm thấy phiếu bán')
    s = dict(sale)

    fc = _money(d.get('amount_fc'))
    loan_vnd = _money(d.get('amount_vnd'))
    book_rate = _fx(ar_book_rate or s.get('exchange_rate') or d.get('exchange_rate'))
    ar_vnd = _money(fc * book_rate)
    diff = _money(loan_vnd - ar_vnd)  # nếu vay VND > sổ 131 → lỗ? 
    # Đúng hơn: so sánh VND sổ 131 vs VND tất toán 341
    # Nợ 341 (loan_vnd) / Có 131 (ar_vnd) — chênh vào 515/635
    date_s = str(settle_date or '')[:10]
    sale_no = s.get('sale_no') or f'#{s["id"]}'
    loan = resolve_postable_account(conn, d.get('loan_account') or '3411')
    ar = resolve_postable_account(conn, '131')
    desc = f'Tất toán chiết khấu CT XK {sale_no}'

    lines = [
        {
            'sequence': 1,
            'account_code': loan,
            'debit': float(loan_vnd),
            'credit': 0,
            'debit_fc': float(fc),
            'description': desc,
        },
        {
            'sequence': 2,
            'account_code': ar,
            'debit': 0,
            'credit': float(ar_vnd),
            'credit_fc': float(fc),
            'partner_type': 'customer',
            'description': desc,
        },
    ]
    fx = _money(loan_vnd - ar_vnd)
    if fx > Decimal('0.009'):
        # Nợ 341 nhiều hơn Có 131 → cần thêm Có 515 (lãi) hoặc Nợ? 
        # Cân: Nợ 341 = Có 131 + Có 515 nếu loan > ar (lãi? thực ra là chênh TG)
        lines.append({
            'sequence': 3,
            'account_code': resolve_postable_account(conn, '515'),
            'debit': 0,
            'credit': float(fx),
            'description': f'CLTG tất toán chiết khấu {sale_no}',
        })
    elif fx < Decimal('-0.009'):
        lines.append({
            'sequence': 3,
            'account_code': resolve_postable_account(conn, '635'),
            'debit': float(abs(fx)),
            'credit': 0,
            'description': f'CLTG tất toán chiết khấu {sale_no}',
        })

    from Services.sme.branch_filter import warehouse_branch_or_session
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='EXPORT_DOC_DISCOUNT_SETTLE',
        document_no=sale_no,
        document_id=int(s['id']),
        business_type='TAT_TOAN_CK_XK',
        currency=s.get('currency') or 'USD',
        exchange_rate=float(book_rate),
        description=desc,
        created_by=created_by,
        branch_code=warehouse_branch_or_session(conn, s.get('warehouse_code')),
        lines=lines,
    )
    conn.execute(
        """
        UPDATE sme_export_doc_discounts
        SET status = 'settled', settle_journal_id = ?
        WHERE id = ?
        """,
        (entry['id'], discount_id),
    )
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sale)').fetchall()}
    if 'ar_status' in cols:
        conn.execute(
            "UPDATE sale SET ar_status = 'settled', settle_journal_id = ? WHERE id = ?",
            (entry['id'], int(s['id'])),
        )
    if commit:
        conn.commit()
    return {'success': True, 'journal_entry_id': entry['id'], 'fx_diff': float(fx)}


def post_export_cost(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    cost_date: str,
    description: str,
    amount_vnd,
    vat_vnd=0,
    payment_method: str = 'bank',
    credit_account: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Phần III: Nợ 641 (+ 1331) / Có 111·112·331."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_export_sale_schema(conn, commit=False)
    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError('Không tìm thấy phiếu bán')
    s = dict(sale)
    amt = _money(amount_vnd)
    vat = _money(vat_vnd)
    if amt <= 0:
        raise ValueError('Số tiền chi phí phải > 0')
    date_s = str(cost_date or '')[:10]
    sale_no = s.get('sale_no') or f'#{sale_id}'
    pm = str(payment_method or 'bank').lower()
    if credit_account:
        cred = str(credit_account).strip()
    elif pm in ('credit', '331', 'cong_no'):
        cred = '331'
    elif pm in ('cash', '111', '1111'):
        cred = '1111'
    else:
        cred = '1121'
    cred = resolve_postable_account(conn, cred)
    exp = resolve_postable_account(conn, '641')
    desc = description or f'Chi phí XK {sale_no}'

    lines = [
        {
            'sequence': 1,
            'account_code': exp,
            'debit': float(amt),
            'credit': 0,
            'description': desc,
        },
    ]
    seq = 2
    if vat > 0:
        lines.append({
            'sequence': seq,
            'account_code': resolve_postable_account(conn, '1331'),
            'debit': float(vat),
            'credit': 0,
            'description': f'VAT CP XK {sale_no}',
        })
        seq += 1
    lines.append({
        'sequence': seq,
        'account_code': cred,
        'debit': 0,
        'credit': float(amt + vat),
        'description': desc,
    })

    from Services.sme.branch_filter import warehouse_branch_or_session
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='EXPORT_COST',
        document_no=sale_no,
        document_id=sale_id,
        business_type='CHI_PHI_XK',
        currency='VND',
        exchange_rate=1,
        description=desc,
        created_by=created_by,
        branch_code=warehouse_branch_or_session(conn, s.get('warehouse_code')),
        lines=lines,
    )
    conn.execute(
        """
        INSERT INTO sme_export_costs
        (sale_id, cost_date, description, amount_vnd, vat_vnd, credit_account,
         payment_method, journal_entry_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            sale_id, date_s, desc, float(amt), float(vat), cred,
            payment_method, entry['id'], _now(),
        ),
    )
    if commit:
        conn.commit()
    return {
        'success': True,
        'journal_entry_id': entry['id'],
        'amount_vnd': float(amt),
        'vat_vnd': float(vat),
    }


def list_export_costs(conn: sqlite3.Connection, sale_id: int) -> list[dict]:
    ensure_export_sale_schema(conn, commit=False)
    return [dict(r) for r in conn.execute(
        'SELECT * FROM sme_export_costs WHERE sale_id = ? ORDER BY id',
        (sale_id,),
    ).fetchall()]
