"""Thư tín dụng (L/C) — ký quỹ mở L/C theo TT99 (TK 244).

Hai chế độ khởi tạo:
  - full_margin: ký quỹ 100% từ tiền gửi → Nợ 244 / Có 1122|1121
  - margin_and_loan: một phần vốn tự có + phần vay NH → Nợ 244 / Có 112 + Có 3411
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry

MONEY_Q = Decimal('0.01')
FX_Q = Decimal('0.0001')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _fx(val) -> Decimal:
    if val is None or val == '':
        return Decimal('1.0000')
    rate = Decimal(str(val))
    if rate <= 0:
        return Decimal('1.0000')
    return rate.quantize(FX_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _add_months(date_s: str, months: int) -> str:
    """Cộng số tháng vào ngày YYYY-MM-DD (giữ ngày cuối tháng nếu cần)."""
    dt = datetime.strptime(str(date_s or '')[:10], '%Y-%m-%d')
    m0 = dt.month - 1 + int(months)
    y = dt.year + m0 // 12
    m = m0 % 12 + 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return f'{y:04d}-{m:02d}-{d:02d}'


def ensure_sme_lc_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_lc_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lc_no TEXT NOT NULL UNIQUE,
            open_date TEXT NOT NULL,
            bank_name TEXT NOT NULL,
            beneficiary_name TEXT,
            currency TEXT NOT NULL DEFAULT 'USD',
            amount_fc REAL NOT NULL DEFAULT 0,
            exchange_rate REAL NOT NULL DEFAULT 1,
            amount_vnd REAL NOT NULL DEFAULT 0,
            funding_mode TEXT NOT NULL DEFAULT 'full_margin',
            margin_pct REAL NOT NULL DEFAULT 100,
            interest_rate REAL NOT NULL DEFAULT 0,
            loan_term_months INTEGER NOT NULL DEFAULT 0,
            own_amount_vnd REAL NOT NULL DEFAULT 0,
            loan_amount_vnd REAL NOT NULL DEFAULT 0,
            margin_account TEXT NOT NULL DEFAULT '244',
            cash_account TEXT NOT NULL DEFAULT '1122',
            liability_account TEXT NOT NULL DEFAULT '3411',
            loan_id INTEGER,
            import_id INTEGER,
            po_id INTEGER,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_lc_docs')
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_lc_docs)').fetchall()}
    extras = [
        ('interest_rate', 'REAL NOT NULL DEFAULT 0'),
        ('loan_term_months', 'INTEGER NOT NULL DEFAULT 0'),
        ('direction', "TEXT DEFAULT 'import'"),
        ('sale_id', 'INTEGER'),
        ('applicant_name', 'TEXT'),
    ]
    for col, decl in extras:
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE sme_lc_docs ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_lc_date ON sme_lc_docs(open_date)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_lc_status ON sme_lc_docs(status)'
    )
    # Nhật ký tất toán từng đợt chứng từ / phiếu nhập gắn L/C
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_lc_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lc_id INTEGER NOT NULL,
            import_id INTEGER NOT NULL,
            settle_date TEXT NOT NULL,
            amount_fc REAL NOT NULL DEFAULT 0,
            amount_vnd REAL NOT NULL DEFAULT 0,
            released_244 REAL NOT NULL DEFAULT 0,
            cash_shortfall REAL NOT NULL DEFAULT 0,
            journal_entry_id INTEGER,
            voucher_id INTEGER,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(import_id),
            FOREIGN KEY(lc_id) REFERENCES sme_lc_docs(id)
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_lc_settle_lc ON sme_lc_settlements(lc_id)'
    )
    if commit:
        conn.commit()


def get_lc_balance(conn: sqlite3.Connection, lc_id: int) -> dict[str, Any]:
    """Số dư L/C còn dùng cho các đợt chứng từ tiếp theo.

    Công thức:
      đã dùng NT = Σ amount_fc các lần tất toán (sme_lc_settlements)
      đã giải toả 244 = Σ released_244
      còn lại NT = face_fc − đã dùng NT
      còn lại 244 = face_vnd − đã giải toả 244
    """
    ensure_sme_lc_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_lc_docs WHERE id = ?', (lc_id,)).fetchone()
    if not row:
        raise ValueError('Không tìm thấy L/C')
    lc = dict(row)
    face_fc = _money(lc.get('amount_fc'))
    face_vnd = _money(lc.get('amount_vnd'))
    used = conn.execute(
        """
        SELECT
            COALESCE(SUM(amount_fc), 0) AS used_fc,
            COALESCE(SUM(amount_vnd), 0) AS used_vnd,
            COALESCE(SUM(released_244), 0) AS released_244,
            COALESCE(SUM(cash_shortfall), 0) AS cash_shortfall,
            COUNT(*) AS settle_count
        FROM sme_lc_settlements
        WHERE lc_id = ?
        """,
        (lc_id,),
    ).fetchone()
    used_fc = _money(used[0] if not isinstance(used, sqlite3.Row) else used['used_fc'])
    used_ap_vnd = _money(used[1] if not isinstance(used, sqlite3.Row) else used['used_vnd'])
    released_244 = _money(used[2] if not isinstance(used, sqlite3.Row) else used['released_244'])
    cash_shortfall = _money(used[3] if not isinstance(used, sqlite3.Row) else used['cash_shortfall'])
    settle_count = int(used[4] if not isinstance(used, sqlite3.Row) else used['settle_count'] or 0)

    # Fallback nếu chưa có bảng settlement nhưng LC đã settled một lần cũ
    if settle_count == 0 and (lc.get('status') == 'settled' or lc.get('settle_journal_id')):
        # Ước lượng đã dùng full face (hành vi cũ: 1 L/C = 1 phiếu)
        used_fc = face_fc
        released_244 = face_vnd
        used_ap_vnd = face_vnd
        settle_count = 1

    remain_fc = _money(face_fc - used_fc)
    remain_244 = _money(face_vnd - released_244)
    if remain_fc < 0:
        remain_fc = Decimal('0.00')
    if remain_244 < 0:
        remain_244 = Decimal('0.00')

    shipments = []
    try:
        ship_rows = conn.execute(
            """
            SELECT s.*, i.import_no
            FROM sme_lc_settlements s
            LEFT JOIN "import" i ON i.id = s.import_id
            WHERE s.lc_id = ?
            ORDER BY s.settle_date, s.id
            """,
            (lc_id,),
        ).fetchall()
        shipments = [dict(r) for r in ship_rows]
    except sqlite3.OperationalError:
        shipments = []

    return {
        'lc_id': lc_id,
        'lc_no': lc.get('lc_no'),
        'currency': lc.get('currency') or 'USD',
        'status': lc.get('status'),
        'face_fc': float(face_fc),
        'face_vnd': float(face_vnd),
        'exchange_rate': float(lc.get('exchange_rate') or 1),
        'used_fc': float(used_fc),
        'used_ap_vnd': float(used_ap_vnd),
        'released_244': float(released_244),
        'cash_shortfall_total': float(cash_shortfall),
        'remaining_fc': float(remain_fc),
        'remaining_vnd': float(remain_244),  # alias UX
        'remaining_244': float(remain_244),
        'settle_count': settle_count,
        'shipments': shipments,
        'can_link_import': (
            (lc.get('status') or '') in ('open', 'partial')
            and remain_fc > Decimal('0.00005')
        ),
    }


def enrich_lc_doc(conn: sqlite3.Connection, doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    try:
        bal = get_lc_balance(conn, int(doc['id']))
    except (ValueError, TypeError, KeyError):
        return doc
    out = dict(doc)
    out.update({
        'face_fc': bal['face_fc'],
        'used_fc': bal['used_fc'],
        'remaining_fc': bal['remaining_fc'],
        'remaining_244': bal['remaining_244'],
        'remaining_vnd': bal['remaining_vnd'],
        'released_244': bal['released_244'],
        'settle_count': bal['settle_count'],
        'can_link_import': bal['can_link_import'],
        'shipments': bal.get('shipments') or [],
    })
    return out


def refresh_lc_status_from_balance(
    conn: sqlite3.Connection,
    lc_id: int,
    *,
    commit: bool = False,
) -> str:
    """Cập nhật status: open (còn dư) / settled (hết dư)."""
    bal = get_lc_balance(conn, lc_id)
    doc = get_lc(conn, lc_id)
    if not doc or doc.get('status') == 'void':
        return doc.get('status') if doc else 'void'
    remain = _money(bal['remaining_fc'])
    new_status = 'settled' if remain <= Decimal('0.00005') else 'open'
    if new_status != doc.get('status'):
        conn.execute(
            'UPDATE sme_lc_docs SET status = ?, updated_at = ? WHERE id = ?',
            (new_status, _now(), lc_id),
        )
        if commit:
            conn.commit()
    return new_status


def record_lc_settlement(
    conn: sqlite3.Connection,
    *,
    lc_id: int,
    import_id: int,
    settle_date: str,
    amount_fc,
    amount_vnd,
    released_244,
    cash_shortfall=0,
    journal_entry_id: int | None = None,
    voucher_id: int | None = None,
    created_by: str | None = None,
) -> int:
    ensure_sme_lc_schema(conn, commit=False)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_lc_settlements (
            lc_id, import_id, settle_date, amount_fc, amount_vnd,
            released_244, cash_shortfall, journal_entry_id, voucher_id,
            created_by, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(import_id) DO UPDATE SET
            settle_date = excluded.settle_date,
            amount_fc = excluded.amount_fc,
            amount_vnd = excluded.amount_vnd,
            released_244 = excluded.released_244,
            cash_shortfall = excluded.cash_shortfall,
            journal_entry_id = excluded.journal_entry_id,
            voucher_id = excluded.voucher_id,
            created_by = excluded.created_by
        """,
        (
            lc_id, import_id, str(settle_date)[:10],
            float(_money(amount_fc)), float(_money(amount_vnd)),
            float(_money(released_244)), float(_money(cash_shortfall)),
            journal_entry_id, voucher_id, created_by, _now(),
        ),
    )
    refresh_lc_status_from_balance(conn, lc_id, commit=False)
    return cur.lastrowid


def delete_lc_settlement_for_import(
    conn: sqlite3.Connection,
    import_id: int,
) -> int | None:
    """Xóa dòng tất toán khi hủy phiếu nhập — trả lại số dư L/C."""
    ensure_sme_lc_schema(conn, commit=False)
    row = conn.execute(
        'SELECT lc_id FROM sme_lc_settlements WHERE import_id = ?',
        (import_id,),
    ).fetchone()
    if not row:
        return None
    lc_id = int(row[0] if not isinstance(row, sqlite3.Row) else row['lc_id'])
    conn.execute('DELETE FROM sme_lc_settlements WHERE import_id = ?', (import_id,))
    refresh_lc_status_from_balance(conn, lc_id, commit=False)
    return lc_id


def open_letter_of_credit(
    conn: sqlite3.Connection,
    *,
    open_date: str,
    bank_name: str,
    amount_fc,
    exchange_rate=1,
    currency: str = 'USD',
    funding_mode: str = 'full_margin',
    margin_pct=100,
    interest_rate=0,
    loan_term_months=0,
    beneficiary_name: str = '',
    lc_no: str | None = None,
    cash_account: str = '1122',
    liability_account: str = '3411',
    margin_account: str = '244',
    lender_name: str = '',
    import_id: int | None = None,
    po_id: int | None = None,
    notes: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Khởi tạo L/C + ghi sổ ký quỹ 244.

    ``interest_rate``: lãi suất năm dạng thập phân (0.12 = 12%/năm),
    dùng khi có phần vốn vay NH — lưu vào ``sme_loans`` để trích lãi.
    ``loan_term_months``: kỳ hạn vay (tháng) — tính hạn trả trên ``sme_loans.due_date``.
    """
    from Services.sme.branches import resolve_posting_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_lc_schema(conn, commit=False)

    date_s = str(open_date or '')[:10]
    bank = (bank_name or '').strip()
    if not date_s:
        raise ValueError('Thiếu ngày mở L/C')
    if not bank:
        raise ValueError('Thiếu ngân hàng phát hành L/C')

    fc = _money(amount_fc)
    if fc <= 0:
        raise ValueError('Số tiền L/C (ngoại tệ) phải > 0')

    cur_code = (currency or 'USD').strip().upper() or 'USD'
    rate = _fx(exchange_rate)
    if cur_code == 'VND':
        rate = Decimal('1.0000')

    mode = (funding_mode or 'full_margin').strip().lower()
    if mode in ('100', 'full', 'full_own', 'own'):
        mode = 'full_margin'
    if mode in ('loan', 'partial', 'margin_loan', 'with_loan'):
        mode = 'margin_and_loan'
    if mode not in ('full_margin', 'margin_and_loan'):
        raise ValueError('Chế độ L/C không hợp lệ (full_margin | margin_and_loan)')

    pct = Decimal(str(margin_pct if margin_pct is not None else 100))
    if mode == 'full_margin':
        pct = Decimal('100')
    if pct <= 0 or pct > 100:
        raise ValueError('Tỷ lệ ký quỹ phải trong khoảng (0, 100]')

    total_vnd = _money(fc * rate)
    own_vnd = _money(total_vnd * pct / Decimal('100'))
    loan_vnd = _money(total_vnd - own_vnd)
    if mode == 'full_margin':
        own_vnd = total_vnd
        loan_vnd = Decimal('0.00')
    elif loan_vnd <= 0:
        raise ValueError('Chế độ có vốn vay yêu cầu tỷ lệ ký quỹ < 100%')

    # Lãi suất năm: chấp nhận 0.12 hoặc 12 (= 12%)
    rate_raw = Decimal(str(interest_rate if interest_rate is not None else 0))
    if rate_raw < 0:
        raise ValueError('Lãi suất vốn vay NH không được âm')
    if rate_raw > 1:
        # Người dùng nhập dạng % (vd 12) → 0.12
        rate_raw = rate_raw / Decimal('100')
    if rate_raw > Decimal('1'):
        raise ValueError('Lãi suất vốn vay NH không hợp lệ')
    interest_dec = float(rate_raw.quantize(Decimal('0.0001')))
    try:
        term_months = int(loan_term_months or 0)
    except (TypeError, ValueError):
        term_months = 0
    loan_due: str | None = None
    if mode == 'full_margin':
        interest_dec = 0.0
        term_months = 0
    elif loan_vnd > 0:
        if interest_dec <= 0:
            raise ValueError('Nhập % lãi suất vốn vay NH khi ký quỹ kèm vay')
        if term_months <= 0:
            raise ValueError('Nhập thời hạn vay (kỳ hạn) theo tháng')
        if term_months > 600:
            raise ValueError('Kỳ hạn vay không hợp lệ')
        loan_due = _add_months(date_s, term_months)

    cash = (cash_account or '1122').strip() or '1122'
    if cur_code != 'VND' and cash in ('1121', '1111'):
        cash = '1122' if cash.startswith('112') else '1112'
    liab = (liability_account or '3411').strip() or '3411'
    margin = (margin_account or '244').strip() or '244'

    doc_no = (lc_no or '').strip()
    if not doc_no:
        raise ValueError('Nhập số L/C do ngân hàng cấp — hệ thống không tự tạo số L/C')
    exists = conn.execute(
        'SELECT id FROM sme_lc_docs WHERE lc_no = ?', (doc_no,)
    ).fetchone()
    if exists:
        raise ValueError(f'Số L/C {doc_no} đã tồn tại')

    beneficiary = (beneficiary_name or '').strip()
    branch = resolve_posting_branch(conn, branch_code)
    desc = notes or (
        f'Ký quỹ mở L/C {doc_no} — {bank}'
        + (f' — {beneficiary}' if beneficiary else '')
    )

    lines: list[dict[str, Any]] = [
        {
            'sequence': 1,
            'account_code': margin,
            'debit': float(total_vnd),
            'credit': 0,
            'debit_fc': float(fc) if cur_code != 'VND' else 0,
            'credit_fc': 0,
            'description': desc,
        },
    ]
    seq = 2
    if own_vnd > 0:
        own_fc = _money(fc * pct / Decimal('100')) if cur_code != 'VND' else Decimal('0')
        lines.append({
            'sequence': seq,
            'account_code': cash,
            'debit': 0,
            'credit': float(own_vnd),
            'debit_fc': 0,
            'credit_fc': float(own_fc) if cur_code != 'VND' else 0,
            'description': f'{desc} (vốn tự có {float(pct):g}%)',
        })
        seq += 1
    if loan_vnd > 0:
        loan_fc = _money(fc - (fc * pct / Decimal('100'))) if cur_code != 'VND' else Decimal('0')
        lines.append({
            'sequence': seq,
            'account_code': liab,
            'debit': 0,
            'credit': float(loan_vnd),
            'debit_fc': 0,
            'credit_fc': float(loan_fc) if cur_code != 'VND' else 0,
            'description': f'{desc} (vay NH {float(100 - pct):g}%)',
        })

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='LC',
        document_no=doc_no,
        document_id=import_id,
        business_type='MO_LC',
        currency=cur_code,
        exchange_rate=float(rate),
        description=desc,
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )

    loan_id = None
    if loan_vnd > 0:
        from Services.sme.loans_deposits import ensure_sme_loans_schema, _next_loan_no
        ensure_sme_loans_schema(conn, commit=False)
        loan_no = _next_loan_no(conn)
        lender = (lender_name or bank).strip() or bank
        loan_cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_loans)').fetchall()}
        cur = conn.cursor()
        loan_note = (
            f'Vay kèm mở L/C {doc_no} — LS {interest_dec * 100:g}%/năm'
            f' · kỳ hạn {term_months} tháng'
            + (f' · hạn {loan_due}' if loan_due else '')
        )
        if 'currency' in loan_cols:
            cur.execute(
                """
                INSERT INTO sme_loans (
                    loan_no, lender_name, contract_no, start_date, due_date, principal,
                    interest_rate, liability_account, currency, status,
                    disbursement_journal_id, notes, created_by, created_at, branch_code
                ) VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?)
                """,
                (
                    loan_no, lender, doc_no, date_s, loan_due, float(loan_vnd),
                    interest_dec, liab, cur_code, entry['id'],
                    loan_note, created_by, _now(), branch,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO sme_loans (
                    loan_no, lender_name, contract_no, start_date, due_date, principal,
                    interest_rate, liability_account, status,
                    disbursement_journal_id, notes, created_by, created_at, branch_code
                ) VALUES (?,?,?,?,?,?,?,?,'active',?,?,?,?,?)
                """,
                (
                    loan_no, lender, doc_no, date_s, loan_due, float(loan_vnd),
                    interest_dec, liab, entry['id'],
                    loan_note, created_by, _now(), branch,
                ),
            )
        loan_id = cur.lastrowid

    cur = conn.cursor()
    lc_cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_lc_docs)').fetchall()}
    has_rate = 'interest_rate' in lc_cols
    has_term = 'loan_term_months' in lc_cols
    if has_rate and has_term:
        cur.execute(
            """
            INSERT INTO sme_lc_docs (
                lc_no, open_date, bank_name, beneficiary_name, currency,
                amount_fc, exchange_rate, amount_vnd, funding_mode, margin_pct,
                interest_rate, loan_term_months, own_amount_vnd, loan_amount_vnd,
                margin_account, cash_account,
                liability_account, loan_id, import_id, po_id, journal_entry_id,
                status, notes, created_by, created_at, updated_at, branch_code
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?)
            """,
            (
                doc_no, date_s, bank, beneficiary or None, cur_code,
                float(fc), float(rate), float(total_vnd), mode, float(pct),
                interest_dec, term_months, float(own_vnd), float(loan_vnd),
                margin, cash, liab,
                loan_id, import_id, po_id, entry['id'],
                notes or '', created_by, _now(), _now(), branch,
            ),
        )
    elif has_rate:
        cur.execute(
            """
            INSERT INTO sme_lc_docs (
                lc_no, open_date, bank_name, beneficiary_name, currency,
                amount_fc, exchange_rate, amount_vnd, funding_mode, margin_pct,
                interest_rate, own_amount_vnd, loan_amount_vnd, margin_account, cash_account,
                liability_account, loan_id, import_id, po_id, journal_entry_id,
                status, notes, created_by, created_at, updated_at, branch_code
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?)
            """,
            (
                doc_no, date_s, bank, beneficiary or None, cur_code,
                float(fc), float(rate), float(total_vnd), mode, float(pct),
                interest_dec, float(own_vnd), float(loan_vnd), margin, cash, liab,
                loan_id, import_id, po_id, entry['id'],
                notes or '', created_by, _now(), _now(), branch,
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO sme_lc_docs (
                lc_no, open_date, bank_name, beneficiary_name, currency,
                amount_fc, exchange_rate, amount_vnd, funding_mode, margin_pct,
                own_amount_vnd, loan_amount_vnd, margin_account, cash_account,
                liability_account, loan_id, import_id, po_id, journal_entry_id,
                status, notes, created_by, created_at, updated_at, branch_code
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?)
            """,
            (
                doc_no, date_s, bank, beneficiary or None, cur_code,
                float(fc), float(rate), float(total_vnd), mode, float(pct),
                float(own_vnd), float(loan_vnd), margin, cash, liab,
                loan_id, import_id, po_id, entry['id'],
                notes or '', created_by, _now(), _now(), branch,
            ),
        )
    lc_id = cur.lastrowid
    # Mặc định L/C mở ký quỹ = nhập khẩu
    try:
        if 'direction' in {r[1] for r in conn.execute('PRAGMA table_info(sme_lc_docs)').fetchall()}:
            conn.execute(
                "UPDATE sme_lc_docs SET direction = 'import' WHERE id = ? AND COALESCE(direction,'') = ''",
                (lc_id,),
            )
    except sqlite3.OperationalError:
        pass
    if commit:
        conn.commit()
    return get_lc(conn, lc_id)


def open_export_letter_of_credit(
    conn: sqlite3.Connection,
    *,
    open_date: str,
    bank_name: str,
    amount_fc,
    exchange_rate=1,
    currency: str = 'USD',
    beneficiary_name: str = '',
    applicant_name: str = '',
    lc_no: str | None = None,
    notes: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Đăng ký L/C xuất (theo dõi hạn mức) — không ghi ký quỹ 244.

    Beneficiary = doanh nghiệp VN (người thụ hưởng). Không phát sinh Nợ 244.
    """
    from Services.sme.branches import resolve_posting_branch

    ensure_sme_lc_schema(conn, commit=False)
    date_s = str(open_date or '')[:10]
    bank = (bank_name or '').strip()
    if not date_s:
        raise ValueError('Thiếu ngày mở / nhận thông báo L/C')
    if not bank:
        raise ValueError('Thiếu ngân hàng thông báo / phát hành L/C')
    fc = _money(amount_fc)
    if fc <= 0:
        raise ValueError('Mệnh giá L/C (NT) phải > 0')
    cur_code = (currency or 'USD').strip().upper() or 'USD'
    rate = _fx(exchange_rate)
    total_vnd = _money(fc * rate)
    branch = resolve_posting_branch(conn, branch_code)
    doc_no = (lc_no or '').strip() or f'LCX-{date_s.replace("-", "")}-{int(fc)}'
    existing = conn.execute(
        'SELECT id FROM sme_lc_docs WHERE lc_no = ?', (doc_no,),
    ).fetchone()
    if existing:
        raise ValueError(f'Số L/C {doc_no} đã tồn tại')

    beneficiary = (beneficiary_name or '').strip()
    applicant = (applicant_name or '').strip()
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_lc_docs)').fetchall()}
    fields = [
        'lc_no', 'open_date', 'bank_name', 'beneficiary_name', 'currency',
        'amount_fc', 'exchange_rate', 'amount_vnd', 'funding_mode', 'margin_pct',
        'own_amount_vnd', 'loan_amount_vnd', 'status', 'notes',
        'created_by', 'created_at', 'updated_at', 'branch_code',
    ]
    vals: list[Any] = [
        doc_no, date_s, bank, beneficiary or None, cur_code,
        float(fc), float(rate), float(total_vnd), 'export_track', 0,
        0, 0, 'open', notes or 'L/C xuất khẩu',
        created_by, _now(), _now(), branch,
    ]
    if 'direction' in cols:
        fields.append('direction')
        vals.append('export')
    if 'applicant_name' in cols:
        fields.append('applicant_name')
        vals.append(applicant or None)
    conn.execute(
        f"INSERT INTO sme_lc_docs ({', '.join(fields)}) VALUES ({', '.join(['?']*len(vals))})",
        vals,
    )
    lc_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    if commit:
        conn.commit()
    return get_lc(conn, int(lc_id))


def get_lc(conn: sqlite3.Connection, lc_id: int) -> dict[str, Any] | None:
    ensure_sme_lc_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_lc_docs WHERE id = ?', (lc_id,)).fetchone()
    return enrich_lc_doc(conn, dict(row) if row else None)


def list_lc_docs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    branch_code: str | None = None,
    direction: str | None = None,
    limit: int = 200,
    with_balance: bool = True,
    only_linkable: bool = False,
) -> list[dict[str, Any]]:
    ensure_sme_lc_schema(conn, commit=False)
    from Services.sme.branches import branch_sql_filter

    sql = 'SELECT * FROM sme_lc_docs v WHERE 1=1'
    params: list[Any] = []
    if status:
        sql += ' AND v.status = ?'
        params.append(status)
    else:
        sql += " AND v.status != 'void'"
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_lc_docs)').fetchall()}
    if direction and 'direction' in cols:
        d = direction.strip().lower()
        if d in ('export', 'exp', 'xk'):
            sql += " AND LOWER(COALESCE(v.direction,'import')) = 'export'"
        elif d in ('import', 'imp', 'nk'):
            sql += " AND LOWER(COALESCE(v.direction,'import')) = 'import'"
    bf, bp = branch_sql_filter(branch_code, alias='v')
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY v.open_date DESC, v.id DESC LIMIT ?'
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out = [dict(r) for r in rows]
    if with_balance:
        out = [enrich_lc_doc(conn, d) or d for d in out]
    if only_linkable:
        out = [d for d in out if d.get('can_link_import')]
    return out


def settle_lc(
    conn: sqlite3.Connection,
    lc_id: int,
    *,
    import_id: int | None = None,
    settle_date: str | None = None,
    shortfall_exchange_rate=None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Tất toán L/C gắn phiếu nhập — ủy quyền ``import_settle.settle_import_by_lc``.

    Một L/C có thể gắn nhiều phiếu nhập (nhiều đợt chứng từ). Hàm này chọn
    phiếu chưa tất toán cũ nhất nếu không truyền ``import_id``.
    """
    from Services.sme.import_settle import settle_import_by_lc

    doc = get_lc(conn, lc_id)
    if not doc:
        raise ValueError('Không tìm thấy L/C')
    if doc.get('status') == 'void':
        raise ValueError('L/C đã hủy')
    if _money(doc.get('remaining_fc')) <= 0:
        raise ValueError('L/C đã hết số dư — không còn để tất toán đợt tiếp theo')

    target_import = import_id or doc.get('import_id')
    if not target_import:
        row = conn.execute(
            """
            SELECT id FROM "import"
            WHERE linked_lc_id = ?
              AND COALESCE(settle_journal_id, 0) = 0
            ORDER BY id ASC
            LIMIT 1
            """,
            (lc_id,),
        ).fetchone()
        if row:
            target_import = row[0] if not isinstance(row, sqlite3.Row) else row['id']
    if not target_import:
        raise ValueError(
            'Không có phiếu nhập chưa tất toán gắn L/C này — '
            'mở phiếu NK chọn L/C (còn số dư) rồi tất toán từng đợt'
        )
    return settle_import_by_lc(
        conn,
        int(target_import),
        settle_date=settle_date,
        shortfall_exchange_rate=shortfall_exchange_rate,
        created_by=created_by,
        commit=commit,
    )


def void_lc(
    conn: sqlite3.Connection,
    lc_id: int,
    *,
    reason: str = 'Hủy mở L/C',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    ensure_sme_lc_schema(conn, commit=False)
    doc = get_lc(conn, lc_id)
    if not doc:
        raise ValueError('Không tìm thấy L/C')
    if doc.get('status') == 'void':
        raise ValueError('L/C đã hủy')
    settle_count = int(doc.get('settle_count') or 0)
    if settle_count > 0 or doc.get('status') == 'settled':
        raise ValueError(
            'L/C đã có đợt tất toán chứng từ — không hủy trực tiếp. '
            'Hãy hủy phiếu nhập tương ứng để hoàn số dư L/C trước.'
        )

    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_lc_docs', lc_id, label='L/C')

    rev = None
    if doc.get('journal_entry_id'):
        rev = reverse_journal_entry(
            conn,
            int(doc['journal_entry_id']),
            created_by=created_by,
            reason=reason,
        )
    conn.execute(
        """
        UPDATE sme_lc_docs
        SET status = 'void', notes = COALESCE(notes,'') || ?, updated_at = ?
        WHERE id = ?
        """,
        (f'\n[Hủy] {reason}', _now(), lc_id),
    )
    if doc.get('loan_id'):
        try:
            conn.execute(
                "UPDATE sme_loans SET status = 'void', notes = COALESCE(notes,'') || ? WHERE id = ?",
                (f'\n[Hủy theo L/C {doc.get("lc_no")}]', int(doc['loan_id'])),
            )
        except sqlite3.OperationalError:
            pass
    if commit:
        conn.commit()
    return {'id': lc_id, 'status': 'void', 'reversal': rev}
