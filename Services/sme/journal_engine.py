"""Engine bút toán kép SME — validate COA, cân Nợ/Có, cập nhật số dư kỳ."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.coa_service import ensure_sme_coa_ready, get_account
from Services.sme.journal_schema import ensure_sme_journal_schema
from Services.sme.posting_rules_seed import DEFAULT_POSTING_RULES, RULES_SEED_VERSION

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_journal_ready(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    ensure_sme_coa_ready(conn, commit=commit)
    ensure_sme_journal_schema(conn, commit=commit)
    return seed_posting_rules(conn, commit=commit)


def seed_posting_rules(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    c = conn.cursor()
    row = c.execute(
        "SELECT value FROM sme_journal_seed_meta WHERE key = 'rules_version'"
    ).fetchone()
    current = row[0] if row else None
    count = c.execute("SELECT COUNT(*) FROM sme_posting_rules").fetchone()[0]
    if not force and current == RULES_SEED_VERSION and count > 0:
        return {'seeded': False, 'rules_version': current, 'count': count}

    inserted = 0
    for rule in DEFAULT_POSTING_RULES:
        # Bỏ qua nếu mã TK chưa có trong COA (tránh FK lỗi trên DB trống)
        for key in ('debit_account_code', 'credit_account_code', 'vat_account_code', 'import_tax_credit_account'):
            code = rule.get(key)
            if code and not get_account(conn, code, commit=commit):
                raise ValueError(f"Seed rule thiếu tài khoản {code} trong COA — chạy ensure_sme_coa_ready trước")

        exists = c.execute(
            """
            SELECT id FROM sme_posting_rules
            WHERE business_type = ? AND payment_method = ?
            """,
            (rule['business_type'], rule['payment_method']),
        ).fetchone()
        if exists:
            c.execute(
                """
                UPDATE sme_posting_rules SET
                    debit_account_code = ?, credit_account_code = ?,
                    vat_account_code = ?, import_tax_credit_account = ?,
                    is_vat_applicable = ?, active = 1, description = ?
                WHERE business_type = ? AND payment_method = ?
                """,
                (
                    rule['debit_account_code'], rule['credit_account_code'],
                    rule.get('vat_account_code'), rule.get('import_tax_credit_account'),
                    int(rule.get('is_vat_applicable', 1)), rule.get('description'),
                    rule['business_type'], rule['payment_method'],
                ),
            )
        else:
            c.execute(
                """
                INSERT INTO sme_posting_rules (
                    business_type, payment_method, debit_account_code, credit_account_code,
                    vat_account_code, import_tax_credit_account, is_vat_applicable, active, description
                ) VALUES (?,?,?,?,?,?,?,1,?)
                """,
                (
                    rule['business_type'], rule['payment_method'],
                    rule['debit_account_code'], rule['credit_account_code'],
                    rule.get('vat_account_code'), rule.get('import_tax_credit_account'),
                    int(rule.get('is_vat_applicable', 1)), rule.get('description'),
                ),
            )
            inserted += 1

    c.execute(
        """
        INSERT INTO sme_journal_seed_meta(key, value, updated_at) VALUES ('rules_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (RULES_SEED_VERSION, _now()),
    )
    if commit:
        conn.commit()
    count = c.execute("SELECT COUNT(*) FROM sme_posting_rules").fetchone()[0]
    return {'seeded': True, 'rules_version': RULES_SEED_VERSION, 'inserted': inserted, 'count': count}


def get_posting_rule(
    conn: sqlite3.Connection,
    business_type: str,
    payment_method: str,
    *,
    commit: bool = True,
) -> dict | None:
    ensure_sme_journal_ready(conn, commit=commit)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT * FROM sme_posting_rules
        WHERE business_type = ? AND payment_method = ? AND active = 1
        """,
        (business_type, payment_method),
    ).fetchone()
    return dict(row) if row else None


def resolve_postable_account(conn: sqlite3.Connection, code: str) -> str:
    """
    Trả về mã TK được phép ghi sổ.
    Nếu TK là tổng hợp (có con), lỗi — bắt buộc chọn TK chi tiết.
    """
    acc = get_account(conn, code, commit=False)
    if not acc or not acc.get('is_active'):
        raise ValueError(f'Tài khoản {code} không tồn tại hoặc đã ngừng sử dụng')
    if not acc.get('is_postable'):
        raise ValueError(
            f'Tài khoản {code} là tài khoản tổng hợp — hãy chọn tài khoản con để ghi sổ '
            f'(ví dụ mở chi tiết dưới {code})'
        )
    return acc['code']


def _next_entry_no(
    conn: sqlite3.Connection,
    posting_date: str = '',
    document_type: str = '',
) -> str:
    """Đánh số bút toán liên tục: BT0000001, BT0000002, …"""
    # posting_date / document_type giữ để tương thích chữ ký cũ; không đưa vào số BT.
    _ = (posting_date, document_type)
    prefix = 'BT'
    width = 7
    row = conn.execute(
        """
        SELECT entry_no FROM sme_journal_entries
        WHERE entry_no GLOB 'BT[0-9]*'
          AND length(entry_no) = ?
          AND substr(entry_no, 3) GLOB '[0-9]*'
        ORDER BY CAST(substr(entry_no, 3) AS INTEGER) DESC
        LIMIT 1
        """,
        (len(prefix) + width,),
    ).fetchone()
    seq = 1
    if row and row[0]:
        tail = str(row[0])[len(prefix):]
        if tail.isdigit():
            seq = int(tail) + 1
    return f"{prefix}{seq:0{width}d}"


def _apply_balance_delta(
    c: sqlite3.Cursor,
    *,
    account_code: str,
    fiscal_year: int,
    period: int,
    debit: Decimal,
    credit: Decimal,
    sign: int = 1,
) -> None:
    """Cộng/trừ phát sinh kỳ vào sme_account_balances (sign=-1 khi đảo)."""
    d = float(debit * sign)
    cr = float(credit * sign)
    c.execute(
        """
        INSERT INTO sme_account_balances (
            account_code, fiscal_year, period,
            period_debit, period_credit, closing_debit, closing_credit
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_code, fiscal_year, period) DO UPDATE SET
            period_debit = period_debit + excluded.period_debit,
            period_credit = period_credit + excluded.period_credit,
            closing_debit = closing_debit + excluded.closing_debit,
            closing_credit = closing_credit + excluded.closing_credit
        """,
        (account_code, fiscal_year, period, d, cr, d, cr),
    )


def post_journal_entry(
    conn: sqlite3.Connection,
    *,
    posting_date: str,
    document_type: str,
    lines: list[dict],
    document_date: str | None = None,
    document_no: str | None = None,
    document_id: int | None = None,
    business_type: str | None = None,
    currency: str = 'VND',
    exchange_rate: float | Decimal = 1,
    description: str = '',
    reference_document: str | None = None,
    created_by: str | None = None,
    entry_uuid: str | None = None,
    branch_code: str | None = None,
) -> dict:
    """
    Ghi một chứng từ kế toán (nhiều dòng Nợ/Có).
    lines[]: account_code, debit, credit, + optional partner/product/tax/description...
    branch_code: chi nhánh (mặc định session / HQ) — không tách pháp nhân.
    """
    # Không được commit ngầm: caller có thể đang ghi kho/chứng từ cùng transaction.
    ensure_sme_journal_ready(conn, commit=False)
    if not lines:
        raise ValueError('Bút toán phải có ít nhất một dòng')

    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, branch_code)

    prepared: list[dict] = []
    total_debit = Decimal('0.00')
    total_credit = Decimal('0.00')
    rate = _money(exchange_rate) if exchange_rate else Decimal('1.00')
    if rate <= 0:
        rate = Decimal('1.00')

    for i, raw in enumerate(lines, start=1):
        code = resolve_postable_account(conn, str(raw.get('account_code') or '').strip())
        debit = _money(raw.get('debit', 0))
        credit = _money(raw.get('credit', 0))
        if debit < 0 or credit < 0:
            raise ValueError('Số tiền Nợ/Có không được âm')
        if debit > 0 and credit > 0:
            raise ValueError(f'Dòng {i} ({code}): không được vừa Nợ vừa Có')
        if debit == 0 and credit == 0:
            continue
        total_debit += debit
        total_credit += credit
        debit_fc = _money(raw.get('debit_fc', debit if currency != 'VND' else 0))
        credit_fc = _money(raw.get('credit_fc', credit if currency != 'VND' else 0))
        prepared.append({
            'sequence': int(raw.get('sequence') or i),
            'account_code': code,
            'debit': debit,
            'credit': credit,
            'currency': raw.get('currency') or currency,
            'exchange_rate': float(raw.get('exchange_rate') or rate),
            'debit_fc': debit_fc,
            'credit_fc': credit_fc,
            'partner_id': raw.get('partner_id'),
            'partner_type': raw.get('partner_type'),
            'warehouse_code': raw.get('warehouse_code'),
            'product_id': raw.get('product_id'),
            'employee_id': raw.get('employee_id'),
            'project_code': raw.get('project_code'),
            'department_code': raw.get('department_code'),
            'tax_code': raw.get('tax_code'),
            'tax_rate': raw.get('tax_rate'),
            'vat_invoice_no': raw.get('vat_invoice_no'),
            'description': raw.get('description') or description,
        })

    if not prepared:
        raise ValueError('Không có dòng bút toán hợp lệ (số tiền > 0)')
    if total_debit != total_credit:
        raise ValueError(
            f'Bút toán không cân: Nợ {total_debit} ≠ Có {total_credit}'
        )

    try:
        dt = datetime.strptime(posting_date[:10], '%Y-%m-%d')
    except ValueError as exc:
        raise ValueError('posting_date phải dạng YYYY-MM-DD') from exc

    fiscal_year, period = dt.year, dt.month
    from Services.sme.period_lock import assert_period_open
    assert_period_open(conn, fiscal_year, period, action='ghi bút toán')

    entry_uuid = entry_uuid or str(uuid.uuid4())
    entry_no = _next_entry_no(conn, posting_date[:10], document_type or 'KT')

    c = conn.cursor()
    je_cols = {r[1] for r in c.execute('PRAGMA table_info(sme_journal_entries)').fetchall()}
    if 'branch_code' in je_cols:
        c.execute(
            """
            INSERT INTO sme_journal_entries (
                entry_uuid, entry_no, fiscal_year, period, posting_date, document_date,
                document_type, document_no, document_id, business_type, currency, exchange_rate,
                description, reference_document, status, total_debit, total_credit,
                created_by, created_at, updated_at, branch_code
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'posted',?,?,?,?,?,?)
            """,
            (
                entry_uuid, entry_no, fiscal_year, period, posting_date[:10],
                (document_date or posting_date)[:10] if document_date or posting_date else None,
                document_type, document_no, document_id, business_type,
                currency, float(rate), description, reference_document,
                float(total_debit), float(total_credit), created_by, _now(), _now(),
                branch,
            ),
        )
    else:
        c.execute(
            """
            INSERT INTO sme_journal_entries (
                entry_uuid, entry_no, fiscal_year, period, posting_date, document_date,
                document_type, document_no, document_id, business_type, currency, exchange_rate,
                description, reference_document, status, total_debit, total_credit,
                created_by, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'posted',?,?,?,?,?)
            """,
            (
                entry_uuid, entry_no, fiscal_year, period, posting_date[:10],
                (document_date or posting_date)[:10] if document_date or posting_date else None,
                document_type, document_no, document_id, business_type,
                currency, float(rate), description, reference_document,
                float(total_debit), float(total_credit), created_by, _now(), _now(),
            ),
        )
    entry_id = c.lastrowid

    for line in prepared:
        c.execute(
            """
            INSERT INTO sme_journal_lines (
                entry_id, sequence, account_code, debit, credit, currency, exchange_rate,
                debit_fc, credit_fc, partner_id, partner_type, warehouse_code, product_id,
                employee_id, project_code, department_code, tax_code, tax_rate,
                vat_invoice_no, description
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                entry_id, line['sequence'], line['account_code'],
                float(line['debit']), float(line['credit']),
                line['currency'], line['exchange_rate'],
                float(line['debit_fc']), float(line['credit_fc']),
                line['partner_id'], line['partner_type'], line['warehouse_code'],
                line['product_id'], line['employee_id'], line['project_code'],
                line['department_code'], line['tax_code'], line['tax_rate'],
                line['vat_invoice_no'], line['description'],
            ),
        )
        _apply_balance_delta(
            c,
            account_code=line['account_code'],
            fiscal_year=fiscal_year,
            period=period,
            debit=line['debit'],
            credit=line['credit'],
            sign=1,
        )

    return {
        'id': entry_id,
        'entry_uuid': entry_uuid,
        'entry_no': entry_no,
        'total_debit': float(total_debit),
        'total_credit': float(total_credit),
        'line_count': len(prepared),
        'fiscal_year': fiscal_year,
        'period': period,
        'branch_code': branch,
    }


def reverse_journal_entry(
    conn: sqlite3.Connection,
    entry_id: int,
    *,
    posting_date: str | None = None,
    created_by: str | None = None,
    reason: str = 'Đảo bút toán',
) -> dict:
    """Đảo bút toán đã ghi (tạo chứng từ đảo, không xóa lịch sử)."""
    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    entry = conn.execute(
        "SELECT * FROM sme_journal_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if not entry:
        raise ValueError(f'Không tìm thấy bút toán #{entry_id}')
    if entry['status'] == 'reversed':
        raise ValueError('Bút toán đã bị đảo trước đó')
    if entry['reversed_by_id']:
        raise ValueError('Bút toán đã có chứng từ đảo')

    from Services.sme.period_lock import assert_period_open
    assert_period_open(
        conn, int(entry['fiscal_year']), int(entry['period']),
        action='đảo bút toán',
    )
    rev_date = (posting_date or entry['posting_date'] or '')[:10]
    if rev_date:
        try:
            rdt = datetime.strptime(rev_date, '%Y-%m-%d')
            assert_period_open(conn, rdt.year, rdt.month, action='ghi bút toán đảo')
        except ValueError:
            pass

    lines = conn.execute(
        "SELECT * FROM sme_journal_lines WHERE entry_id = ? ORDER BY sequence",
        (entry_id,),
    ).fetchall()
    rev_lines = []
    for ln in lines:
        rev_lines.append({
            'account_code': ln['account_code'],
            'debit': ln['credit'],
            'credit': ln['debit'],
            'currency': ln['currency'],
            'exchange_rate': ln['exchange_rate'],
            'debit_fc': ln['credit_fc'],
            'credit_fc': ln['debit_fc'],
            'partner_id': ln['partner_id'],
            'partner_type': ln['partner_type'],
            'warehouse_code': ln['warehouse_code'],
            'product_id': ln['product_id'],
            'employee_id': ln['employee_id'],
            'project_code': ln['project_code'],
            'department_code': ln['department_code'],
            'tax_code': ln['tax_code'],
            'tax_rate': ln['tax_rate'],
            'vat_invoice_no': ln['vat_invoice_no'],
            'description': f"{reason}: {ln['description'] or ''}".strip(),
        })

    date = (posting_date or entry['posting_date'])[:10]
    try:
        orig_branch = entry['branch_code']
    except (KeyError, IndexError):
        orig_branch = None
    rev = post_journal_entry(
        conn,
        posting_date=date,
        document_type=f"REV_{entry['document_type']}",
        document_date=date,
        document_no=entry['document_no'],
        document_id=entry['document_id'],
        business_type=entry['business_type'],
        currency=entry['currency'],
        exchange_rate=entry['exchange_rate'],
        description=f"{reason} — {entry['entry_no']}",
        reference_document=entry['entry_no'],
        created_by=created_by,
        branch_code=orig_branch,
        lines=rev_lines,
    )
    conn.execute(
        """
        UPDATE sme_journal_entries
        SET status = 'reversed', reversed_by_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (rev['id'], _now(), entry_id),
    )
    conn.execute(
        "UPDATE sme_journal_entries SET reverses_id = ?, updated_at = ? WHERE id = ?",
        (entry_id, _now(), rev['id']),
    )
    return rev


def build_import_stock_lines(
    conn: sqlite3.Connection,
    *,
    business_type: str,
    payment_method: str,
    inventory_lines: list[dict],
    vat_amount: Decimal | float = 0,
    import_tax_amount: Decimal | float = 0,
    payable_amount: Decimal | float | None = None,
    supplier_id: int | None = None,
    bill_no: str | None = None,
    tax_code: str | None = None,
    import_type: str = 'DOMESTIC',
    description: str = '',
) -> tuple[dict, list[dict]]:
    """
    Dựng dòng bút toán nhập kho từ quy tắc + COA.
    inventory_lines: [{product_id, amount, product_name, tax_pct, warehouse_code?}, ...]
    amount = nguyên giá nhập kho (không VAT).
    """
    rule = get_posting_rule(conn, business_type, payment_method, commit=False)
    if not rule:
        raise ValueError(
            f'Chưa cấu hình quy tắc định khoản cho {business_type} / {payment_method}. '
            f'Vào seed sme_posting_rules hoặc tạo quy tắc mới.'
        )

    debit_inv = resolve_postable_account(conn, rule['debit_account_code'])
    credit_pay = resolve_postable_account(conn, rule['credit_account_code'])

    lines: list[dict] = []
    seq = 1
    inv_total = Decimal('0.00')
    for item in inventory_lines:
        amt = _money(item.get('amount', 0))
        if amt <= 0:
            continue
        inv_total += amt
        lines.append({
            'sequence': seq,
            'account_code': debit_inv,
            'debit': amt,
            'credit': 0,
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'product_id': item.get('product_id'),
            'warehouse_code': item.get('warehouse_code'),
            'tax_rate': item.get('tax_pct'),
            'vat_invoice_no': bill_no,
            'tax_code': tax_code,
            'description': item.get('description') or (
                f"Nhập kho: {item.get('product_name') or item.get('product_id')}"
            ),
        })
        seq += 1

    vat_amt = _money(vat_amount)
    if rule.get('is_vat_applicable') and vat_amt > 0:
        vat_code = rule.get('vat_account_code') or '13311'
        if import_type == 'IMPORT':
            # Ưu tiên 13312 nếu có trong COA
            prefer = '13312'
            preferred_account = get_account(conn, prefer, commit=False)
            if preferred_account and preferred_account.get('is_postable'):
                vat_code = prefer
        vat_code = resolve_postable_account(conn, vat_code)
        lines.append({
            'sequence': seq,
            'account_code': vat_code,
            'debit': vat_amt,
            'credit': 0,
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'vat_invoice_no': bill_no,
            'tax_code': tax_code,
            'description': f"Thuế GTGT đầu vào — {description or business_type}",
        })
        seq += 1

    import_tax = _money(import_tax_amount)
    if import_type == 'IMPORT' and import_tax > 0:
        tax_credit = rule.get('import_tax_credit_account') or '3333'
        tax_credit = resolve_postable_account(conn, tax_credit)
        # Thuế NK tăng nguyên giá kho (đã nằm trong inventory amount) và Có 3333
        # Phần Có 3333 sẽ được gộp vào tổng thanh toán/đối ứng bên dưới nếu cần tách.
        # Ở đây: Có 3333 = import_tax; phần thanh toán NCC = payable - không gồm thuế NK phải nộp NSNN ngay.
        lines.append({
            'sequence': seq,
            'account_code': tax_credit,
            'debit': 0,
            'credit': import_tax,
            'partner_type': 'tax',
            'description': 'Thuế nhập khẩu phải nộp NSNN',
        })
        seq += 1

    # Bên Có đối ứng (tiền / công nợ) = tổng Nợ kho + VAT (không gồm dòng Có thuế NK)
    debit_sum = sum((_money(x['debit']) for x in lines), Decimal('0.00'))
    credit_sum = sum((_money(x['credit']) for x in lines), Decimal('0.00'))
    payable = _money(payable_amount) if payable_amount is not None else (debit_sum - credit_sum)
    if payable < 0:
        raise ValueError('Số tiền đối ứng không hợp lệ')
    if payable > 0:
        lines.append({
            'sequence': seq,
            'account_code': credit_pay,
            'debit': 0,
            'credit': payable,
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'vat_invoice_no': bill_no,
            'description': f"Đối ứng thanh toán/công nợ — {description or business_type}",
        })

    return rule, lines


def build_return_import_stock_lines(
    conn: sqlite3.Connection,
    *,
    business_type: str,
    payment_method: str,
    inventory_lines: list[dict],
    vat_amount: Decimal | float = 0,
    refund_amount: Decimal | float | None = None,
    supplier_id: int | None = None,
    bill_no: str | None = None,
    tax_code: str | None = None,
    description: str = '',
) -> tuple[dict, list[dict]]:
    """
    Đảo bút toán nhập kho khi trả hàng NCC.
    Nợ 111/112/331 — Có 156/152 — Có 1331 (giảm VAT đầu vào).
    Dùng cùng quy tắc NHAP_KHO_* để lấy mã TK, rồi đảo bên Nợ/Có.
    """
    rule = get_posting_rule(conn, business_type, payment_method, commit=False)
    if not rule:
        raise ValueError(
            f'Chưa cấu hình quy tắc định khoản cho {business_type} / {payment_method}.'
        )

    credit_inv = resolve_postable_account(conn, rule['debit_account_code'])
    debit_pay = resolve_postable_account(conn, rule['credit_account_code'])

    lines: list[dict] = []
    seq = 1
    for item in inventory_lines:
        amt = _money(item.get('amount', 0))
        if amt <= 0:
            continue
        lines.append({
            'sequence': seq,
            'account_code': credit_inv,
            'debit': 0,
            'credit': amt,
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'product_id': item.get('product_id'),
            'warehouse_code': item.get('warehouse_code'),
            'tax_rate': item.get('tax_pct'),
            'vat_invoice_no': bill_no,
            'tax_code': tax_code,
            'description': item.get('description') or (
                f"Trả NCC: {item.get('product_name') or item.get('product_id')}"
            ),
        })
        seq += 1

    vat_amt = _money(vat_amount)
    if rule.get('is_vat_applicable') and vat_amt > 0:
        vat_code = resolve_postable_account(
            conn, rule.get('vat_account_code') or '13311'
        )
        lines.append({
            'sequence': seq,
            'account_code': vat_code,
            'debit': 0,
            'credit': vat_amt,
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'vat_invoice_no': bill_no,
            'tax_code': tax_code,
            'description': f"Giảm VAT đầu vào — {description or business_type}",
        })
        seq += 1

    credit_sum = sum((_money(x['credit']) for x in lines), Decimal('0.00'))
    refund = _money(refund_amount) if refund_amount is not None else credit_sum
    if refund < 0:
        raise ValueError('Số tiền hoàn trả không hợp lệ')
    if refund > 0:
        lines.append({
            'sequence': seq,
            'account_code': debit_pay,
            'debit': refund,
            'credit': 0,
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'vat_invoice_no': bill_no,
            'description': f"NCC hoàn tiền/giảm công nợ — {description or business_type}",
        })

    return rule, lines


def get_journal_entry(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    ensure_sme_journal_ready(conn)
    conn.row_factory = sqlite3.Row
    entry = conn.execute(
        "SELECT * FROM sme_journal_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if not entry:
        return None
    lines = conn.execute(
        "SELECT * FROM sme_journal_lines WHERE entry_id = ? ORDER BY sequence",
        (entry_id,),
    ).fetchall()
    data = dict(entry)
    data['lines'] = [dict(x) for x in lines]
    return data


def list_journal_entries(
    conn: sqlite3.Connection,
    *,
    document_type: str | None = None,
    document_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    branch_code: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    from Services.sme.branches import branch_sql_filter

    ensure_sme_journal_ready(conn)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM sme_journal_entries je WHERE 1=1"
    params: list[Any] = []
    if document_type:
        sql += " AND je.document_type = ?"
        params.append(document_type)
    if document_id is not None:
        sql += " AND je.document_id = ?"
        params.append(document_id)
    if status:
        sql += " AND je.status = ?"
        params.append(status)
    if date_from:
        sql += " AND je.posting_date >= ?"
        params.append(date_from[:10])
    if date_to:
        sql += " AND je.posting_date <= ?"
        params.append(date_to[:10])
    if q:
        like = f"%{q.strip()}%"
        sql += """
            AND (
                je.entry_no LIKE ? OR je.document_no LIKE ?
                OR je.description LIKE ? OR je.reference_document LIKE ?
                OR je.business_type LIKE ? OR je.document_type LIKE ?
                OR IFNULL(je.branch_code,'') LIKE ?
            )
        """
        params.extend([like, like, like, like, like, like, like])
    bf, bp = branch_sql_filter(branch_code, alias='je')
    sql += bf
    params.extend(bp)
    sql += " ORDER BY je.posting_date DESC, je.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, max(0, offset)])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
