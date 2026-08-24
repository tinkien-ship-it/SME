"""Nộp thuế theo khoản — nhập khẩu / xuất khẩu.

Sau khi lập phiếu hàng đi đường (import) hoặc thông quan XK (export):
liệt kê từng khoản thuế phải nộp, cho phép nộp lẻ hoặc gộp cùng cơ quan nhận,
chọn TK Có = tiền mặt (1111) hoặc TGNH (1121).

Phân nhóm cơ quan tiếp nhận:
  - customs: Thuế NK / GTGT hàng NK / Thuế XK → Hải quan / KBNN
  - local_tax: TTĐB / BVMT → Cơ quan thuế địa phương / KBNN

TTĐB hàng nhập khẩu: nộp và vốn hóa vào giá vốn tại khâu NK
(CIF + thuế NK). Khi bán ra trong nước không tính lại TTĐB — chỉ GTGT.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')

SOURCE_IMPORT = 'import'
SOURCE_EXPORT = 'export_sale'

AGENCY_CUSTOMS = 'customs'
AGENCY_LOCAL = 'local_tax'

DEFAULT_AGENCY_NAME = {
    AGENCY_CUSTOMS: 'Cơ quan Hải quan / Kho bạc Nhà nước',
    AGENCY_LOCAL: 'Cơ quan thuế địa phương / Kho bạc Nhà nước',
}

# Định nghĩa khoản thuế
TAX_KIND_DEFS: dict[str, dict[str, Any]] = {
    'import_duty': {
        'label': 'Thuế nhập khẩu',
        'short': 'Thuế NK',
        'account_code': '3333',
        'agency_type': AGENCY_CUSTOMS,
        'source': SOURCE_IMPORT,
        'hint': 'Nộp cho Hải quan theo tờ khai.',
    },
    'import_vat': {
        'label': 'Thuế GTGT hàng nhập khẩu',
        'short': 'GTGT NK',
        'account_code': '33312',
        'agency_type': AGENCY_CUSTOMS,
        'source': SOURCE_IMPORT,
        'hint': 'GTGT khâu nhập khẩu — thường nộp cùng tờ khai HQ.',
    },
    'excise': {
        'label': 'Thuế tiêu thụ đặc biệt',
        'short': 'Thuế TTĐB',
        'account_code': '3332',
        'agency_type': AGENCY_LOCAL,
        'source': SOURCE_IMPORT,
        'hint': (
            'Khâu NK: (CIF + thuế NK) × thuế suất — vốn hóa vào giá vốn. '
            'Khi bán ra không tính lại TTĐB, chỉ GTGT.'
        ),
    },
    'env': {
        'label': 'Thuế bảo vệ môi trường',
        'short': 'Thuế BVMT',
        'account_code': '3338',
        'agency_type': AGENCY_LOCAL,
        'source': SOURCE_IMPORT,
        'hint': 'Nộp cơ quan thuế địa phương; vốn hóa vào giá vốn hàng mua.',
    },
    'export_duty': {
        'label': 'Thuế xuất khẩu',
        'short': 'Thuế XK',
        'account_code': '3333',
        'agency_type': AGENCY_CUSTOMS,
        'source': SOURCE_EXPORT,
        'hint': 'Thuế XK theo tờ khai — nộp Hải quan / KBNN.',
    },
}


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def ensure_tax_payment_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_tax_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            tax_kind TEXT NOT NULL,
            account_code TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            agency_type TEXT,
            agency_name TEXT,
            payment_method TEXT,
            voucher_id INTEGER,
            journal_entry_id INTEGER,
            pay_date TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_type, source_id, tax_kind)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_tax_payments_source
        ON sme_tax_payments(source_type, source_id)
        """
    )
    if commit:
        sqlite_commit(conn, label='tax_payment_ops')


def _paid_map(
    conn: sqlite3.Connection, source_type: str, source_id: int
) -> dict[str, dict[str, Any]]:
    ensure_tax_payment_schema(conn, commit=False)
    rows = conn.execute(
        """
        SELECT tax_kind, amount, voucher_id, journal_entry_id, pay_date,
               agency_name, payment_method, account_code
        FROM sme_tax_payments
        WHERE source_type = ? AND source_id = ?
        """,
        (source_type, int(source_id)),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = dict(r) if hasattr(r, 'keys') else {
            'tax_kind': r[0], 'amount': r[1], 'voucher_id': r[2],
            'journal_entry_id': r[3], 'pay_date': r[4],
            'agency_name': r[5], 'payment_method': r[6], 'account_code': r[7],
        }
        out[str(d['tax_kind'])] = d
    return out


def _obligation_item(
    kind: str,
    amount: Decimal,
    paid: dict[str, Any] | None,
    *,
    customs_decl_no: str | None = None,
) -> dict[str, Any]:
    meta = TAX_KIND_DEFS[kind]
    paid_amt = _money(paid.get('amount') if paid else 0)
    remaining = max(amount - paid_amt, Decimal('0.00')) if paid else amount
    is_paid = bool(paid) and remaining <= Decimal('0.00')
    agency_type = meta['agency_type']
    return {
        'tax_kind': kind,
        'label': meta['label'],
        'short': meta['short'],
        'account_code': meta['account_code'],
        'agency_type': agency_type,
        'agency_name': DEFAULT_AGENCY_NAME[agency_type],
        'amount': float(amount),
        'paid_amount': float(paid_amt),
        'remaining': float(remaining),
        'is_paid': is_paid,
        'voucher_id': paid.get('voucher_id') if paid else None,
        'pay_date': paid.get('pay_date') if paid else None,
        'payment_method': paid.get('payment_method') if paid else None,
        'hint': meta['hint'],
        'customs_decl_no': customs_decl_no or '',
    }


def list_import_tax_obligations(
    conn: sqlite3.Connection, import_id: int
) -> dict[str, Any]:
    """Danh sách khoản thuế phải nộp trên phiếu nhập khẩu."""
    ensure_tax_payment_schema(conn, commit=False)
    from Services.sme.import_transit import ensure_import_transit_schema
    ensure_import_transit_schema(conn, commit=False)

    imp = conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone()
    if not imp:
        raise ValueError('Không tìm thấy phiếu nhập')
    imp = dict(imp)
    itype = str(imp.get('import_type') or 'DOMESTIC').upper()
    if itype != 'IMPORT':
        raise ValueError('Chỉ áp dụng cho hàng nhập khẩu')

    nk = _money(imp.get('import_tax_amount'))
    excise = _money(imp.get('excise_tax_amount'))
    env = _money(imp.get('env_tax_amount'))
    vat = Decimal('0.00')
    detail_cols = _cols(conn, 'import_details')
    if 'tax' in detail_cols:
        row = conn.execute(
            'SELECT COALESCE(SUM(tax), 0) FROM import_details WHERE import_id = ?',
            (import_id,),
        ).fetchone()
        vat = _money(row[0] if row else 0)

    paid = _paid_map(conn, SOURCE_IMPORT, import_id)
    # Đồng bộ legacy: nếu đã có tax_payment_journal_id mà chưa có dòng sme_tax_payments
    # → coi như đã nộp hết (phiếu cũ nộp gộp 1 lần).
    legacy_paid = bool(imp.get('tax_payment_journal_id') or imp.get('tax_payment_voucher_id'))
    if legacy_paid and not paid:
        for kind, amt in (
            ('import_duty', nk), ('import_vat', vat),
            ('excise', excise), ('env', env),
        ):
            if amt > 0:
                paid[kind] = {
                    'amount': float(amt),
                    'voucher_id': imp.get('tax_payment_voucher_id'),
                    'journal_entry_id': imp.get('tax_payment_journal_id'),
                    'pay_date': None,
                    'payment_method': None,
                    'agency_name': None,
                    'account_code': TAX_KIND_DEFS[kind]['account_code'],
                }

    decl = (imp.get('customs_decl_no') or '').strip() or None
    items: list[dict[str, Any]] = []
    for kind, amt in (
        ('import_duty', nk),
        ('import_vat', vat),
        ('excise', excise),
        ('env', env),
    ):
        if amt <= 0 and kind not in paid:
            continue
        if amt <= 0:
            continue
        items.append(_obligation_item(kind, amt, paid.get(kind), customs_decl_no=decl))

    unpaid = [x for x in items if not x['is_paid']]
    unpaid_total = sum((_money(x['remaining']) for x in unpaid), Decimal('0.00'))
    by_agency: dict[str, list[dict]] = {AGENCY_CUSTOMS: [], AGENCY_LOCAL: []}
    for it in items:
        by_agency.setdefault(it['agency_type'], []).append(it)

    stage = str(imp.get('receipt_stage') or '').upper()
    return {
        'source_type': SOURCE_IMPORT,
        'source_id': int(import_id),
        'import_no': imp.get('import_no'),
        'customs_decl_no': decl,
        'receipt_stage': stage,
        'items': items,
        'by_agency': by_agency,
        'unpaid_count': len(unpaid),
        'unpaid_total': float(unpaid_total),
        'all_paid': bool(items) and len(unpaid) == 0,
        'agency_labels': {
            AGENCY_CUSTOMS: DEFAULT_AGENCY_NAME[AGENCY_CUSTOMS],
            AGENCY_LOCAL: DEFAULT_AGENCY_NAME[AGENCY_LOCAL],
        },
    }


def list_export_tax_obligations(
    conn: sqlite3.Connection, sale_id: int
) -> dict[str, Any]:
    """Danh sách thuế XK phải nộp sau thông quan."""
    ensure_tax_payment_schema(conn, commit=False)
    from Services.sme.export_payment import ensure_export_sale_schema
    ensure_export_sale_schema(conn, commit=False)

    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError('Không tìm thấy phiếu xuất khẩu')
    sale = dict(sale)
    if str(sale.get('sale_type') or '').upper() != 'EXPORT':
        raise ValueError('Chỉ áp dụng cho phiếu xuất khẩu')

    status = str(sale.get('export_status') or '').lower()
    tax_vnd = _money(sale.get('export_tax_vnd'))
    if tax_vnd <= 0 and _money(sale.get('export_tax_fc')) > 0:
        rate = _money(sale.get('customs_fx_rate') or sale.get('exchange_rate') or 1)
        tax_vnd = _money(_money(sale.get('export_tax_fc')) * rate)

    paid = _paid_map(conn, SOURCE_EXPORT, sale_id)
    decl = (sale.get('customs_decl_no') or '').strip() or None
    items: list[dict[str, Any]] = []
    if tax_vnd > 0:
        items.append(
            _obligation_item('export_duty', tax_vnd, paid.get('export_duty'), customs_decl_no=decl)
        )

    unpaid = [x for x in items if not x['is_paid']]
    unpaid_total = sum((_money(x['remaining']) for x in unpaid), Decimal('0.00'))
    return {
        'source_type': SOURCE_EXPORT,
        'source_id': int(sale_id),
        'sale_no': sale.get('sale_no'),
        'export_status': status,
        'customs_decl_no': decl,
        'items': items,
        'by_agency': {AGENCY_CUSTOMS: items, AGENCY_LOCAL: []},
        'unpaid_count': len(unpaid),
        'unpaid_total': float(unpaid_total),
        'all_paid': bool(items) and len(unpaid) == 0,
        'can_pay': status == 'cleared' and unpaid_total > 0,
        'agency_labels': {
            AGENCY_CUSTOMS: DEFAULT_AGENCY_NAME[AGENCY_CUSTOMS],
            AGENCY_LOCAL: DEFAULT_AGENCY_NAME[AGENCY_LOCAL],
        },
    }


def _normalize_payment_method(payment_method: str) -> str:
    pm = (payment_method or 'bank').strip().lower()
    if pm in ('cash', '111', '1111', 'tien_mat', 'tm'):
        return 'cash'
    if pm in ('bank', '112', '1121', 'bank_transfer', 'ck', 'transfer', 'tgnh'):
        return 'bank'
    if pm[:1].isdigit() and (pm.startswith('111') or pm.startswith('112')):
        return pm
    return 'bank'


def pay_selected_taxes(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_id: int,
    tax_kinds: list[str],
    payment_method: str = 'bank',
    pay_date: str | None = None,
    agency_name: str | None = None,
    agency_type: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Nộp một hoặc nhiều khoản thuế cùng cơ quan nhận trên 1 phiếu chi."""
    from Services.sme.branches import resolve_posting_branch
    from Services.sme.vouchers import create_payment

    ensure_tax_payment_schema(conn, commit=False)
    kinds = [str(k).strip() for k in (tax_kinds or []) if str(k).strip()]
    if not kinds:
        raise ValueError('Chọn ít nhất một khoản thuế để nộp')

    if source_type == SOURCE_IMPORT:
        summary = list_import_tax_obligations(conn, source_id)
        ref_doc = summary.get('customs_decl_no') or summary.get('import_no') or ''
        doc_label = f"PN {summary.get('import_no') or source_id}"
        import_id = source_id
        sale_id = None
    elif source_type == SOURCE_EXPORT:
        summary = list_export_tax_obligations(conn, source_id)
        if not summary.get('can_pay') and summary.get('unpaid_total', 0) <= 0:
            raise ValueError('Không có thuế XK còn phải nộp (cần thông quan trước)')
        if str(summary.get('export_status') or '') != 'cleared' and summary['unpaid_total'] > 0:
            # Cho phép nộp nếu đã có số thuế và đã cleared; chặn nếu chưa thông quan
            if str(summary.get('export_status') or '') != 'cleared':
                raise ValueError('Chỉ nộp thuế XK sau khi xác nhận thông quan')
        ref_doc = summary.get('customs_decl_no') or summary.get('sale_no') or ''
        doc_label = f"XK {summary.get('sale_no') or source_id}"
        import_id = None
        sale_id = source_id
    else:
        raise ValueError('source_type không hợp lệ')

    by_kind = {x['tax_kind']: x for x in summary['items']}
    selected: list[dict[str, Any]] = []
    for k in kinds:
        it = by_kind.get(k)
        if not it:
            raise ValueError(f'Khoản thuế không có trên chứng từ: {k}')
        if it['is_paid'] or _money(it['remaining']) <= 0:
            raise ValueError(f"«{it['label']}» đã nộp đủ")
        selected.append(it)

    agencies = {x['agency_type'] for x in selected}
    if len(agencies) > 1:
        raise ValueError(
            'Không gộp thuế của hai cơ quan khác nhau trên cùng một phiếu chi. '
            'Nộp riêng nhóm Hải quan và nhóm cơ quan thuế địa phương.'
        )
    resolved_agency_type = agency_type or next(iter(agencies))
    if resolved_agency_type not in (AGENCY_CUSTOMS, AGENCY_LOCAL):
        resolved_agency_type = next(iter(agencies))
    party = (agency_name or '').strip() or DEFAULT_AGENCY_NAME[resolved_agency_type]

    date_s = str(pay_date or '')[:10]
    if not date_s:
        date_s = datetime.now().strftime('%Y-%m-%d')

    pm = _normalize_payment_method(payment_method)
    debit_lines = []
    for it in selected:
        debit_lines.append({
            'account_code': it['account_code'],
            'amount': float(_money(it['remaining'])),
            'description': f"Nộp {it['short']} — {doc_label}",
        })
    total = sum((_money(x['amount']) for x in debit_lines), Decimal('0.00'))
    if total <= 0:
        raise ValueError('Số tiền nộp thuế không hợp lệ')

    kind_labels = ', '.join(x['short'] for x in selected)
    voucher = create_payment(
        conn,
        voucher_date=date_s,
        party_name=party,
        amount=float(total),
        payment_method=pm,
        debit_account=debit_lines[0]['account_code'],
        debit_lines=debit_lines,
        reason=f'Nộp {kind_labels} — {doc_label}',
        reference_document=ref_doc,
        source_type=(
            'import_customs_tax' if source_type == SOURCE_IMPORT else 'export_customs_tax'
        ),
        source_id=source_id,
        import_id=import_id,
        purpose='customs_tax',
        created_by=created_by,
        branch_code=resolve_posting_branch(conn, None),
        commit=False,
    )

    for it in selected:
        conn.execute(
            """
            INSERT INTO sme_tax_payments (
                source_type, source_id, tax_kind, account_code, amount,
                agency_type, agency_name, payment_method,
                voucher_id, journal_entry_id, pay_date, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id, tax_kind) DO UPDATE SET
                amount = excluded.amount,
                agency_type = excluded.agency_type,
                agency_name = excluded.agency_name,
                payment_method = excluded.payment_method,
                voucher_id = excluded.voucher_id,
                journal_entry_id = excluded.journal_entry_id,
                pay_date = excluded.pay_date,
                created_by = excluded.created_by
            """,
            (
                source_type, int(source_id), it['tax_kind'], it['account_code'],
                float(_money(it['remaining'])), resolved_agency_type, party, pm,
                voucher['id'], voucher.get('journal_entry_id'), date_s,
                created_by, _now(),
            ),
        )

    # Cập nhật header import / export
    if source_type == SOURCE_IMPORT:
        _sync_import_tax_stage(conn, source_id, voucher)
    elif source_type == SOURCE_EXPORT and sale_id:
        _sync_export_tax_flags(conn, sale_id, voucher)

    if commit:
        sqlite_commit(conn, label='tax_payment_ops')

    refreshed = (
        list_import_tax_obligations(conn, source_id)
        if source_type == SOURCE_IMPORT
        else list_export_tax_obligations(conn, source_id)
    )
    return {
        'voucher': voucher,
        'paid_kinds': [x['tax_kind'] for x in selected],
        'amount': float(total),
        'agency_type': resolved_agency_type,
        'agency_name': party,
        'payment_method': pm,
        'summary': refreshed,
    }


def _sync_import_tax_stage(
    conn: sqlite3.Connection, import_id: int, voucher: dict[str, Any]
) -> None:
    from Services.sme.import_transit import (
        STAGE_IN_TRANSIT,
        STAGE_RECEIVED,
        STAGE_TAX_PAID,
        ensure_import_transit_schema,
    )
    ensure_import_transit_schema(conn, commit=False)
    summary = list_import_tax_obligations(conn, import_id)
    cols = _cols(conn, 'import')
    imp = dict(conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone())
    stage = str(imp.get('receipt_stage') or STAGE_IN_TRANSIT).upper()
    if stage == STAGE_RECEIVED:
        new_stage = stage
    elif summary.get('all_paid'):
        new_stage = STAGE_TAX_PAID
    elif summary.get('unpaid_count', 0) < len(summary.get('items') or []):
        new_stage = 'TAX_PARTIAL'
    else:
        new_stage = stage or STAGE_IN_TRANSIT

    sets = []
    vals: list[Any] = []
    if 'tax_payment_voucher_id' in cols:
        sets.append('tax_payment_voucher_id = ?')
        vals.append(voucher['id'])
    if 'tax_payment_journal_id' in cols:
        sets.append('tax_payment_journal_id = ?')
        vals.append(voucher.get('journal_entry_id'))
    if 'receipt_stage' in cols:
        sets.append('receipt_stage = ?')
        vals.append(new_stage)
    if not sets:
        return
    vals.append(import_id)
    conn.execute(f'UPDATE "import" SET {", ".join(sets)} WHERE id = ?', vals)


def _sync_export_tax_flags(
    conn: sqlite3.Connection, sale_id: int, voucher: dict[str, Any]
) -> None:
    """Gắn voucher nộp thuế XK nếu cột tồn tại trên bảng sale."""
    from Services.sme.export_payment import ensure_export_sale_schema
    ensure_export_sale_schema(conn, commit=False)
    cols = _cols(conn, 'sale')
    sets, vals = [], []
    if 'tax_payment_voucher_id' not in cols:
        try:
            conn.execute('ALTER TABLE sale ADD COLUMN tax_payment_voucher_id INTEGER')
            cols.add('tax_payment_voucher_id')
        except sqlite3.OperationalError:
            pass
    if 'tax_payment_journal_id' not in cols:
        try:
            conn.execute('ALTER TABLE sale ADD COLUMN tax_payment_journal_id INTEGER')
            cols.add('tax_payment_journal_id')
        except sqlite3.OperationalError:
            pass
    if 'tax_paid_at' not in cols:
        try:
            conn.execute('ALTER TABLE sale ADD COLUMN tax_paid_at TEXT')
            cols.add('tax_paid_at')
        except sqlite3.OperationalError:
            pass
    cols = _cols(conn, 'sale')
    if 'tax_payment_voucher_id' in cols:
        sets.append('tax_payment_voucher_id = ?')
        vals.append(voucher['id'])
    if 'tax_payment_journal_id' in cols:
        sets.append('tax_payment_journal_id = ?')
        vals.append(voucher.get('journal_entry_id'))
    if 'tax_paid_at' in cols:
        sets.append('tax_paid_at = ?')
        vals.append(_now())
    if sets:
        vals.append(sale_id)
        conn.execute(f'UPDATE sale SET {", ".join(sets)} WHERE id = ?', vals)


def pay_customs_taxes_compat(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    pay_date: str | None = None,
    payment_method: str = 'bank',
    created_by: str | None = None,
    commit: bool = False,
    agency_type: str | None = None,
) -> dict[str, Any]:
    """Tương thích API cũ: nộp tất cả khoản chưa nộp (cùng 1 nhóm nếu chỉ định agency)."""
    summary = list_import_tax_obligations(conn, import_id)
    unpaid = [x for x in summary['items'] if not x['is_paid']]
    if not unpaid:
        raise ValueError('Không có thuế còn phải nộp trên phiếu này')
    if agency_type:
        unpaid = [x for x in unpaid if x['agency_type'] == agency_type]
        if not unpaid:
            raise ValueError('Không còn khoản thuế thuộc nhóm cơ quan đã chọn')
    else:
        # API cũ nộp gộp — nếu lẫn 2 cơ quan thì ưu tiên nộp nhóm Hải quan trước
        customs = [x for x in unpaid if x['agency_type'] == AGENCY_CUSTOMS]
        local = [x for x in unpaid if x['agency_type'] == AGENCY_LOCAL]
        if customs and local:
            unpaid = customs
        # nếu chỉ còn 1 nhóm thì nộp hết nhóm đó

    kinds = [x['tax_kind'] for x in unpaid]
    result = pay_selected_taxes(
        conn,
        source_type=SOURCE_IMPORT,
        source_id=import_id,
        tax_kinds=kinds,
        payment_method=payment_method,
        pay_date=pay_date,
        created_by=created_by,
        commit=commit,
    )
    s = result['summary']
    amounts = {x['tax_kind']: x['amount'] for x in s.get('items') or []}
    return {
        'import_id': import_id,
        'receipt_stage': s.get('receipt_stage'),
        'voucher': result['voucher'],
        'amounts': {
            'import_tax': amounts.get('import_duty', 0),
            'excise_tax': amounts.get('excise', 0),
            'env_tax': amounts.get('env', 0),
            'vat': amounts.get('import_vat', 0),
            'total': result['amount'],
        },
        'paid_kinds': result['paid_kinds'],
        'summary': s,
        'note': (
            'Đã nộp nhóm Hải quan; còn thuế địa phương chưa nộp — mở modal để nộp tiếp.'
            if s.get('unpaid_count')
            else None
        ),
    }
