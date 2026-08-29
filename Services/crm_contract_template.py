# -*- coding: utf-8 -*-
"""Mẫu hợp đồng mua bán hàng hóa — placeholder [[FIELD]] xuất/nạp được.

Lưu trữ: bảng ``crm_settings`` (key ``contract_sale_template_html``) trong **DB của từng tenant**
(SQLite ``tenants/<tenant_id>.db`` hoặc schema PostgreSQL riêng). Mỗi tenant chỉnh mẫu riêng —
không dùng file chung, không ghi vào main/registry DB.
"""
from __future__ import annotations

import html
import re
import sqlite3
from datetime import datetime
from typing import Any

from Services.crm_schema import ensure_crm_schema

TEMPLATE_SETTING_KEY = 'contract_sale_template_html'

PLACEHOLDER_RE = re.compile(r'\[\[([A-Z0-9_]+)\]\]')

# Các mã hệ thống hiểu khi nạp lại mẫu đã chỉnh sửa
KNOWN_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ('CONTRACT_NO', 'Số hợp đồng'),
    ('SIGNED_DATE', 'Ngày ký (dd/mm/yyyy)'),
    ('DAY', 'Ngày (số)'),
    ('MONTH', 'Tháng (số)'),
    ('YEAR', 'Năm'),
    ('PLACE', 'Nơi ký'),
    ('SELLER_NAME', 'Bên A — tên DN'),
    ('SELLER_TAX', 'Bên A — MST'),
    ('SELLER_ADDRESS', 'Bên A — địa chỉ'),
    ('SELLER_PHONE', 'Bên A — điện thoại'),
    ('SELLER_EMAIL', 'Bên A — email'),
    ('SELLER_BANK', 'Bên A — số TK'),
    ('SELLER_BANK_NAME', 'Bên A — ngân hàng'),
    ('SELLER_REP', 'Bên A — người đại diện'),
    ('SELLER_TITLE', 'Bên A — chức vụ'),
    ('BUYER_NAME', 'Bên B — tên KH'),
    ('BUYER_TAX', 'Bên B — MST'),
    ('BUYER_ADDRESS', 'Bên B — địa chỉ'),
    ('BUYER_PHONE', 'Bên B — điện thoại'),
    ('BUYER_EMAIL', 'Bên B — email'),
    ('BUYER_REP', 'Bên B — người đại diện'),
    ('BUYER_TITLE', 'Bên B — chức vụ'),
    ('ITEMS_TABLE', 'Bảng hàng hóa đầy đủ (HTML)'),
    ('ITEMS_ROWS', 'Chỉ các dòng <tr> hàng hóa'),
    ('SUBTOTAL', 'Tổng chưa VAT'),
    ('VAT_AMOUNT', 'Tổng tiền VAT'),
    ('TOTAL', 'Tổng thanh toán'),
    ('TOTAL_WORDS', 'Tổng bằng chữ'),
    ('PAYMENT_METHOD', 'Hình thức thanh toán'),
    ('PAYMENT_TERM', 'Thời hạn thanh toán'),
    ('DELIVERY_SCHEDULE', 'Lịch / thời gian giao hàng'),
    ('DELIVERY_PLACE', 'Địa điểm giao hàng'),
    ('SHIPPING_PARTY', 'Bên chịu phí vận chuyển'),
    ('WARRANTY_MONTHS', 'Thời hạn bảo hành (tháng)'),
    ('QUALITY_NOTES', 'Chất lượng / quy cách'),
    ('PACKAGING_NOTES', 'Bao bì / đóng gói'),
    ('NOTES', 'Ghi chú chung'),
)

REQUIRED_MARKERS = ('[[CONTRACT_NO]]', '[[ITEMS_TABLE]]', '[[TOTAL]]')

DEFAULT_TEMPLATE_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8"/>
<title>Hợp đồng mua bán hàng hóa [[CONTRACT_NO]]</title>
<style>
  @page { size: A4; margin: 18mm 16mm; }
  body { font-family: "Times New Roman", Times, serif; font-size: 13pt; color: #111; line-height: 1.45; }
  .center { text-align: center; }
  .right { text-align: right; }
  .muted { color: #555; font-size: 11pt; }
  h1 { font-size: 16pt; margin: .6rem 0 .2rem; letter-spacing: .04em; }
  h2 { font-size: 13pt; margin: 1rem 0 .4rem; }
  .party { margin: .6rem 0 1rem; }
  .party p { margin: .15rem 0; }
  table.items { width: 100%; border-collapse: collapse; margin: .5rem 0 1rem; font-size: 11pt; }
  table.items th, table.items td { border: 1px solid #333; padding: .28rem .35rem; vertical-align: top; }
  table.items th { background: #f3f3f3; text-align: center; }
  table.items .num { text-align: right; white-space: nowrap; }
  table.items .c { text-align: center; }
  .sign { display: flex; justify-content: space-between; margin-top: 2rem; gap: 1rem; }
  .sign .box { width: 45%; text-align: center; }
  .hint { background: #fff8e6; border: 1px dashed #c9a227; padding: .5rem .75rem; margin-bottom: 1rem; font-size: 10.5pt; }
  @media print { .hint, .no-print { display: none !important; } }
</style>
</head>
<body>

<div class="hint no-print">
  <b>Hướng dẫn chỉnh mẫu:</b> Giữ nguyên các mã dạng <code>[[TÊN_TRƯỜNG]]</code> (ví dụ <code>[[SELLER_NAME]]</code>,
  <code>[[ITEMS_TABLE]]</code>). Có thể sửa câu chữ điều khoản; khi nạp lại file HTML này, hệ thống vẫn điền dữ liệu từ form hợp đồng.
</div>

<p class="center" style="margin:0;font-weight:700">CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM</p>
<p class="center" style="margin:0">Độc lập - Tự do - Hạnh phúc</p>
<p class="center muted">———————</p>

<h1 class="center">HỢP ĐỒNG MUA BÁN HÀNG HÓA</h1>
<p class="center">Số: [[CONTRACT_NO]]</p>

<p>– Căn cứ Bộ luật Dân sự năm 2015 và các văn bản pháp luật liên quan;<br/>
– Căn cứ Luật Thương mại năm 2005 và các văn bản hướng dẫn;<br/>
– Căn cứ nhu cầu và khả năng của các bên.</p>

<p>Hôm nay, ngày [[DAY]] tháng [[MONTH]] năm [[YEAR]], tại [[PLACE]], chúng tôi gồm có:</p>

<div class="party">
  <p><b>BÊN A (Bên bán):</b></p>
  <p>Tên doanh nghiệp: <b>[[SELLER_NAME]]</b></p>
  <p>Mã số thuế: [[SELLER_TAX]]</p>
  <p>Địa chỉ: [[SELLER_ADDRESS]]</p>
  <p>Điện thoại: [[SELLER_PHONE]] &nbsp;|&nbsp; Email: [[SELLER_EMAIL]]</p>
  <p>Tài khoản: [[SELLER_BANK]] tại [[SELLER_BANK_NAME]]</p>
  <p>Đại diện: Ông/Bà <b>[[SELLER_REP]]</b> &nbsp;—&nbsp; Chức vụ: [[SELLER_TITLE]]</p>
</div>

<div class="party">
  <p><b>BÊN B (Bên mua):</b></p>
  <p>Tên: <b>[[BUYER_NAME]]</b></p>
  <p>Mã số thuế: [[BUYER_TAX]]</p>
  <p>Địa chỉ: [[BUYER_ADDRESS]]</p>
  <p>Điện thoại: [[BUYER_PHONE]] &nbsp;|&nbsp; Email: [[BUYER_EMAIL]]</p>
  <p>Đại diện: Ông/Bà <b>[[BUYER_REP]]</b> &nbsp;—&nbsp; Chức vụ: [[BUYER_TITLE]]</p>
</div>

<p>Hai bên thống nhất ký kết hợp đồng mua bán hàng hóa với các điều khoản như sau:</p>

<h2>Điều 1: Tên hàng – Số lượng – Giá trị hợp đồng</h2>
<p>Bên A bán cho Bên B các hàng hóa theo bảng dưới đây (đơn giá chưa bao gồm thuế GTGT, trừ khi ghi chú khác):</p>
[[ITEMS_TABLE]]
<p>Tổng giá trị hàng hóa chưa VAT: <b>[[SUBTOTAL]]</b> đồng<br/>
Tổng thuế GTGT (VAT): <b>[[VAT_AMOUNT]]</b> đồng<br/>
Tổng giá trị hợp đồng (đã gồm VAT): <b>[[TOTAL]]</b> đồng<br/>
Bằng chữ: <b>[[TOTAL_WORDS]]</b>.</p>

<h2>Điều 2: Chất lượng và quy cách hàng hóa</h2>
<p>[[QUALITY_NOTES]]</p>

<h2>Điều 3: Bao bì và ký mã hiệu</h2>
<p>[[PACKAGING_NOTES]]</p>

<h2>Điều 4: Thời gian, địa điểm và phương thức giao hàng</h2>
<p>1. Thời gian / lịch giao hàng: [[DELIVERY_SCHEDULE]]</p>
<p>2. Địa điểm giao hàng: [[DELIVERY_PLACE]]</p>
<p>3. Chi phí vận chuyển do bên [[SHIPPING_PARTY]] chịu.</p>
<p>4. Khi nhận hàng, Bên B có trách nhiệm kiểm tra số lượng, quy cách. Nếu phát hiện sai lệch phải lập biên bản và yêu cầu Bên A xác nhận.</p>

<h2>Điều 5: Bảo hành</h2>
<p>Bên A bảo hành hàng hóa trong thời hạn [[WARRANTY_MONTHS]] tháng kể từ ngày bàn giao, trừ hư hỏng do Bên B sử dụng sai hướng dẫn.</p>

<h2>Điều 6: Phương thức thanh toán</h2>
<p>Bên B thanh toán cho Bên A bằng hình thức [[PAYMENT_METHOD]] trong thời hạn [[PAYMENT_TERM]].</p>

<h2>Điều 7: Điều khoản chung</h2>
<p>1. Hai bên cam kết thực hiện đúng các điều khoản đã thỏa thuận.</p>
<p>2. Mọi tranh chấp được ưu tiên giải quyết bằng thương lượng; nếu không đạt, đưa ra Tòa án có thẩm quyền tại nơi Bên A đặt trụ sở.</p>
<p>3. Hợp đồng có hiệu lực kể từ ngày ký, lập thành 02 bản có giá trị pháp lý như nhau, mỗi bên giữ 01 bản.</p>
<p>[[NOTES]]</p>

<div class="sign">
  <div class="box">
    <p><b>ĐẠI DIỆN BÊN B</b><br/><span class="muted">(Ký, ghi rõ họ tên)</span></p>
    <p style="margin-top:4rem"><b>[[BUYER_REP]]</b></p>
  </div>
  <div class="box">
    <p><b>ĐẠI DIỆN BÊN A</b><br/><span class="muted">(Ký, ghi rõ họ tên)</span></p>
    <p style="margin-top:4rem"><b>[[SELLER_REP]]</b></p>
  </div>
</div>

</body>
</html>
"""


def _money(n: Any) -> str:
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f'{round(v):,.0f}'.replace(',', '.')


def _esc(s: Any) -> str:
    return html.escape('' if s is None else str(s), quote=True)


def _row(r) -> dict:
    if r is None:
        return {}
    if isinstance(r, dict):
        return r
    try:
        return dict(r)
    except Exception:
        return {}


def _load_business_info(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
        return _row(row)
    except sqlite3.Error:
        return {}


def get_template_html(conn: sqlite3.Connection) -> str:
    """Đọc mẫu tùy chỉnh của tenant hiện tại (conn phải từ get_db_connection)."""
    try:
        ensure_crm_schema(conn, commit=False)
        row = conn.execute(
            'SELECT value FROM crm_settings WHERE key = ?',
            (TEMPLATE_SETTING_KEY,),
        ).fetchone()
        if row:
            val = (_row(row).get('value') or '').strip()
            if val:
                return val
    except sqlite3.Error:
        pass
    return DEFAULT_TEMPLATE_HTML


def get_template_meta(conn: sqlite3.Connection) -> dict[str, Any]:
    """Metadata mẫu — dùng cho API (phạm vi tenant)."""
    body = get_template_html(conn)
    return {
        'html': body,
        'is_custom': body != DEFAULT_TEMPLATE_HTML,
        'storage': 'crm_settings',
        'setting_key': TEMPLATE_SETTING_KEY,
        'tenant_scoped': True,
    }


def set_template_html(conn: sqlite3.Connection, html_body: str) -> None:
    """Ghi mẫu vào DB tenant hiện tại — không ảnh hưởng tenant khác."""
    ensure_crm_schema(conn, commit=False)
    body = (html_body or '').strip()
    if not body:
        raise ValueError('Nội dung mẫu trống')
    errs = validate_template(body)
    if errs:
        raise ValueError('; '.join(errs))
    conn.execute(
        """
        INSERT INTO crm_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (TEMPLATE_SETTING_KEY, body),
    )


def reset_template(conn: sqlite3.Connection) -> None:
    ensure_crm_schema(conn, commit=False)
    conn.execute('DELETE FROM crm_settings WHERE key = ?', (TEMPLATE_SETTING_KEY,))


def validate_template(html_body: str) -> list[str]:
    errs: list[str] = []
    body = html_body or ''
    for marker in REQUIRED_MARKERS:
        if marker not in body:
            errs.append(f'Thiếu mã bắt buộc {marker}')
    return errs


def extract_placeholders(html_body: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(html_body or '')))


def build_items_rows_html(items: list[dict]) -> str:
    rows: list[str] = []
    for i, it in enumerate(items or [], 1):
        rows.append(
            '<tr>'
            f'<td class="c">{i}</td>'
            f'<td>{_esc(it.get("product_name"))}</td>'
            f'<td class="c">{_esc(it.get("unit") or "")}</td>'
            f'<td class="num">{_money(it.get("qty"))}</td>'
            f'<td class="num">{_money(it.get("unit_price"))}</td>'
            f'<td class="num">{_money(it.get("line_subtotal"))}</td>'
            f'<td class="c">{_money(it.get("tax_rate"))}%</td>'
            f'<td class="num">{_money(it.get("vat_amount"))}</td>'
            f'<td class="num">{_money(it.get("line_total"))}</td>'
            f'<td>{_esc(it.get("notes") or "")}</td>'
            '</tr>'
        )
    if not rows:
        rows.append(
            '<tr><td class="c">1</td><td colspan="9" class="muted">Chưa có hàng hóa</td></tr>'
        )
    return '\n'.join(rows)


def build_items_table_html(items: list[dict]) -> str:
    return (
        '<table class="items">'
        '<thead><tr>'
        '<th>STT</th><th>Tên hàng hóa</th><th>ĐVT</th><th>Số lượng</th>'
        '<th>Đơn giá</th><th>Thành tiền</th><th>%VAT</th><th>Tiền VAT</th>'
        '<th>Cộng sau VAT</th><th>Ghi chú</th>'
        '</tr></thead>'
        f'<tbody>{build_items_rows_html(items)}</tbody>'
        '</table>'
    )


def _parse_date_parts(raw: str | None) -> tuple[str, str, str, str]:
    s = (raw or '').strip()[:10]
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        y, m, d = s[0:4], s[5:7], s[8:10]
        return d, m, y, f'{d}/{m}/{y}'
    now = datetime.now()
    return (
        f'{now.day:02d}',
        f'{now.month:02d}',
        str(now.year),
        now.strftime('%d/%m/%Y'),
    )


def build_fill_context(conn: sqlite3.Connection, contract: dict) -> dict[str, str]:
    from helpers import so_thanh_chu

    info = _load_business_info(conn)
    day, month, year, signed_vi = _parse_date_parts(
        contract.get('signed_date') or contract.get('start_date')
    )
    items = contract.get('items') or []
    total = contract.get('amount')
    if total is None:
        total = contract.get('total') or 0
    try:
        total_words = so_thanh_chu(int(round(float(total or 0)))) + ' đồng'
    except Exception:
        total_words = ''

    seller_name = (
        info.get('business_name')
        or info.get('company_name')
        or info.get('name')
        or ''
    )
    return {
        'CONTRACT_NO': str(contract.get('contract_no') or ''),
        'SIGNED_DATE': signed_vi,
        'DAY': day,
        'MONTH': month,
        'YEAR': year,
        'PLACE': str(contract.get('place') or info.get('address') or 'trụ sở Bên A'),
        'SELLER_NAME': str(seller_name),
        'SELLER_TAX': str(info.get('tax_code') or info.get('mst') or ''),
        'SELLER_ADDRESS': str(info.get('address') or ''),
        'SELLER_PHONE': str(info.get('phone') or ''),
        'SELLER_EMAIL': str(info.get('email') or ''),
        'SELLER_BANK': str(info.get('bank_account') or ''),
        'SELLER_BANK_NAME': str(info.get('bank_name') or ''),
        'SELLER_REP': str(
            info.get('representative_name')
            or info.get('director_name')
            or info.get('owner_name')
            or ''
        ),
        'SELLER_TITLE': str(info.get('representative_title') or 'Giám đốc'),
        'BUYER_NAME': str(
            contract.get('customer_name')
            or contract.get('buyer_name')
            or ''
        ),
        'BUYER_TAX': str(contract.get('customer_tax_code') or ''),
        'BUYER_ADDRESS': str(contract.get('customer_address') or ''),
        'BUYER_PHONE': str(contract.get('customer_phone') or ''),
        'BUYER_EMAIL': str(contract.get('customer_email') or ''),
        'BUYER_REP': str(contract.get('buyer_rep') or ''),
        'BUYER_TITLE': str(contract.get('buyer_title') or ''),
        'SUBTOTAL': _money(contract.get('subtotal')),
        'VAT_AMOUNT': _money(contract.get('tax_amount')),
        'TOTAL': _money(total),
        'TOTAL_WORDS': total_words,
        'PAYMENT_METHOD': str(contract.get('payment_method') or '…'),
        'PAYMENT_TERM': str(contract.get('payment_term') or '…'),
        'DELIVERY_SCHEDULE': str(contract.get('delivery_schedule') or '…'),
        'DELIVERY_PLACE': str(contract.get('delivery_place') or '…'),
        'SHIPPING_PARTY': str(contract.get('shipping_party') or 'A'),
        'WARRANTY_MONTHS': str(contract.get('warranty_months') or '12'),
        'QUALITY_NOTES': str(
            contract.get('quality_notes')
            or 'Hàng hóa đúng chủng loại, quy cách đã thỏa thuận; chất lượng theo tiêu chuẩn nhà sản xuất.'
        ),
        'PACKAGING_NOTES': str(
            contract.get('packaging_notes')
            or 'Bao bì nguyên kiện, phù hợp vận chuyển; ký mã hiệu theo quy định của Bên A.'
        ),
        'NOTES': str(contract.get('notes') or ''),
        '_ITEMS': items,  # type: ignore[dict-item]
    }


def fill_template(html_body: str, ctx: dict[str, Any]) -> str:
    items = ctx.get('_ITEMS') or []
    out = html_body or ''
    # Bỏ dòng tiêu đề phụ nếu mẫu cũ còn [[CONTRACT_TITLE]]
    out = re.sub(
        r'<p[^>]*>\s*\[\[CONTRACT_TITLE\]\]\s*</p>\s*',
        '',
        out,
        flags=re.IGNORECASE,
    )
    out = out.replace('[[CONTRACT_TITLE]]', '')
    out = out.replace('[[ITEMS_TABLE]]', build_items_table_html(items))
    out = out.replace('[[ITEMS_ROWS]]', build_items_rows_html(items))

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in ('ITEMS_TABLE', 'ITEMS_ROWS'):
            return m.group(0)
        val = ctx.get(key)
        if val is None:
            return m.group(0)
        return _esc(val)

    return PLACEHOLDER_RE.sub(_repl, out)


def render_contract_html(conn: sqlite3.Connection, contract: dict) -> str:
    tpl = get_template_html(conn)
    ctx = build_fill_context(conn, contract)
    return fill_template(tpl, ctx)


def placeholders_guide() -> list[dict[str, str]]:
    return [{'code': f'[[{k}]]', 'label': lab} for k, lab in KNOWN_PLACEHOLDERS]
