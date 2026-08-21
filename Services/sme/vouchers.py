"""Chứng từ thu/chi SME (mẫu 01-TT / 02-TT) — journal-first, tách HKD phieu_thu/chi."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry

MONEY_Q = Decimal('0.01')

VOUCHER_FORM_RECEIPT = '01-TT'
VOUCHER_FORM_PAYMENT = '02-TT'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_voucher_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_type TEXT NOT NULL,
            form_code TEXT NOT NULL,
            voucher_no TEXT NOT NULL,
            voucher_date TEXT NOT NULL,
            party_name TEXT,
            party_address TEXT,
            party_tax_code TEXT,
            amount REAL NOT NULL DEFAULT 0,
            debit_account TEXT NOT NULL,
            credit_account TEXT NOT NULL,
            reason TEXT,
            attached_docs INTEGER DEFAULT 0,
            reference_document TEXT,
            source_type TEXT,
            source_id INTEGER,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(voucher_type, voucher_no)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_vouchers_date
        ON sme_vouchers(voucher_type, voucher_date)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_vouchers_journal
        ON sme_vouchers(journal_entry_id)
        """
    )
    cols = {r[1] for r in c.execute('PRAGMA table_info(sme_vouchers)').fetchall()}
    alters = {
        'branch_code': 'TEXT',
        'currency': "TEXT DEFAULT 'VND'",
        'exchange_rate': 'REAL DEFAULT 1',
        'amount_fc': 'REAL DEFAULT 0',
        'purpose': 'TEXT',
    }
    for col, decl in alters.items():
        if col not in cols:
            try:
                c.execute(f'ALTER TABLE sme_vouchers ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass
    if commit:
        conn.commit()


def _voucher_prefix(voucher_type: str) -> str:
    return 'PT' if voucher_type == 'receipt' else 'PC'


def _next_voucher_no(conn: sqlite3.Connection, voucher_type: str) -> str:
    """Số liên tục PT/PC000001… theo max số hiện có (không theo id)."""
    prefix = _voucher_prefix(voucher_type)
    width = 6
    row = conn.execute(
        """
        SELECT voucher_no FROM sme_vouchers
        WHERE voucher_type = ?
          AND voucher_no GLOB ?
          AND length(voucher_no) = ?
          AND substr(voucher_no, ?) GLOB '[0-9]*'
        ORDER BY CAST(substr(voucher_no, ?) AS INTEGER) DESC
        LIMIT 1
        """,
        (
            voucher_type,
            f'{prefix}[0-9]*',
            len(prefix) + width,
            len(prefix) + 1,
            len(prefix) + 1,
        ),
    ).fetchone()
    seq = 1
    if row and row[0]:
        tail = str(row[0])[len(prefix):]
        if tail.isdigit():
            seq = int(tail) + 1
    return f'{prefix}{seq:0{width}d}'


def renumber_vouchers(
    conn: sqlite3.Connection,
    voucher_type: str,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    """Đánh lại số phiếu thu/chi liên tục theo ngày + id (giống HKD).

    Đồng bộ ``document_no`` trên bút toán liên kết.
    """
    ensure_sme_voucher_schema(conn, commit=False)
    vtype = (voucher_type or '').strip().lower()
    if vtype not in ('receipt', 'payment'):
        raise ValueError('Loại chứng từ không hợp lệ (receipt|payment)')

    prefix = _voucher_prefix(vtype)
    width = 6
    label = 'phiếu thu' if vtype == 'receipt' else 'phiếu chi'

    rows = conn.execute(
        """
        SELECT id, voucher_no, journal_entry_id
        FROM sme_vouchers
        WHERE voucher_type = ?
        ORDER BY date(voucher_date) ASC, id ASC
        """,
        (vtype,),
    ).fetchall()
    if not rows:
        raise ValueError(f'Không có {label} nào để đánh lại số')

    # Tránh UNIQUE(voucher_type, voucher_no) khi đổi số chéo
    for row in rows:
        vid = int(row['id'] if hasattr(row, 'keys') else row[0])
        conn.execute(
            "UPDATE sme_vouchers SET voucher_no = ?, updated_at = ? WHERE id = ?",
            (f'__TMP_{prefix}_{vid}', _now(), vid),
        )

    count = 0
    for index, row in enumerate(rows, start=1):
        vid = int(row['id'] if hasattr(row, 'keys') else row[0])
        journal_id = row['journal_entry_id'] if hasattr(row, 'keys') else row[2]
        new_no = f'{prefix}{index:0{width}d}'
        conn.execute(
            "UPDATE sme_vouchers SET voucher_no = ?, updated_at = ? WHERE id = ?",
            (new_no, _now(), vid),
        )
        if journal_id:
            conn.execute(
                """
                UPDATE sme_journal_entries
                SET document_no = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_no, _now(), int(journal_id)),
            )
        count += 1

    if commit:
        conn.commit()
    return {
        'voucher_type': vtype,
        'count': count,
        'prefix': prefix,
        'message': (
            f'Đã đánh lại số {count} {label} từ {prefix}000001 '
            f'theo thứ tự ngày lập.'
        ),
    }


def _cash_account(payment_method: str, *, currency: str = 'VND') -> str:
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
    # Cho phép truyền thẳng mã TK (kể cả TK con 11211, 112111…)
    if method[0:1].isdigit() and (method.startswith('111') or method.startswith('112')):
        return method if all(ch.isdigit() for ch in method) else method
    return '1122' if fx else '1111'


def _resolve_cash_gl(
    conn: sqlite3.Connection,
    payment_method: str,
    *,
    currency: str = 'VND',
) -> str:
    """TK tiền ghi sổ — ưu tiên STK VietQR làm mặc định 1121*."""
    from Services.sme.bank_accounts import resolve_cash_gl_account
    return resolve_cash_gl_account(conn, payment_method, currency=currency)


def _vnd_funding_account(payment_method: str, conn: sqlite3.Connection | None = None) -> str:
    """TK nguồn VND khi mua ngoại tệ (luôn 1111 hoặc 1121*, không bao giờ 1112/1122)."""
    method = (payment_method or 'bank').strip().lower()
    if method in (
        'cash', '111', '1111', 'cash_fx', 'fx_cash', '1112',
    ) or (method[:1].isdigit() and method.startswith('111')):
        if conn is not None:
            return _resolve_cash_gl(conn, 'cash', currency='VND')
        return '1111'
    if conn is not None:
        # Mã TK 1121* cụ thể hoặc bank → mặc định QR
        if method[:1].isdigit() and method.startswith('1121'):
            return _resolve_cash_gl(conn, method, currency='VND')
        return _resolve_cash_gl(conn, 'bank', currency='VND')
    return '1121'


def _is_fx_cash_account(code: str) -> bool:
    c = (code or '').strip()
    return c.startswith('1122') or c.startswith('1112')


def _supplier_partner_id(conn: sqlite3.Connection, import_id: int | None) -> int | None:
    if not import_id:
        return None
    try:
        row = conn.execute(
            'SELECT supplier_id FROM import WHERE id = ?', (int(import_id),),
        ).fetchone()
        if not row:
            return None
        sid = row[0] if not isinstance(row, sqlite3.Row) else row['supplier_id']
        return int(sid) if sid else None
    except (TypeError, ValueError, sqlite3.Error):
        return None


def _resolve_voucher_amounts(
    *,
    amount=None,
    amount_fc=None,
    currency: str = 'VND',
    exchange_rate=1,
) -> tuple[Decimal, Decimal, str, Decimal]:
    """Trả (amount_vnd, amount_fc, currency, exchange_rate)."""
    cur = (currency or 'VND').strip().upper() or 'VND'
    rate = Decimal(str(exchange_rate or 1))
    if rate <= 0:
        rate = Decimal('1')
    if cur == 'VND':
        amt = _money(amount if amount is not None else amount_fc)
        return amt, Decimal('0.00'), 'VND', Decimal('1')
    fc = _money(amount_fc if amount_fc is not None else 0)
    if fc <= 0 and amount is not None:
        # Cho phép nhập VND rồi suy ra FC khi có tỷ giá
        vnd = _money(amount)
        if vnd > 0 and rate > 0:
            fc = (vnd / rate).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        else:
            raise ValueError('Số tiền ngoại tệ phải > 0')
    if fc <= 0:
        raise ValueError('Số tiền ngoại tệ phải > 0')
    vnd = _money(fc * rate)
    return vnd, fc, cur, rate.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def create_receipt(
    conn: sqlite3.Connection,
    *,
    voucher_date: str,
    party_name: str,
    amount=None,
    payment_method: str = 'cash',
    credit_account: str = '131',
    reason: str = '',
    party_address: str = '',
    party_tax_code: str = '',
    reference_document: str = '',
    source_type: str | None = None,
    source_id: int | None = None,
    sale_id: int | None = None,
    allocations: list | None = None,
    currency: str = 'VND',
    exchange_rate=1,
    amount_fc=None,
    purpose: str | None = None,
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Lập phiếu thu 01-TT + bút toán Nợ 1111/1121/1112/1122 · Có credit_account.

    Hỗ trợ ngoại tệ: ``amount_fc`` × ``exchange_rate`` (tỷ giá ngày thu) → VND.
    Thu ngoại tệ vào 1112/1122: Nợ TK NT (có FC) · Có đối ứng.
    ``allocations``: [{sale_id, amount}, ...] — một PT phân bổ nhiều HĐ bán.
    """
    from Services.sme.branches import resolve_posting_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    branch = resolve_posting_branch(conn, branch_code)

    purpose_s = (purpose or '').strip() or None
    if purpose_s in ('fx_receipt', 'thu_ngoai_te', 'receive_fx'):
        purpose_s = 'fx_receipt'
    if purpose_s in (
        'customer_advance', 'tam_ung_kh', 'ung_truoc_kh', 'advance_customer',
        'kh_advance', 'thu_tam_ung_kh',
    ):
        purpose_s = 'customer_advance'
    if purpose_s in (
        'tat_toan_113', 'settle_113', 'nhan_tien_nh', 'giai_toa_113',
        'cash_in_transit_in',
    ):
        purpose_s = 'tat_toan_113'

    alloc_rows: list[dict[str, Any]] = []
    if allocations:
        for raw in allocations:
            if not isinstance(raw, dict):
                continue
            try:
                sid = int(raw.get('sale_id') or 0)
                aamt = float(raw.get('amount') or 0)
            except (TypeError, ValueError):
                continue
            if sid <= 0 or aamt <= 0:
                continue
            alloc_rows.append({'sale_id': sid, 'amount': aamt})
        if not alloc_rows:
            raise ValueError('Danh sách phân bổ thu công nợ trống hoặc không hợp lệ')
        if amount is None and amount_fc is None:
            amount = sum(r['amount'] for r in alloc_rows)
        if not sale_id:
            sale_id = alloc_rows[0]['sale_id']
        if not reference_document:
            nos = []
            for r in alloc_rows:
                sn = conn.execute(
                    'SELECT sale_no FROM sale WHERE id = ?', (r['sale_id'],),
                ).fetchone()
                if sn:
                    nos.append(str(sn[0] if not isinstance(sn, sqlite3.Row) else sn['sale_no']))
            reference_document = ', '.join(nos)

    amt, fc_amt, cur, rate = _resolve_voucher_amounts(
        amount=amount, amount_fc=amount_fc, currency=currency, exchange_rate=exchange_rate,
    )
    if amt <= 0:
        raise ValueError('Số tiền phiếu thu phải > 0')
    date_s = str(voucher_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày phiếu thu')

    debit = _resolve_cash_gl(conn, payment_method, currency=cur)
    credit = str(credit_account or '131').strip() or '131'

    # Tất toán 113: Nợ 1121 (TGNH) / Có 1131 (tiền đang chuyển)
    if purpose_s == 'tat_toan_113':
        cur = 'VND'
        debit = _resolve_cash_gl(conn, 'bank', currency='VND')
        credit = '1131'

    # Tạm ứng KH XK: Nợ 1122|1112 / Có 131 (bắt buộc NT)
    if purpose_s == 'customer_advance':
        if cur == 'VND':
            raise ValueError('Tạm ứng khách hàng XK cần ngoại tệ (USD/EUR/…)')
        credit = '131' if not credit.startswith('131') else credit
        if not _is_fx_cash_account(debit):
            pm = str(payment_method or '').lower()
            debit = _resolve_cash_gl(
                conn,
                'bank_fx' if pm in (
                    'bank', 'bank_fx', '112', '1121', '1122', 'transfer', 'ck',
                ) or pm.startswith('112') else 'cash_fx',
                currency=cur,
            )

    # Thu ngoại tệ: buộc Nợ 1112/1122 khi purpose fx_receipt
    if purpose_s == 'fx_receipt':
        if cur == 'VND':
            raise ValueError('Thu ngoại tệ cần chọn loại ngoại tệ (USD/EUR/…)')
        if not _is_fx_cash_account(debit):
            pm = str(payment_method or '').lower()
            debit = _resolve_cash_gl(
                conn,
                'bank_fx' if pm in (
                    'bank', 'bank_fx', '112', '1121', '1122', 'transfer', 'ck',
                ) or pm.startswith('112') else 'cash_fx',
                currency=cur,
            )
        if debit == credit:
            raise ValueError(f'Hạch toán không hợp lệ: Nợ và Có cùng TK {debit}')

    if debit == credit:
        raise ValueError(f'Hạch toán không hợp lệ: Nợ và Có cùng TK {debit}')

    vno = _next_voucher_no(conn, 'receipt')
    desc = reason or f'Thu tiền {party_name or ""}'.strip()
    if purpose_s == 'customer_advance' and not reason:
        desc = f'Tạm ứng khách hàng XK — Có 131 / Nợ {debit}'
    if purpose_s == 'fx_receipt' and not reason:
        desc = f'Thu ngoại tệ {cur} vào TK {debit}'.strip()
    if purpose_s == 'tat_toan_113' and not reason:
        desc = f'Tất toán tiền đang chuyển — Nợ {debit} / Có 1131'
    if cur != 'VND':
        desc = (
            f'{desc} ({float(fc_amt):g} {cur} × {float(rate):g})'
        ).strip()

    use_debit_fc = cur != 'VND' and _is_fx_cash_account(debit)
    # FC phía Có chỉ khi đối ứng công nợ NT (131*/331*)
    use_credit_fc = (
        cur != 'VND'
        and (
            credit.startswith('131')
            or credit.startswith('331')
            or _is_fx_cash_account(credit)
            or purpose_s == 'customer_advance'
        )
    )

    def _partner_for_sale(sid: int | None) -> int | None:
        if not sid:
            return None
        try:
            from Services.sme.sale_journal import resolve_sale_partner_id
            row = conn.execute('SELECT * FROM sale WHERE id = ?', (int(sid),)).fetchone()
            if row:
                return resolve_sale_partner_id(conn, row)
        except Exception:
            return None
        return None

    lines: list[dict[str, Any]] = [
        {
            'sequence': 1,
            'account_code': debit,
            'debit': float(amt),
            'credit': 0,
            'debit_fc': float(fc_amt) if use_debit_fc else 0,
            'credit_fc': 0,
            'description': desc,
        },
    ]
    if alloc_rows and credit.startswith('131') and purpose_s != 'customer_advance':
        ratio = float(fc_amt) / float(amt) if amt and cur != 'VND' else 0.0
        seq = 2
        for ar in alloc_rows:
            a_vnd = float(ar['amount'])
            a_fc = (a_vnd * ratio) if ratio else 0.0
            lines.append({
                'sequence': seq,
                'account_code': credit,
                'debit': 0,
                'credit': a_vnd,
                'debit_fc': 0,
                'credit_fc': float(a_fc) if use_credit_fc else 0,
                'description': desc,
                'partner_type': 'customer',
                'partner_id': _partner_for_sale(ar['sale_id']),
            })
            seq += 1
    else:
        lines.append({
            'sequence': 2,
            'account_code': credit,
            'debit': 0,
            'credit': float(amt),
            'debit_fc': 0,
            'credit_fc': float(fc_amt) if use_credit_fc else 0,
            'description': desc,
            'partner_type': 'customer' if credit.startswith('131') else None,
            'partner_id': _partner_for_sale(sale_id) if credit.startswith('131') else None,
        })

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='PT',
        document_no=vno,
        document_id=source_id or sale_id,
        business_type=(
            'TAM_UNG_KH' if purpose_s == 'customer_advance'
            else ('THU_NGOAI_TE' if purpose_s == 'fx_receipt'
                  else ('TAT_TOAN_113' if purpose_s == 'tat_toan_113' else 'THU_TIEN'))
        ),
        currency=cur,
        exchange_rate=float(rate),
        description=desc,
        reference_document=reference_document or None,
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )

    cur_db = conn.cursor()
    cols = {r[1] for r in cur_db.execute('PRAGMA table_info(sme_vouchers)').fetchall()}
    base_cols = [
        'voucher_type', 'form_code', 'voucher_no', 'voucher_date',
        'party_name', 'party_address', 'party_tax_code', 'amount',
        'debit_account', 'credit_account', 'reason', 'reference_document',
        'source_type', 'source_id', 'journal_entry_id', 'status', 'created_by',
        'created_at', 'updated_at', 'branch_code',
    ]
    base_vals: list[Any] = [
        'receipt', VOUCHER_FORM_RECEIPT, vno, date_s,
        party_name, party_address, party_tax_code, float(amt),
        debit, credit, desc, reference_document or None,
        source_type or (
            'customer_advance' if purpose_s == 'customer_advance'
            else ('fx_receipt' if purpose_s == 'fx_receipt'
                  else ('tat_toan_113' if purpose_s == 'tat_toan_113'
                        else ('sale' if sale_id else None)))
        ),
        source_id or sale_id,
        entry['id'], 'posted', created_by, _now(), _now(), branch,
    ]
    if 'currency' in cols:
        base_cols.extend(['currency', 'exchange_rate', 'amount_fc'])
        base_vals.extend([cur, float(rate), float(fc_amt)])
    if 'purpose' in cols:
        base_cols.append('purpose')
        base_vals.append(purpose_s)
    placeholders = ','.join('?' * len(base_cols))
    cur_db.execute(
        f"INSERT INTO sme_vouchers ({', '.join(base_cols)}) VALUES ({placeholders})",
        base_vals,
    )
    voucher_id = cur_db.lastrowid

    # Cập nhật công nợ bán nếu thu theo đơn / phân bổ
    if credit.startswith('131') and purpose_s != 'customer_advance':
        try:
            from Services.sme.cong_no_ops import apply_ar_receipt
            if alloc_rows:
                for ar in alloc_rows:
                    apply_ar_receipt(conn, int(ar['sale_id']), float(ar['amount']))
            elif sale_id:
                apply_ar_receipt(conn, int(sale_id), float(amt))
        except sqlite3.OperationalError:
            pass

    if commit:
        conn.commit()

    return {
        'id': voucher_id,
        'voucher_no': vno,
        'form_code': VOUCHER_FORM_RECEIPT,
        'journal_entry_id': entry['id'],
        'amount': float(amt),
        'amount_fc': float(fc_amt),
        'currency': cur,
        'exchange_rate': float(rate),
        'debit_account': debit,
        'credit_account': credit,
        'purpose': purpose_s,
        'branch_code': branch,
        'allocations': alloc_rows or None,
    }


def create_payment(
    conn: sqlite3.Connection,
    *,
    voucher_date: str,
    party_name: str,
    amount=None,
    payment_method: str = 'cash',
    debit_account: str = '331',
    reason: str = '',
    party_address: str = '',
    party_tax_code: str = '',
    reference_document: str = '',
    source_type: str | None = None,
    source_id: int | None = None,
    import_id: int | None = None,
    allocations: list | None = None,
    currency: str = 'VND',
    exchange_rate=1,
    amount_fc=None,
    purpose: str | None = None,
    created_by: str | None = None,
    branch_code: str | None = None,
    debit_lines: list[dict[str, Any]] | None = None,
    form_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Lập phiếu chi 02-TT + bút toán Nợ debit · Có 1111/1121/1112/1122.

    ``purpose=supplier_advance``: tạm ứng NCC (Nợ 331), hỗ trợ ngoại tệ + tỷ giá ngày ứng.
    ``purpose=buy_fx``: mua ngoại tệ bằng VND — Nợ 1122|1112 (FC) / Có 1111|1121 (VND).
    ``debit_lines`` (tuỳ chọn): nhiều dòng Nợ ``[{account_code, amount, description?}, ...]``.
    ``allocations``: [{import_id, amount}, ...] — một PC phân bổ nhiều phiếu nhập.
    """
    from Services.sme.branches import resolve_posting_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    branch = resolve_posting_branch(conn, branch_code)

    alloc_imports: list[dict[str, Any]] = []
    if allocations and not debit_lines:
        for raw in allocations:
            if not isinstance(raw, dict):
                continue
            try:
                iid = int(raw.get('import_id') or 0)
                aamt = float(raw.get('amount') or 0)
            except (TypeError, ValueError):
                continue
            if iid <= 0 or aamt <= 0:
                continue
            alloc_imports.append({'import_id': iid, 'amount': aamt})
        if alloc_imports:
            debit_lines = [
                {
                    'account_code': str(debit_account or '331').strip() or '331',
                    'amount': a['amount'],
                    'partner_id': _supplier_partner_id(conn, a['import_id']),
                    'description': reason or 'Thanh toán công nợ NCC',
                }
                for a in alloc_imports
            ]
            if not import_id:
                import_id = alloc_imports[0]['import_id']
            if amount is None and amount_fc is None:
                amount = sum(a['amount'] for a in alloc_imports)

    date_s = str(voucher_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày phiếu chi')

    purpose_s = (purpose or '').strip() or None
    if purpose_s in ('advance', 'tam_ung_ncc', 'ung_truoc_ncc', 'ncc_advance'):
        purpose_s = 'supplier_advance'
    if purpose_s in ('buy_fx', 'mua_ngoai_te', 'mua_nt', 'fx_purchase'):
        purpose_s = 'buy_fx'
    if purpose_s in (
        'nop_ngan_hang', 'nop_nh', 'cash_in_transit', '113', 'gui_tien_nh',
    ):
        purpose_s = 'nop_ngan_hang'

    amt, fc_amt, cur, rate = _resolve_voucher_amounts(
        amount=amount, amount_fc=amount_fc, currency=currency, exchange_rate=exchange_rate,
    )

    # Xác định TK Nợ trước (cần để nhận diện mua ngoại tệ)
    if debit_lines:
        debit = None  # gán sau khi duyệt dòng
    else:
        debit = str(debit_account or '331').strip() or '331'

    pm = str(payment_method or '').strip().lower()
    pm_is_vnd_source = pm in (
        'cash', 'bank', 'bank_transfer', 'ck', 'transfer',
        '111', '1111', '112', '1121',
    ) or (pm[:1].isdigit() and (pm.startswith('1111') or pm.startswith('1121')))

    # Mua ngoại tệ: Nợ 1122|1112 (FC) / Có 1111|1121 (VND). Không bao giờ Có 1122.
    auto_buy_fx = (
        purpose_s not in ('supplier_advance', 'nop_ngan_hang')
        and cur != 'VND'
        and not debit_lines
        and _is_fx_cash_account(debit)
        and pm_is_vnd_source
    )
    if purpose_s == 'buy_fx' or auto_buy_fx:
        purpose_s = 'buy_fx'
        if cur == 'VND':
            raise ValueError('Mua ngoại tệ cần chọn loại ngoại tệ (USD/EUR/…)')
        if rate <= 0:
            raise ValueError('Nhập tỷ giá ngoại tệ khi mua ngoại tệ')
        if not _is_fx_cash_account(debit):
            debit = _resolve_cash_gl(conn, 'bank_fx', currency=cur)
        credit = _vnd_funding_account(payment_method, conn)
    elif purpose_s == 'nop_ngan_hang':
        # Nộp tiền mặt vào NH: Nợ 1131 / Có 1111 — chờ tất toán Nợ 1121 / Có 1131
        cur = 'VND'
        debit = '1131'
        credit = _resolve_cash_gl(conn, 'cash', currency='VND')
    else:
        credit = _resolve_cash_gl(conn, payment_method, currency=cur)

    vno = _next_voucher_no(conn, 'payment')
    desc = reason or f'Chi tiền {party_name or ""}'.strip()
    if purpose_s == 'supplier_advance' and not reason:
        desc = f'Tạm ứng NCC {party_name or ""}'.strip()
    if purpose_s == 'buy_fx' and not reason:
        desc = f'Mua ngoại tệ {cur} vào TK {debit}'.strip()
    if purpose_s == 'nop_ngan_hang' and not reason:
        desc = 'Nộp tiền mặt vào ngân hàng — Nợ 1131 / Có 1111'
    if cur != 'VND':
        desc = f'{desc} ({float(fc_amt):g} {cur} × {float(rate):g})'.strip()

    lines: list[dict[str, Any]] = []
    if debit_lines:
        total = Decimal('0.00')
        seq = 1
        primary_debit = None
        for ln in debit_lines:
            ln_amt = _money(ln.get('amount'))
            if ln_amt <= 0:
                continue
            acct = str(ln.get('account_code') or '').strip()
            if not acct:
                raise ValueError('Thiếu tài khoản Nợ trên dòng phiếu chi')
            if primary_debit is None:
                primary_debit = acct
            lines.append({
                'sequence': seq,
                'account_code': acct,
                'debit': float(ln_amt),
                'credit': 0,
                'debit_fc': float(_money(ln.get('amount_fc') or 0)),
                'credit_fc': 0,
                'description': (ln.get('description') or desc),
                'partner_type': 'supplier' if acct.startswith('331') else None,
                'partner_id': ln.get('partner_id') if acct.startswith('331') else None,
            })
            total += ln_amt
            seq += 1
        if total <= 0:
            raise ValueError('Số tiền phiếu chi phải > 0')
        amt = total
        debit = primary_debit or str(debit_account or '331').strip() or '331'
        accts = {str(x.get('account_code') or '') for x in debit_lines if _money(x.get('amount')) > 0}
        if len(accts) > 1 and all(a.startswith('338') for a in accts):
            debit = '338'
    else:
        if amt <= 0:
            raise ValueError('Số tiền phiếu chi phải > 0')
        if purpose_s == 'supplier_advance' and not debit.startswith('331'):
            debit = '331'
        # FC gắn đúng phía ngoại tệ: TK 1112/1122, hoặc tạm ứng 331*
        use_debit_fc = (
            cur != 'VND' and (
                _is_fx_cash_account(debit)
                or purpose_s == 'supplier_advance'
                or debit.startswith('331')
            )
        )
        lines.append({
            'sequence': 1,
            'account_code': debit,
            'debit': float(amt),
            'credit': 0,
            'debit_fc': float(fc_amt) if use_debit_fc else 0,
            'credit_fc': 0,
            'description': desc,
            'partner_type': (
                'supplier' if purpose_s == 'supplier_advance' or debit.startswith('331') else None
            ),
            'partner_id': (
                _supplier_partner_id(conn, import_id)
                if (import_id and (
                    purpose_s == 'supplier_advance' or debit.startswith('331')
                )) else None
            ),
        })

    # Không cho Nợ/Có trùng một TK tiền (lỗi cũ: bank+USD → Có 1122 khi Nợ cũng 1122)
    if _is_fx_cash_account(debit) and _is_fx_cash_account(credit) and debit[:4] == credit[:4]:
        if purpose_s == 'buy_fx' or pm_is_vnd_source:
            credit = _vnd_funding_account(payment_method, conn)
        else:
            raise ValueError(
                f'Hạch toán không hợp lệ: Nợ {debit} và Có {credit} trùng nhóm ngoại tệ. '
                f'Mua ngoại tệ phải Có {_vnd_funding_account(payment_method, conn)} (VND).'
            )
    if debit == credit:
        raise ValueError(f'Hạch toán không hợp lệ: Nợ và Có cùng TK {debit}')

    # Có VND khi mua NT: không ghi credit_fc; Có 1122/1112 mới gắn FC
    use_credit_fc = (
        cur != 'VND'
        and _is_fx_cash_account(credit)
        and purpose_s != 'buy_fx'
    )
    lines.append({
        'sequence': (lines[-1]['sequence'] + 1) if lines else 1,
        'account_code': credit,
        'debit': 0,
        'credit': float(amt),
        'debit_fc': 0,
        'credit_fc': float(fc_amt) if use_credit_fc else 0,
        'description': desc,
    })

    if purpose_s == 'buy_fx':
        biz = 'MUA_NGOAI_TE'
    elif purpose_s == 'supplier_advance':
        biz = 'TAM_UNG_NCC'
    elif purpose_s == 'nop_ngan_hang':
        biz = 'NOP_NGAN_HANG'
    else:
        biz = 'CHI_TIEN'

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='PC',
        document_no=vno,
        document_id=source_id or import_id,
        business_type=biz,
        currency=cur,
        exchange_rate=float(rate),
        description=desc,
        reference_document=reference_document or None,
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )

    form = (form_code or VOUCHER_FORM_PAYMENT).strip() or VOUCHER_FORM_PAYMENT
    cur_db = conn.cursor()
    cols = {r[1] for r in cur_db.execute('PRAGMA table_info(sme_vouchers)').fetchall()}
    base_cols = [
        'voucher_type', 'form_code', 'voucher_no', 'voucher_date',
        'party_name', 'party_address', 'party_tax_code', 'amount',
        'debit_account', 'credit_account', 'reason', 'reference_document',
        'source_type', 'source_id', 'journal_entry_id', 'status', 'created_by',
        'created_at', 'updated_at', 'branch_code',
    ]
    src_type = source_type or (
        'supplier_advance' if purpose_s == 'supplier_advance'
        else ('buy_fx' if purpose_s == 'buy_fx'
              else ('nop_ngan_hang' if purpose_s == 'nop_ngan_hang'
                    else ('import' if import_id else None)))
    )
    base_vals: list[Any] = [
        'payment', form, vno, date_s,
        party_name, party_address, party_tax_code, float(amt),
        debit, credit, desc, reference_document or None,
        src_type,
        source_id or import_id,
        entry['id'], 'posted', created_by, _now(), _now(), branch,
    ]
    if 'currency' in cols:
        base_cols.extend(['currency', 'exchange_rate', 'amount_fc'])
        base_vals.extend([cur, float(rate), float(fc_amt)])
    if 'purpose' in cols:
        base_cols.append('purpose')
        base_vals.append(purpose_s)
    placeholders = ','.join('?' * len(base_cols))
    cur_db.execute(
        f"INSERT INTO sme_vouchers ({', '.join(base_cols)}) VALUES ({placeholders})",
        base_vals,
    )
    voucher_id = cur_db.lastrowid

    if str(debit).startswith('331'):
        try:
            if alloc_imports:
                for a in alloc_imports:
                    cur_db.execute(
                        """
                        UPDATE import
                        SET paid_amount = COALESCE(paid_amount, 0) + ?
                        WHERE id = ?
                        """,
                        (float(a['amount']), int(a['import_id'])),
                    )
            elif import_id:
                cur_db.execute(
                    """
                    UPDATE import
                    SET paid_amount = COALESCE(paid_amount, 0) + ?
                    WHERE id = ?
                    """,
                    (float(amt), import_id),
                )
        except sqlite3.OperationalError:
            pass

    if commit:
        conn.commit()

    return {
        'id': voucher_id,
        'voucher_no': vno,
        'form_code': form,
        'journal_entry_id': entry['id'],
        'amount': float(amt),
        'amount_fc': float(fc_amt),
        'currency': cur,
        'exchange_rate': float(rate),
        'debit_account': debit,
        'credit_account': credit,
        'purpose': purpose_s,
        'branch_code': branch,
        'journal_lines': lines,
    }


def list_vouchers(
    conn: sqlite3.Connection,
    *,
    voucher_type: str,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_code: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_sme_voucher_schema(conn, commit=False)
    from Services.sme.branches import branch_sql_filter

    sql = """
        SELECT * FROM sme_vouchers v
        WHERE v.voucher_type = ? AND v.status != 'void'
    """
    params: list[Any] = [voucher_type]
    if date_from:
        sql += ' AND date(v.voucher_date) >= date(?)'
        params.append(date_from[:10])
    if date_to:
        sql += ' AND date(v.voucher_date) <= date(?)'
        params.append(date_to[:10])
    bf, bp = branch_sql_filter(branch_code, alias='v')
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY v.voucher_date DESC, v.id DESC LIMIT ?'
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_voucher(conn: sqlite3.Connection, voucher_id: int) -> dict[str, Any] | None:
    ensure_sme_voucher_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_vouchers WHERE id = ?', (voucher_id,)
    ).fetchone()
    return dict(row) if row else None


def void_voucher(
    conn: sqlite3.Connection,
    voucher_id: int,
    *,
    reason: str = 'Hủy chứng từ thu/chi',
    created_by: str | None = None,
    posting_date: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Hủy phiếu thu/chi.

    - Kỳ **chưa** chốt kê khai / **chưa** khóa sổ năm: **xóa** bút toán (+ chứng từ)
      để ghi lại — không tạo chứng từ đảo.
    - Kỳ đã chốt kê khai hoặc khóa năm: ghi bút toán đảo, giữ phiếu ``status=void``.
    """
    from Services.sme.journal_engine import reverse_journal_entry
    from Services.sme.period_lock import is_period_sealed

    ensure_sme_voucher_schema(conn, commit=False)
    voucher = get_voucher(conn, voucher_id)
    if not voucher:
        raise ValueError('Không tìm thấy chứng từ')
    if voucher.get('status') == 'void':
        raise ValueError('Chứng từ đã hủy trước đó')

    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_vouchers', voucher_id, label='Chứng từ thu/chi')

    date_s = (posting_date or voucher.get('voucher_date') or '')[:10]
    try:
        fy = int(date_s[:4])
        per = int(date_s[5:7])
    except (TypeError, ValueError):
        fy, per = 0, 0
    sealed = bool(fy and per and is_period_sealed(conn, fy, per))

    rev = None
    mode = 'hard_delete'
    if voucher.get('journal_entry_id'):
        try:
            rev = reverse_journal_entry(
                conn,
                int(voucher['journal_entry_id']),
                posting_date=posting_date,
                created_by=created_by,
                reason=reason,
            )
            mode = rev.get('mode') or ('reverse' if sealed else 'hard_delete')
        except ValueError as exc:
            # Bút toán đã bị xóa cứng trước đó — vẫn hủy chứng từ
            if 'Không tìm thấy bút toán' not in str(exc):
                raise
            rev = {'deleted': True, 'mode': 'hard_delete', 'voided_entry_id': voucher.get('journal_entry_id')}
            mode = 'hard_delete' if not sealed else 'void_only'
    elif not sealed:
        mode = 'hard_delete'
    else:
        mode = 'void_only'

    # delete_journal cascade có thể đã xóa sme_vouchers + hoàn công nợ
    still = get_voucher(conn, voucher_id)
    if not still and mode == 'hard_delete':
        if commit:
            conn.commit()
        return {
            'id': voucher_id,
            'voucher_no': voucher.get('voucher_no'),
            'voucher_type': voucher.get('voucher_type'),
            'deleted': True,
            'mode': 'hard_delete',
            'journal_mode': (rev or {}).get('mode') or 'hard_delete',
            'reversal': rev,
            'cascade_via_journal': True,
            'message': (
                f"Đã xóa bút toán và chứng từ {voucher.get('voucher_no')} "
                f"(kỳ chưa kê khai / chưa khóa sổ) — có thể ghi lại."
            ),
        }

    # Hoàn tác side-effect công nợ nếu có
    if voucher.get('voucher_type') == 'receipt' and voucher.get('source_id'):
        try:
            if (voucher.get('credit_account') or '').startswith('131'):
                from Services.sme.cong_no_ops import reverse_ar_receipt
                reverse_ar_receipt(conn, int(voucher['source_id']), float(voucher['amount']))
        except sqlite3.OperationalError:
            pass
    if voucher.get('voucher_type') == 'payment' and voucher.get('source_id'):
        try:
            if (voucher.get('debit_account') or '').startswith('331'):
                conn.execute(
                    """
                    UPDATE import
                    SET paid_amount = CASE
                        WHEN COALESCE(paid_amount, 0) - ? < 0 THEN 0
                        ELSE COALESCE(paid_amount, 0) - ?
                    END
                    WHERE id = ?
                    """,
                    (float(voucher['amount']), float(voucher['amount']), int(voucher['source_id'])),
                )
        except sqlite3.OperationalError:
            pass

    # Phân bổ nộp BH cả kỳ (nếu có)
    try:
        conn.execute(
            'DELETE FROM sme_insurance_pay_alloc WHERE voucher_id = ?',
            (voucher_id,),
        )
    except sqlite3.Error:
        pass

    if mode == 'hard_delete':
        # Xóa chứng từ — cho phép lập phiếu / bút toán mới trong kỳ mở
        conn.execute('DELETE FROM sme_vouchers WHERE id = ?', (voucher_id,))
        if commit:
            conn.commit()
        return {
            'id': voucher_id,
            'voucher_no': voucher.get('voucher_no'),
            'voucher_type': voucher.get('voucher_type'),
            'deleted': True,
            'mode': 'hard_delete',
            'journal_mode': (rev or {}).get('mode') or 'hard_delete',
            'reversal': rev,
            'message': (
                f"Đã xóa bút toán và chứng từ {voucher.get('voucher_no')} "
                f"(kỳ chưa kê khai / chưa khóa sổ) — có thể ghi lại."
            ),
        }
    # Kỳ đã chốt kê khai / khóa năm: giữ lịch sử + đảo (nếu có)
    conn.execute(
        """
        UPDATE sme_vouchers
        SET status = 'void', reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            f"{voucher.get('reason') or ''} | {reason}".strip(' |'),
            _now(),
            voucher_id,
        ),
    )
    if commit:
        conn.commit()
    out = get_voucher(conn, voucher_id) or dict(voucher)
    out['reversal'] = rev
    out['mode'] = mode
    out['deleted'] = False
    out['message'] = (
        f"Đã hủy {voucher.get('voucher_no')} và ghi bút toán đảo "
        f"(kỳ đã chốt kê khai / khóa sổ)."
        if mode == 'reverse'
        else f"Đã đánh dấu hủy {voucher.get('voucher_no')}."
    )
    return out
