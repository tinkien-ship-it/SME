"""Đồng bộ một hóa đơn bán hàng POS/F&B sang sổ nhật ký SME."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from Services.sme.payment_method import normalize_sale_payment_method
from Services.sme.account_roles import resolve_posting_account

from Services.sme.journal_engine import (
    get_posting_rule,
    post_journal_entry,
    reverse_journal_entry,
)

SALE_DOCUMENT_TYPES = ('SALE_REVENUE', 'SALE_COGS')
PAYMENT_RULES = {
    '111': ('BAN_HANG_TM', 'CASH'),
    '112': ('BAN_HANG_CK', 'BANK_TRANSFER'),
    '131': ('BAN_HANG_CONG_NO', 'CREDIT'),
}

def _product_revenue_class(
    *,
    product_type: str | None,
    product_code: str | None,
) -> str:
    code = str(product_code or '').strip().upper()

    # Kiểm tra prefix đặc thù trước
    if code.startswith('BDSDT'):
        return 'BDSDT'

    if code.startswith('HH'):
        return 'HH'

    if code.startswith('TP'):
        return 'TP'

    if code.startswith('DV'):
        return 'DV'

    # Fallback cho dữ liệu cũ
    pt = str(product_type or '').strip().lower()

    if pt in (
        'investment_property',
        'bat_dong_san_dau_tu',
        'bdsdt',
    ):
        return 'BDSDT'

    if pt in (
        'service',
        'services',
        'dich_vu',
        'dv',
    ):
        return 'DV'

    if pt in (
        'finished_goods',
        'finished_good',
        'finished_product',
        'thanh_pham',
        'tp',
    ):
        return 'TP'

    return 'HH'


def _revenue_role_for_class(revenue_class: str) -> str:
    return {
        'HH': 'revenue.goods',
        'TP': 'revenue.fg',
        'DV': 'revenue.service',
        'BDSDT': 'revenue.investment_property',
    }[revenue_class]


def resolve_revenue_posting_account(
    conn: sqlite3.Connection,
    revenue_class: str,
) -> str:
    """Resolve TK doanh thu qua Account Roles/COA hiện hành.

    sale_journal không tự fallback tài khoản. Quy tắc leaf/default và
    fallback 511 được tập trung tại account_roles.resolve_posting_account().
    """
    return resolve_posting_account(
        conn,
        _revenue_role_for_class(revenue_class),
    )

def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def resolve_sale_partner_id(conn: sqlite3.Connection, sale) -> int | None:
    """customer_id trên sale, hoặc tra/tạo customers theo MST/tên — dùng cho S4a DNSN."""
    d = dict(sale) if sale is not None and not isinstance(sale, dict) else (sale or {})
    raw = d.get('customer_id')
    if raw not in (None, '', 0, '0'):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    tax = str(d.get('tax_code') or '').strip()
    name = str(d.get('customer_name') or '').strip()
    company = str(d.get('company_name') or '').strip()
    if not tax and not name and not company:
        return None
    try:
        if tax:
            row = conn.execute(
                'SELECT id FROM customers WHERE tax_code = ? LIMIT 1', (tax,),
            ).fetchone()
            if row:
                return int(row[0] if not isinstance(row, sqlite3.Row) else row['id'])
        lookup = name or company
        if lookup:
            row = conn.execute(
                'SELECT id FROM customers WHERE name = ? OR company_name = ? LIMIT 1',
                (lookup, lookup),
            ).fetchone()
            if row:
                return int(row[0] if not isinstance(row, sqlite3.Row) else row['id'])
            conn.execute(
                'INSERT INTO customers (name, company_name, tax_code) VALUES (?, ?, ?)',
                (name or company, company or None, tax or None),
            )
            return int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
    except sqlite3.Error:
        return None
    return None


def _partner_id_for_sale(conn: sqlite3.Connection, sale) -> int | None:
    return resolve_sale_partner_id(conn, sale)



def _active_sale_entries(conn: sqlite3.Connection, sale_id: int) -> list[int]:
    placeholders = ','.join('?' for _ in SALE_DOCUMENT_TYPES)
    rows = conn.execute(
        f"""
        SELECT id FROM sme_journal_entries
        WHERE document_id = ? AND document_type IN ({placeholders})
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id
        """,
        (sale_id, *SALE_DOCUMENT_TYPES),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _is_service_product_type(product_type: str | None) -> bool:
    return str(product_type or '').strip().lower() in (
        'service', 'services', 'dich_vu', 'dv',
    )


def _sale_revenue_buckets(
    conn: sqlite3.Connection,
    sale_id: int,
) -> tuple[
    Decimal,
    Decimal,
    Decimal,
    Decimal,
    Decimal,
    Decimal,
]:
    """
    Phân loại doanh thu POS theo từng dòng:

    HH...    -> 5111: Doanh thu bán hàng hóa
    TP...    -> 5112: Doanh thu bán thành phẩm
    DV...    -> 5113: Doanh thu cung cấp dịch vụ
    BDSDT... -> 5117: Doanh thu kinh doanh BĐS đầu tư
    deferred -> 3387: Doanh thu chưa thực hiện

    Việc phân loại HH/TP/DV/BDSDT chỉ dựa trên products.product_code.
    barcode không được dùng để quyết định tài khoản kế toán.

    Trả về:
        (goods, finished, service, bdsdt, deferred, vat)
    """

    cols = _table_columns(conn, 'sale_items')

    if not {'quantity', 'price'}.issubset(cols):
        zero = Decimal('0.00')
        return zero, zero, zero, zero, zero, zero

    discount_expr = (
        'COALESCE(si.discount_pct, 0)'
        if 'discount_pct' in cols
        else '0'
    )

    from Services.sme.deferred_revenue import (
        ensure_product_revenue_columns,
    )

    ensure_product_revenue_columns(conn)

    product_cols = _table_columns(conn, 'products')

    # product_code là mã nghiệp vụ/kế toán chuẩn để phân loại doanh thu.
    # Không dùng barcode làm fallback vì barcode có thể là mã riêng in trên bao bì.
    if 'product_code' not in product_cols:
        raise ValueError(
            "Bảng products thiếu cột product_code; "
            "không thể phân loại tài khoản doanh thu POS theo HH/TP/DV/BDSDT."
        )

    product_code_expr = "COALESCE(p.product_code, '')"

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(p.product_type, 'goods') AS product_type,
            {product_code_expr} AS product_code,
            COALESCE(p.is_subscription_plan, 0) AS is_subscription_plan,
            COALESCE(p.revenue_mode, 'immediate') AS revenue_mode,
            si.quantity,
            si.price,
            {discount_expr} AS discount_pct,
            COALESCE(si.tax_pct, 0) AS tax_pct
        FROM sale_items si
        LEFT JOIN products p
               ON p.id = si.product_id
        WHERE si.sale_id = ?
        """,
        (int(sale_id),),
    ).fetchall()

    goods = Decimal('0.00')
    finished = Decimal('0.00')
    service = Decimal('0.00')
    bdsdt = Decimal('0.00')
    deferred = Decimal('0.00')
    vat = Decimal('0.00')

    for row in rows:
        product_type = str(row[0] or '').strip().lower()
        product_code = str(row[1] or '').strip().upper()

        is_subscription = int(row[2] or 0)
        revenue_mode = str(row[3] or '').strip().lower()

        quantity = float(row[4] or 0)
        price = float(row[5] or 0)
        discount_pct = float(row[6] or 0)
        tax_pct = float(row[7] or 0)

        subtotal = quantity * price
        discount = round(
            subtotal * discount_pct / 100
        )
        taxable = subtotal - discount

        line_vat = round(
            taxable * tax_pct / 100
        )

        net = _money(taxable)
        vat += _money(line_vat)

        # Doanh thu nhận trước ưu tiên trước phân loại 511x.
        if (
            is_subscription == 1
            or revenue_mode == 'deferred'
        ):
            deferred += net
            continue

        # -------------------------------------------------
        # PHÂN LOẠI DOANH THU THEO PREFIX MÃ SẢN PHẨM
        # -------------------------------------------------

        # Phải kiểm tra BDSDT trước các prefix khác.
        if product_code.startswith('BDSDT'):
            bdsdt += net

        elif product_code.startswith('TP'):
            finished += net

        elif product_code.startswith('DV'):
            service += net

        elif product_code.startswith('HH'):
            goods += net

        # -------------------------------------------------
        # FALLBACK CHO DỮ LIỆU CŨ
        # -------------------------------------------------

        elif product_type in (
            'investment_property',
            'bat_dong_san_dau_tu',
            'bdsdt',
        ):
            bdsdt += net

        elif product_type in (
            'finished_goods',
            'finished_good',
            'finished_product',
            'thanh_pham',
            'tp',
        ):
            finished += net

        elif _is_service_product_type(product_type):
            service += net

        else:
            # Dữ liệu cũ không xác định -> hàng hóa
            goods += net

    return (
        goods,
        finished,
        service,
        bdsdt,
        deferred,
        vat,
    )


def _sale_vat(conn: sqlite3.Connection, sale_id: int, business_line: str) -> Decimal:
    """Tính lại VAT theo đúng công thức checkout POS; F&B hiện chưa lưu VAT dòng."""
    cols = _table_columns(conn, 'sale_items')
    if business_line == 'fb_service' or not {'quantity', 'price', 'tax_pct'}.issubset(cols):
        return Decimal('0.00')

    discount_expr = 'COALESCE(discount_pct, 0)' if 'discount_pct' in cols else '0'
    rows = conn.execute(
        f"""
        SELECT quantity, price, {discount_expr} AS discount_pct,
               COALESCE(tax_pct, 0) AS tax_pct
        FROM sale_items WHERE sale_id = ?
        """,
        (sale_id,),
    ).fetchall()
    vat = Decimal('0.00')
    for row in rows:
        subtotal = float(row[0] or 0) * float(row[1] or 0)
        discount = round(subtotal * (float(row[2] or 0) / 100))
        taxable = subtotal - discount
        vat += _money(round(taxable * (float(row[3] or 0) / 100)))
    return vat


def _tt58_pct_revenue_tax(
    conn: sqlite3.Connection,
    sale: sqlite3.Row,
    revenue_base: Decimal,
) -> tuple[Decimal, Decimal]:
    """GTGT / TNDN % trên DT theo ngành (TT58 PP1/PP2/PP3). Trả (vat, cit)."""
    from Services.sme.dnsn_books import _sector_key
    from Services.sme.regime_profile import get_ledger_profile
    from Services.sme.tt58_tax_rates import sector_tax_map

    profile = get_ledger_profile(conn)
    if not profile.get('is_tt58_micro'):
        return Decimal('0.00'), Decimal('0.00')
    tax_def = profile.get('tt58_tax_method_def') or {}
    if not tax_def:
        return Decimal('0.00'), Decimal('0.00')

    sale_date = str(sale['date'] or '')[:10] or '2099-12-31'
    sector_map = sector_tax_map(conn, as_of=sale_date)
    # Xác định ngành từ dòng hàng / business_line
    sector = 'other'
    try:
        item_cols = _table_columns(conn, 'sale_items')
        sector_col = 'hkd_sector_code' if 'hkd_sector_code' in item_cols else None
        bl_col = 'business_line' if 'business_line' in item_cols else None
        pt_join = 'LEFT JOIN products p ON p.id = si.product_id' if 'product_id' in item_cols else ''
        pt_expr = 'p.product_type' if pt_join else "NULL"
        sc = f'si.{sector_col}' if sector_col else 'NULL'
        bl = f'si.{bl_col}' if bl_col else 'NULL'
        rows = conn.execute(
            f"""
            SELECT {sc} AS sector_code, {bl} AS business_line, {pt_expr} AS product_type,
                   COALESCE(si.quantity,0)*COALESCE(si.price,0) AS amt
            FROM sale_items si
            {pt_join}
            WHERE si.sale_id = ?
            """,
            (int(sale['id']),),
        ).fetchall()
        by_sec: dict[str, Decimal] = {}
        for r in rows:
            sk = _sector_key(
                r['business_line'] if hasattr(r, 'keys') else r[1],
                r['sector_code'] if hasattr(r, 'keys') else r[0],
                r['product_type'] if hasattr(r, 'keys') else r[2],
            )
            amt = _money(r['amt'] if hasattr(r, 'keys') else r[3])
            by_sec[sk] = by_sec.get(sk, Decimal('0')) + amt
        if by_sec:
            sector = max(by_sec.items(), key=lambda x: x[1])[0]
    except sqlite3.Error:
        bl = str(sale['business_line'] or '') if 'business_line' in sale.keys() else ''
        sector = _sector_key(bl, None, None)

    meta = sector_map.get(sector) or sector_map.get('other') or {}
    vat = Decimal('0.00')
    cit = Decimal('0.00')
    if tax_def.get('vat_mode') == 'pct_revenue':
        vat = (revenue_base * Decimal(str(meta.get('vat_pct') or 0)) / Decimal('100')).quantize(Decimal('0.01'))
    if tax_def.get('cit_mode') == 'pct_revenue':
        cit = (revenue_base * Decimal(str(meta.get('cit_pct') or 0)) / Decimal('100')).quantize(Decimal('0.01'))
    return vat, cit


def _build_revenue_lines(
    conn: sqlite3.Connection,
    sale: sqlite3.Row,
) -> tuple[str, list[dict]]:
    payment_code = normalize_sale_payment_method(
        sale['payment_method']
    )

    mapping = PAYMENT_RULES.get(payment_code)
    if not mapping:
        raise ValueError(
            f'Phương thức thanh toán bán hàng không hỗ trợ: '
            f'{payment_code}'
        )

    business_type, payment_method = mapping

    rule = get_posting_rule(
        conn,
        business_type,
        payment_method,
        commit=False,
    )

    if not rule:
        raise ValueError(
            f'Chưa có quy tắc định khoản '
            f'{business_type}/{payment_method}'
        )

    total = _money(sale['total_amount'])

    if total <= 0:
        return business_type, []

    business_line = (
        str(sale['business_line'] or 'pos')
        if 'business_line' in sale.keys()
        else 'pos'
    )

    # ---------------------------------------------------------
    # Xác định phương pháp thuế
    # ---------------------------------------------------------
    from Services.sme.regime_profile import get_ledger_profile

    profile = get_ledger_profile(conn)

    tax_def = (
        profile.get('tt58_tax_method_def')
        or {}
    )

    use_pct_vat = bool(
        profile.get('is_tt58_micro')
        and tax_def.get('vat_mode') == 'pct_revenue'
    )

    if use_pct_vat:
        # TT58 % doanh thu:
        # Toàn bộ giá thanh toán là doanh thu,
        # không tách VAT trên hóa đơn.
        revenue = total
        invoice_vat = Decimal('0.00')

        pct_vat, pct_cit = _tt58_pct_revenue_tax(
            conn,
            sale,
            revenue,
        )

    else:
        invoice_vat = _sale_vat(
            conn,
            int(sale['id']),
            business_line,
        )

        if invoice_vat > total:
            raise ValueError(
                f'Thuế GTGT {invoice_vat} '
                f'lớn hơn tổng thanh toán {total}'
            )

        revenue = total - invoice_vat

        pct_vat = Decimal('0.00')
        pct_cit = Decimal('0.00')

    # ---------------------------------------------------------
    # Thông tin đối tượng
    # ---------------------------------------------------------
    partner_id = _partner_id_for_sale(
        conn,
        sale,
    )

    common = {
        'partner_type': 'customer',
        'partner_id': partner_id,

        'tax_code': (
            sale['tax_code']
            if 'tax_code' in sale.keys()
            else None
        ),

        'vat_invoice_no': (
            sale['invoice_number']
            if 'invoice_number' in sale.keys()
            else None
        ),
    }

    # ---------------------------------------------------------
    # Dòng Nợ:
    #
    # 111 / 112 / 131 tùy phương thức thanh toán
    # ---------------------------------------------------------
    lines = [{
        **common,

        'account_code': rule['debit_account_code'],

        'debit': total,
        'credit': 0,

        'description': (
            'Thu tiền/phải thu khách hàng'
        ),
    }]

    # ---------------------------------------------------------
    # DOANH THU
    # ---------------------------------------------------------
    if revenue > 0:

        # -----------------------------------------------------
        # F&B được xem là dịch vụ
        # -----------------------------------------------------
        if business_line == 'fb_service':

            lines.append({
                **common,

                'account_code': resolve_revenue_posting_account(conn, 'DV'),

                'debit': 0,
                'credit': revenue,

                'description': (
                    'Doanh thu cung cấp dịch vụ'
                ),
            })

        else:
            # -------------------------------------------------
            # Phân loại doanh thu theo sản phẩm POS
            #
            # HH...    -> 5111
            # TP...    -> 5112
            # DV...    -> 5113
            # BDSDT... -> 5117
            # deferred -> 3387
            # -------------------------------------------------

            (
                goods_rev,
                finished_rev,
                service_rev,
                bdsdt_rev,
                deferred_rev,
                _bucket_vat,
            ) = _sale_revenue_buckets(
                conn,
                int(sale['id']),
            )

            bucket_sum = (
                goods_rev
                + finished_rev
                + service_rev
                + bdsdt_rev
                + deferred_rev
            )

            # -------------------------------------------------
            # Đảm bảo tổng doanh thu đã phân loại khớp
            # doanh thu kế toán.
            #
            # Sai lệch <= 0.02 cho phép do làm tròn.
            # -------------------------------------------------
            if (
                bucket_sum > 0
                and abs(
                    bucket_sum - revenue
                ) <= Decimal('0.02')
            ):

                # ---------------------------------------------
                # HH -> 5111
                # ---------------------------------------------
                if goods_rev > 0:

                    lines.append({
                        **common,

                        'account_code': resolve_revenue_posting_account(conn, 'HH'),

                        'debit': 0,
                        'credit': goods_rev,

                        'description': (
                            'Doanh thu bán hàng hóa'
                        ),
                    })

                # ---------------------------------------------
                # TP -> 5112
                # ---------------------------------------------
                if finished_rev > 0:

                    lines.append({
                        **common,

                        'account_code': resolve_revenue_posting_account(conn, 'TP'),

                        'debit': 0,
                        'credit': finished_rev,

                        'description': (
                            'Doanh thu bán thành phẩm'
                        ),
                    })

                # ---------------------------------------------
                # DV -> 5113
                # ---------------------------------------------
                if service_rev > 0:

                    lines.append({
                        **common,

                        'account_code': resolve_revenue_posting_account(conn, 'DV'),

                        'debit': 0,
                        'credit': service_rev,

                        'description': (
                            'Doanh thu cung cấp dịch vụ'
                        ),
                    })

                # ---------------------------------------------
                # BDSDT -> 5117
                # ---------------------------------------------
                if bdsdt_rev > 0:

                    lines.append({
                        **common,

                        'account_code': resolve_revenue_posting_account(conn, 'BDSDT'),

                        'debit': 0,
                        'credit': bdsdt_rev,

                        'description': (
                            'Doanh thu kinh doanh '
                            'bất động sản đầu tư'
                        ),
                    })

                # ---------------------------------------------
                # Doanh thu chưa thực hiện -> 3387
                # ---------------------------------------------
                if deferred_rev > 0:

                    lines.append({
                        **common,

                        'account_code': '3387',

                        'debit': 0,
                        'credit': deferred_rev,

                        'description': (
                            'Doanh thu chưa thực hiện'
                        ),
                    })

            else:
                # -------------------------------------------------
                # KHÔNG fallback về 511 hoặc
                # rule['credit_account_code'].
                #
                # Nếu không phân loại được thì báo lỗi
                # để tránh ghi sai tài khoản doanh thu.
                # -------------------------------------------------
                raise ValueError(
                    'Không thể phân loại đầy đủ doanh thu POS '
                    'theo HH/TP/DV/BDSDT. '
                    f'sale_id={sale["id"]}; '
                    f'revenue={revenue}; '
                    f'bucket_sum={bucket_sum}; '
                    f'HH={goods_rev}; '
                    f'TP={finished_rev}; '
                    f'DV={service_rev}; '
                    f'BDSDT={bdsdt_rev}; '
                    f'deferred={deferred_rev}'
                )

    # ---------------------------------------------------------
    # VAT đầu ra theo hóa đơn
    # ---------------------------------------------------------
    if invoice_vat > 0:

        lines.append({
            **common,

            'account_code': (
                rule.get('vat_account_code')
                or '33311'
            ),

            'debit': 0,
            'credit': invoice_vat,

            'description': (
                'Thuế GTGT đầu ra'
            ),
        })

    # ---------------------------------------------------------
    # TT58:
    # Thuế GTGT tính theo % doanh thu
    #
    # Nợ 811 / Có 33311
    # ---------------------------------------------------------
    if pct_vat > 0:

        lines.append({
            **common,

            'account_code': '811',

            'debit': pct_vat,
            'credit': 0,

            'description': (
                'Thuế GTGT theo tỷ lệ % '
                'trên doanh thu (TT58)'
            ),
        })

        lines.append({
            **common,

            'account_code': (
                rule.get('vat_account_code')
                or '33311'
            ),

            'debit': 0,
            'credit': pct_vat,

            'description': (
                'Thuế GTGT phải nộp '
                '% DT (TT58)'
            ),
        })

    # ---------------------------------------------------------
    # TT58:
    # Thuế TNDN tính theo % doanh thu
    #
    # Nợ 821 / Có 3334
    # ---------------------------------------------------------
    if pct_cit > 0:

        lines.append({
            **common,

            'account_code': '821',

            'debit': pct_cit,
            'credit': 0,

            'description': (
                'Thuế TNDN theo tỷ lệ % '
                'trên doanh thu (TT58)'
            ),
        })

        lines.append({
            **common,

            'account_code': '3334',

            'debit': 0,
            'credit': pct_cit,

            'description': (
                'Thuế TNDN phải nộp '
                '% DT (TT58)'
            ),
        })

    return business_type, lines


def _cogs_accounts_for_product_type(product_type: str | None, move_type: str) -> tuple[str, str, str]:
    """Trả (TK GV, TK kho, nhãn) — bán nội địa."""
    from Services.sme.cogs_accounts import cogs_accounts_for_line
    return cogs_accounts_for_line(product_type, move_type, channel='domestic')


def _build_cogs_lines(conn: sqlite3.Connection, sale_id: int) -> list[dict]:
    # Gom theo loại hàng để xuất đúng 156/152/155 (TP sản xuất)
    try:
        rows = conn.execute(
            """
            SELECT
                UPPER(sm.type) AS move_type,
                COALESCE(p.product_type, 'goods') AS product_type,
                SUM(
                    CASE WHEN UPPER(sm.type) = 'RETURN_SALE' THEN -1 ELSE 1 END
                    * ABS(COALESCE(sm.quantity, 0)) * COALESCE(sm.cost_price, 0)
                ) AS amount
            FROM stock_moves sm
            LEFT JOIN products p ON p.id = sm.product_id
            WHERE sm.ref_id = ? AND UPPER(sm.type) IN ('SALE', 'SALE_RECIPE', 'RETURN_SALE')
            GROUP BY UPPER(sm.type), COALESCE(p.product_type, 'goods')
            """,
            (sale_id,),
        ).fetchall()
    except sqlite3.Error:
        rows = conn.execute(
            """
            SELECT CASE WHEN UPPER(type) = 'SALE_RECIPE' THEN 'SALE_RECIPE' ELSE 'SALE' END AS move_type,
                   'goods' AS product_type,
                   SUM(
                       CASE WHEN UPPER(type) = 'RETURN_SALE' THEN -1 ELSE 1 END
                       * ABS(COALESCE(quantity, 0)) * COALESCE(cost_price, 0)
                   ) AS amount
            FROM stock_moves
            WHERE ref_id = ? AND UPPER(type) IN ('SALE', 'SALE_RECIPE', 'RETURN_SALE')
            GROUP BY CASE WHEN UPPER(type) = 'SALE_RECIPE' THEN 'SALE_RECIPE' ELSE 'SALE' END
            """,
            (sale_id,),
        ).fetchall()
    lines: list[dict] = []
    sequence = 1
    for row in rows:
        amount = _money(row[2] if len(row) > 2 else row[1])
        if amount <= 0:
            continue
        move_type = row[0]
        product_type = row[1] if len(row) > 2 else 'goods'
        debit_code, credit_code, label = _cogs_accounts_for_product_type(product_type, move_type)
        lines.extend([
            {
                'sequence': sequence,
                'account_code': debit_code,
                'debit': amount,
                'credit': 0,
                'description': f'Giá vốn {label}',
            },
            {
                'sequence': sequence + 1,
                'account_code': credit_code,
                'debit': 0,
                'credit': amount,
                'description': f'Xuất kho {label}',
            },
        ])
        sequence += 2
    return lines


def sync_sale_journals(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    accounting_regime: str | None,
    created_by: str | None = None,
    replace_existing: bool = False,
    features: dict | None = None,
) -> dict:
    """
    Ghi doanh thu/VAT và giá vốn của sale đã completed.

    Hàm không commit; caller phải commit/rollback cùng giao dịch bán hàng.
    Tenant HKD được bỏ qua để không trộn sổ SME với Services/hkd_*.
    """
    regime = str(accounting_regime or '').upper()
    if features is not None:
        if not features.get('journal_posting'):
            return {'posted': False, 'reason': 'journal_posting_disabled', 'entry_ids': []}
    elif not regime.startswith('SME'):
        return {'posted': False, 'reason': 'not_sme', 'entry_ids': []}

    from Services.sme.bootstrap import ensure_sme_accounting_ready
    ensure_sme_accounting_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError(f'Không tìm thấy hóa đơn bán #{sale_id}')

    active_ids = _active_sale_entries(conn, sale_id)
    reversed_ids: list[int] = []
    if active_ids and replace_existing:
        reverse_date = str(sale['date'] or '')[:10] or None
        for entry_id in active_ids:
            reversed_entry = reverse_journal_entry(
                conn,
                entry_id,
                posting_date=reverse_date,
                created_by=created_by,
                reason='Thay thế bút toán do cập nhật hóa đơn bán',
            )
            reversed_ids.append(reversed_entry['id'])
        active_ids = []
    if active_ids:
        return {
            'posted': False,
            'reason': 'already_posted',
            'entry_ids': active_ids,
            'reversed_entry_ids': reversed_ids,
        }

    if str(sale['status'] or '').lower() != 'completed':
        return {
            'posted': False,
            'reason': 'sale_not_completed',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    business_line = ''
    if 'business_line' in sale.keys():
        business_line = str(sale['business_line'] or '').strip().lower()
    sale_no = str(sale['sale_no'] or '') if 'sale_no' in sale.keys() else ''
    note = str(sale['note'] or '') if 'note' in sale.keys() else ''
    if (
        business_line == 'return_import'
        or sale_no.upper().startswith('TR')
        or 'Trả hàng NCC' in note
    ):
        return {
            'posted': False,
            'reason': 'return_import_sale',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    posting_date = str(sale['date'] or '')[:10]
    document_no = sale['sale_no'] if 'sale_no' in sale.keys() else None
    description = f"Bán hàng {document_no or ('#' + str(sale_id))}"

    provision_result: dict = {}
    try:
        from Services.sme.pos_service_sale import provision_service_jobs_for_sale
        provision_result = provision_service_jobs_for_sale(
            conn,
            sale_id,
            created_by=created_by or '',
            replace_existing=replace_existing,
            commit=False,
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            'provision_service_jobs_for_sale sale %s: %s', sale_id, exc,
        )

    business_type, revenue_lines = _build_revenue_lines(conn, sale)
    from Services.sme.branch_filter import warehouse_branch_or_session
    wh_code = None
    try:
        sm_cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_moves)').fetchall()}
        if 'warehouse_code' in sm_cols:
            wh_row = conn.execute(
                """
                SELECT warehouse_code FROM stock_moves
                WHERE ref_id = ? AND warehouse_code IS NOT NULL AND warehouse_code != ''
                ORDER BY id DESC LIMIT 1
                """,
                (sale_id,),
            ).fetchone()
            if wh_row:
                wh_code = wh_row[0] if not isinstance(wh_row, sqlite3.Row) else wh_row['warehouse_code']
    except Exception:
        pass
    branch = warehouse_branch_or_session(conn, wh_code)
    posted: list[dict] = []
    if revenue_lines:
        posted.append(post_journal_entry(
            conn,
            posting_date=posting_date,
            document_date=posting_date,
            document_type='SALE_REVENUE',
            document_no=document_no,
            document_id=sale_id,
            business_type=business_type,
            description=description,
            reference_document=document_no,
            created_by=created_by,
            branch_code=branch,
            lines=revenue_lines,
        ))

    cogs_lines = _build_cogs_lines(conn, sale_id)
    if cogs_lines:
        posted.append(post_journal_entry(
            conn,
            posting_date=posting_date,
            document_date=posting_date,
            document_type='SALE_COGS',
            document_no=document_no,
            document_id=sale_id,
            business_type='GIA_VON_BAN_HANG',
            description=f'Giá vốn {description.lower()}',
            reference_document=document_no,
            created_by=created_by,
            branch_code=branch,
            lines=cogs_lines,
        ))

    # Giá vốn dịch vụ TT99: nghiệm thu job gắn hóa đơn → Nợ 6323 / Có 154
    try:
        from Services.sme.service_costing import deliver_jobs_for_sale
        deliver_jobs_for_sale(
            conn, sale_id, created_by=created_by, commit=False,
        )
    except Exception:
        pass

    return {
        'posted': bool(posted),
        'entry_ids': [item['id'] for item in posted],
        'reversed_entry_ids': reversed_ids,
        'service_jobs': provision_result,
    }
