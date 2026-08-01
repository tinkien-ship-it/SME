from pathlib import Path

p = Path("Services/sme/general_ledger.py")
text = p.read_text(encoding="utf-8")
marker = "def account_ledger("
idx = text.find(marker)
assert idx > 0, "marker not found"
head = text[:idx]

tail = r'''
def _level1_account(conn: sqlite3.Connection, account_code: str) -> sqlite3.Row | None:
    """Leo cay COA toi tai khoan cap 1 (So cai chi theo TK cap 1)."""
    code = (account_code or '').strip()
    if not code:
        return None
    row = conn.execute(
        """
        SELECT code, name, normal_balance, is_postable, level, parent_code
        FROM sme_chart_of_accounts WHERE code = ?
        """,
        (code,),
    ).fetchone()
    if not row:
        root = code[:3] if len(code) >= 3 else code
        row = conn.execute(
            """
            SELECT code, name, normal_balance, is_postable, level, parent_code
            FROM sme_chart_of_accounts WHERE code = ?
            """,
            (root,),
        ).fetchone()
        return row
    seen = set()
    while row and int(row['level'] or 1) > 1 and row['parent_code']:
        if row['code'] in seen:
            break
        seen.add(row['code'])
        parent = conn.execute(
            """
            SELECT code, name, normal_balance, is_postable, level, parent_code
            FROM sme_chart_of_accounts WHERE code = ?
            """,
            (row['parent_code'],),
        ).fetchone()
        if not parent:
            break
        row = parent
    return row


def _descendant_account_filter(code: str) -> tuple[str, list[Any]]:
    """Khop TK cap 1 va moi TK con (111 -> 111, 1111; khong khop 112)."""
    return (
        '(jl.account_code = ? OR jl.account_code LIKE ?)',
        [code, f'{code}%'],
    )


def account_ledger(
    conn: sqlite3.Connection,
    account_code: str,
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """So cai chi tiet theo tai khoan cap 1 (gop phat sinh moi TK con)."""
    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    code_in = (account_code or '').strip()
    if not code_in:
        raise ValueError('Thieu ma tai khoan')

    acc = _level1_account(conn, code_in)
    if not acc:
        raise ValueError(f'Khong tim thay tai khoan {code_in}')
    code = acc['code']

    d_from = date_from[:10]
    d_to = date_to[:10]
    normal = acc['normal_balance'] or 'debit'
    match_sql, match_params = _descendant_account_filter(code)

    op = conn.execute(
        f"""
        SELECT COALESCE(SUM(jl.debit), 0), COALESCE(SUM(jl.credit), 0)
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE {match_sql}
          AND je.status IN ('posted', 'reversed')
          AND je.posting_date < ?
        """,
        (*match_params, d_from),
    ).fetchone()
    open_d, open_c = _money(op[0]), _money(op[1])
    open_bal = _net_balance(open_d, open_c, normal)

    lines = conn.execute(
        f"""
        SELECT jl.id AS line_id, jl.sequence, jl.debit, jl.credit, jl.description,
               jl.account_code AS line_account_code,
               jl.partner_id, jl.partner_type, jl.product_id, jl.warehouse_code,
               je.id AS entry_id, je.entry_no, je.posting_date, je.document_type,
               je.document_no, je.document_id, je.business_type, je.status,
               je.description AS entry_description
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE {match_sql}
          AND je.status IN ('posted', 'reversed')
          AND je.posting_date >= ? AND je.posting_date <= ?
        ORDER BY je.posting_date, je.id, jl.sequence, jl.id
        """,
        (*match_params, d_from, d_to),
    ).fetchall()

    run_d, run_c = open_d, open_c
    detail = []
    period_d = Decimal('0.00')
    period_c = Decimal('0.00')
    for ln in lines:
        d = _money(ln['debit'])
        c = _money(ln['credit'])
        period_d += d
        period_c += c
        run_d += d
        run_c += c
        run_bal = _net_balance(run_d, run_c, normal)
        line_acc = ln['line_account_code'] or code
        desc = ln['description'] or ln['entry_description'] or ''
        if line_acc != code:
            desc = f'[{line_acc}] {desc}'.strip()
        detail.append({
            'line_id': ln['line_id'],
            'entry_id': ln['entry_id'],
            'entry_no': ln['entry_no'],
            'posting_date': ln['posting_date'],
            'document_type': ln['document_type'],
            'document_no': ln['document_no'],
            'document_id': ln['document_id'],
            'business_type': ln['business_type'],
            'status': ln['status'],
            'description': desc,
            'account_code': line_acc,
            'debit': float(d),
            'credit': float(c),
            'balance_debit': run_bal['debit'],
            'balance_credit': run_bal['credit'],
            'partner_type': ln['partner_type'],
            'partner_id': ln['partner_id'],
        })

    close_bal = _net_balance(open_d + period_d, open_c + period_c, normal)
    return {
        'account': {
            'code': acc['code'],
            'name': acc['name'],
            'normal_balance': normal,
            'is_postable': acc['is_postable'],
            'level': int(acc['level'] or 1),
            'includes_children': True,
        },
        'account': {
            'code': acc['code'],
            'name': acc['name'],
            'normal_balance': normal,
            'is_postable': acc['is_postable'],
            'level': int(acc['level'] or 1),
            'includes_children': True,
        },
        'date_from': d_from,
        'date_to': d_to,
        'opening': open_bal,
        'period_debit': float(period_d),
        'period_credit': float(period_c),
        'closing': close_bal,
        'lines': detail,
        'line_count': len(detail),
    }


def accounts_with_activity(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Danh sach tai khoan cap 1 co phat sinh (gom TK con) — chon nhanh so cai."""
    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row

    d_from = (date_from or '').strip()[:10] or None
    d_to = (date_to or '').strip()[:10] or None

    params: list[Any] = []
    date_sql = ''
    if d_from:
        date_sql += ' AND je.posting_date >= ?'
        params.append(d_from)
    if d_to:
        date_sql += ' AND je.posting_date <= ?'
        params.append(d_to)

    leaf_rows = conn.execute(
        f"""
        SELECT jl.account_code AS code,
               COALESCE(SUM(jl.debit), 0) AS period_debit,
               COALESCE(SUM(jl.credit), 0) AS period_credit,
               COUNT(*) AS line_count
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          {date_sql}
        GROUP BY jl.account_code
        HAVING SUM(jl.debit) <> 0 OR SUM(jl.credit) <> 0
        """,
        params,
    ).fetchall()

    l1_map: dict[str, str] = {}
    agg: dict[str, dict[str, Any]] = {}

    for r in leaf_rows:
        leaf = (r['code'] or '').strip()
        if not leaf:
            continue
        if leaf not in l1_map:
            root = _level1_account(conn, leaf)
            l1_map[leaf] = root['code'] if root else (leaf[:3] if len(leaf) >= 3 else leaf)
        l1 = l1_map[leaf]
        bucket = agg.get(l1)
        if not bucket:
            root_acc = _level1_account(conn, l1)
            bucket = {
                'code': l1,
                'name': (root_acc['name'] if root_acc else l1),
                'level': 1,
                'period_debit': Decimal('0.00'),
                'period_credit': Decimal('0.00'),
                'line_count': 0,
            }
            agg[l1] = bucket
        bucket['period_debit'] += _money(r['period_debit'])
        bucket['period_credit'] += _money(r['period_credit'])
        bucket['line_count'] += int(r['line_count'] or 0)

    return [
        {
            'code': agg[code]['code'],
            'name': agg[code]['name'],
            'level': 1,
            'period_debit': float(agg[code]['period_debit']),
            'period_credit': float(agg[code]['period_credit']),
            'line_count': agg[code]['line_count'],
        }
        for code in sorted(agg.keys())
    ]
'''

# Fix accidental duplicate 'account' key and restore Vietnamese messages
tail = tail.replace(
    """    return {
        'account': {
            'code': acc['code'],
            'name': acc['name'],
            'normal_balance': normal,
            'is_postable': acc['is_postable'],
            'level': int(acc['level'] or 1),
            'includes_children': True,
        },
        'account': {
            'code': acc['code'],
            'name': acc['name'],
            'normal_balance': normal,
            'is_postable': acc['is_postable'],
            'level': int(acc['level'] or 1),
            'includes_children': True,
        },
""",
    """    return {
        'account': {
            'code': acc['code'],
            'name': acc['name'],
            'normal_balance': normal,
            'is_postable': acc['is_postable'],
            'level': int(acc['level'] or 1),
            'includes_children': True,
        },
""",
)

# Restore Vietnamese docstrings/messages
replacements = {
    'Leo cay COA toi tai khoan cap 1 (So cai chi theo TK cap 1).':
        'Leo cây COA tới tài khoản cấp 1 (Sổ cái chỉ theo TK cấp 1).',
    'Khop TK cap 1 va moi TK con (111 -> 111, 1111; khong khop 112).':
        'Khớp TK cấp 1 và mọi TK con (111 → 111, 1111; không khớp 112).',
    'So cai chi tiet theo tai khoan cap 1 (gop phat sinh moi TK con).':
        'Sổ cái chi tiết theo tài khoản cấp 1 (gộp phát sinh mọi TK con).',
    'Thieu ma tai khoan': 'Thiếu mã tài khoản',
    'Khong tim thay tai khoan': 'Không tìm thấy tài khoản',
    'Danh sach tai khoan cap 1 co phat sinh (gom TK con) — chon nhanh so cai.':
        'Danh sách tài khoản cấp 1 có phát sinh (gồm TK con) — chọn nhanh sổ cái.',
}
for a, b in replacements.items():
    tail = tail.replace(a, b)

p.write_text(head + tail.lstrip("\n"), encoding="utf-8")
print("OK", p.stat().st_size)
