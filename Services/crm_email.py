# -*- coding: utf-8 -*-
"""CRM email theo tenant — SMTP riêng từng DN, gửi báo giá / HĐ / chiến dịch."""
from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from Services.crm_ops import get_setting, set_setting
from Services.crm_schema import ensure_crm_schema
from Services.email_service import get_smtp_config as get_system_smtp_config

logger = logging.getLogger(__name__)

SMTP_KEYS = (
    'smtp_enabled',
    'smtp_server',
    'smtp_port',
    'smtp_sender',
    'smtp_password',
    'smtp_from_name',
)


def _row(r) -> dict:
    if not r:
        return {}
    return dict(r) if hasattr(r, 'keys') else {}


def get_tenant_smtp(conn: sqlite3.Connection) -> dict[str, Any]:
    """Đọc SMTP tenant (crm_settings). Không lộ password đầy đủ ra API (dùng mask)."""
    ensure_crm_schema(conn, commit=False)
    enabled = get_setting(conn, 'smtp_enabled', '0') in ('1', 'true', 'yes', 'on')
    server = get_setting(conn, 'smtp_server', '')
    port_s = get_setting(conn, 'smtp_port', '587') or '587'
    try:
        port = int(port_s)
    except ValueError:
        port = 587
    sender = get_setting(conn, 'smtp_sender', '')
    password = get_setting(conn, 'smtp_password', '')
    from_name = get_setting(conn, 'smtp_from_name', '')
    return {
        'enabled': enabled,
        'server': server,
        'port': port,
        'sender': sender,
        'password': password,
        'from_name': from_name,
        'configured': bool(enabled and server and sender and password),
        'password_set': bool(password),
    }


def get_tenant_smtp_public(conn: sqlite3.Connection) -> dict[str, Any]:
    cfg = get_tenant_smtp(conn)
    return {
        'enabled': cfg['enabled'],
        'server': cfg['server'],
        'port': cfg['port'],
        'sender': cfg['sender'],
        'from_name': cfg['from_name'],
        'configured': cfg['configured'],
        'password_set': cfg['password_set'],
        'password': '********' if cfg['password_set'] else '',
    }


def save_tenant_smtp(conn: sqlite3.Connection, data: dict) -> dict[str, Any]:
    ensure_crm_schema(conn, commit=False)
    enabled = data.get('enabled')
    if enabled is not None:
        set_setting(conn, 'smtp_enabled', '1' if enabled in (True, 1, '1', 'true', 'yes', 'on') else '0')
    if 'server' in data:
        set_setting(conn, 'smtp_server', str(data.get('server') or '').strip())
    if 'port' in data:
        try:
            port = int(data.get('port') or 587)
        except (TypeError, ValueError):
            port = 587
        set_setting(conn, 'smtp_port', str(port))
    if 'sender' in data:
        set_setting(conn, 'smtp_sender', str(data.get('sender') or '').strip())
    if 'from_name' in data:
        set_setting(conn, 'smtp_from_name', str(data.get('from_name') or '').strip())
    pw = data.get('password')
    if pw is not None and str(pw).strip() and str(pw).strip() != '********':
        set_setting(conn, 'smtp_password', str(pw).strip())
    return get_tenant_smtp_public(conn)


def resolve_send_config(conn: sqlite3.Connection) -> tuple[dict[str, Any] | None, str | None]:
    """Ưu tiên SMTP tenant; fallback hệ thống (.env) nếu tenant chưa bật."""
    t = get_tenant_smtp(conn)
    if t['configured']:
        return {
            'server': t['server'],
            'port': t['port'],
            'sender': t['sender'],
            'password': t['password'],
            'from_name': t['from_name'],
            'source': 'tenant',
        }, None
    if t['enabled'] and not t['configured']:
        return None, 'Đã bật SMTP doanh nghiệp nhưng thiếu server / email gửi / mật khẩu ứng dụng.'
    sys_cfg = get_system_smtp_config()
    if sys_cfg.get('sender') and sys_cfg.get('password'):
        return {
            'server': sys_cfg['server'],
            'port': sys_cfg['port'],
            'sender': sys_cfg['sender'],
            'password': sys_cfg['password'],
            'from_name': '',
            'source': 'system',
        }, None
    return None, (
        'Chưa cấu hình email gửi. Vào Thiết lập hệ thống (/thiet-lap) → Email doanh nghiệp '
        'hoặc cấu hình SMTP hệ thống trên VPS (.env).'
    )


def send_with_config(
    cfg: dict[str, Any],
    to_email: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
) -> tuple[bool, str | None]:
    to_email = (to_email or '').strip()
    if not to_email:
        return False, 'Email người nhận trống'
    msg = EmailMessage()
    msg['Subject'] = subject
    from_name = (cfg.get('from_name') or '').strip()
    sender = cfg['sender']
    msg['From'] = formataddr((from_name, sender)) if from_name else sender
    msg['To'] = to_email
    msg.set_content(body or '(xem bản HTML)')
    if html_body:
        msg.add_alternative(html_body, subtype='html')
    try:
        with smtplib.SMTP(cfg['server'], int(cfg['port']), timeout=25) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg['sender'], cfg['password'])
            server.send_message(msg)
        return True, None
    except smtplib.SMTPAuthenticationError as exc:
        logger.error('CRM SMTP auth: %s', exc)
        return False, 'Xác thực SMTP thất bại. Kiểm tra email và mật khẩu ứng dụng.'
    except Exception as exc:
        logger.error('CRM SMTP send: %s', exc)
        return False, str(exc)


def send_tenant_email(
    conn: sqlite3.Connection,
    to_email: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
) -> tuple[bool, str | None, str]:
    """Trả (ok, error, source) — source = tenant|system."""
    cfg, err = resolve_send_config(conn)
    if err or not cfg:
        return False, err or 'Chưa cấu hình SMTP', ''
    ok, e = send_with_config(cfg, to_email, subject, body, html_body=html_body)
    return ok, e, cfg.get('source') or ''


def test_tenant_smtp(conn: sqlite3.Connection, to_email: str | None = None) -> tuple[bool, str | None]:
    cfg, err = resolve_send_config(conn)
    if err or not cfg:
        return False, err
    dest = (to_email or cfg['sender'] or '').strip()
    if not dest:
        return False, 'Nhập email nhận thử'
    return send_with_config(
        cfg,
        dest,
        '[KETO] Kiểm tra cấu hình email doanh nghiệp',
        'Email thử từ hệ thống KETO. Nếu nhận được thư này, SMTP đã hoạt động.',
        html_body='<p>Email thử từ <b>KETO</b>. SMTP doanh nghiệp hoạt động.</p>',
    )


def _money(n: Any) -> str:
    try:
        return f'{round(float(n or 0)):,.0f}'.replace(',', '.')
    except (TypeError, ValueError):
        return '0'


def _customer_email(conn: sqlite3.Connection, customer_id: int | None) -> tuple[str, str]:
    if not customer_id:
        return '', ''
    try:
        row = conn.execute(
            """
            SELECT COALESCE(company_name, name, '') AS cname, COALESCE(email, '') AS email
            FROM customers WHERE id = ?
            """,
            (int(customer_id),),
        ).fetchone()
        d = _row(row)
        return (d.get('email') or '').strip(), (d.get('cname') or '').strip()
    except sqlite3.Error:
        return '', ''


def build_quote_email(quote: dict) -> tuple[str, str, str]:
    """subject, text, html."""
    qno = quote.get('quote_no') or ''
    cust = quote.get('customer_name') or f"#{quote.get('customer_id') or ''}"
    subject = f'Báo giá {qno} — {cust}'
    items = quote.get('items') or []
    rows_txt = []
    rows_html = []
    for i, it in enumerate(items, 1):
        name = it.get('product_name') or ''
        unit = it.get('unit') or ''
        qty = it.get('qty') or 0
        price = it.get('unit_price') or 0
        tax = it.get('tax_rate') or 0
        line = float(qty or 0) * float(price or 0)
        vat = line * float(tax or 0) / 100
        rows_txt.append(f'{i}. {name} | {unit} | SL {qty} | ĐG {_money(price)} | VAT {tax}% | {_money(line + vat)}')
        rows_html.append(
            f'<tr><td>{i}</td><td>{name}</td><td>{unit}</td>'
            f'<td style="text-align:right">{_money(qty)}</td>'
            f'<td style="text-align:right">{_money(price)}</td>'
            f'<td style="text-align:right">{tax}%</td>'
            f'<td style="text-align:right">{_money(line + vat)}</td></tr>'
        )
    total = quote.get('total') or 0
    text = (
        f'Kính gửi Quý khách {cust},\n\n'
        f'Chúng tôi gửi báo giá số {qno}.\n\n'
        + '\n'.join(rows_txt)
        + f'\n\nTổng (đã gồm VAT): {_money(total)} đ\n'
        f'Ghi chú: {quote.get("notes") or "—"}\n\nTrân trọng.'
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#111">
      <p>Kính gửi Quý khách <b>{cust}</b>,</p>
      <p>Chúng tôi gửi <b>Báo giá {qno}</b>.</p>
      <table style="border-collapse:collapse;width:100%;margin:12px 0" border="1" cellpadding="6">
        <thead style="background:#f1f5f9"><tr>
          <th>STT</th><th>Hàng hóa</th><th>ĐVT</th><th>SL</th><th>Đơn giá</th><th>%VAT</th><th>Thành tiền</th>
        </tr></thead>
        <tbody>{''.join(rows_html) or '<tr><td colspan="7">—</td></tr>'}</tbody>
      </table>
      <p><b>Tổng thanh toán (đã gồm VAT): {_money(total)} đ</b></p>
      <p>Ghi chú: {quote.get('notes') or '—'}</p>
      <p>Trân trọng.</p>
    </div>
    """
    return subject, text, html


def build_contract_email(contract: dict, html_doc: str | None = None) -> tuple[str, str, str]:
    cno = contract.get('contract_no') or ''
    cust = contract.get('customer_name') or f"#{contract.get('customer_id') or ''}"
    subject = f'Hợp đồng {cno} — {cust}'
    total = contract.get('amount') or 0
    text = (
        f'Kính gửi Quý khách {cust},\n\n'
        f'Chúng tôi gửi Hợp đồng mua bán hàng hóa số {cno}.\n'
        f'Giá trị: {_money(total)} đ\n\n'
        f'Trân trọng.'
    )
    if html_doc:
        html = html_doc
    else:
        html = f"""
        <div style="font-family:Arial,sans-serif;font-size:14px">
          <p>Kính gửi Quý khách <b>{cust}</b>,</p>
          <p>Chúng tôi gửi <b>Hợp đồng {cno}</b> — giá trị <b>{_money(total)} đ</b>.</p>
          <p>Trân trọng.</p>
        </div>
        """
    return subject, text, html


def build_campaign_email(campaign: dict, body_extra: str = '') -> tuple[str, str, str]:
    name = campaign.get('name') or 'Ưu đãi'
    subject = campaign.get('email_subject') or f'Thông tin chương trình: {name}'
    channel = campaign.get('channel') or ''
    notes = campaign.get('notes') or ''
    custom = (body_extra or campaign.get('email_body') or '').strip()
    text = custom or (
        f'Kính gửi Quý khách,\n\n'
        f'Chúng tôi xin gửi thông tin chương trình «{name}»'
        f'{(" (kênh " + channel + ")") if channel else ""}.\n\n'
        f'{notes}\n\nTrân trọng.'
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#111">
      <p>Kính gửi Quý khách,</p>
      <p>Chương trình: <b>{name}</b>{f' · Kênh {channel}' if channel else ''}.</p>
      <div style="white-space:pre-wrap;margin:12px 0">{custom or notes or '—'}</div>
      <p>Trân trọng.</p>
    </div>
    """
    return subject, text, html


def list_customer_emails(
    conn: sqlite3.Connection,
    *,
    customer_ids: list[int] | None = None,
    limit: int = 500,
) -> list[dict]:
    ensure_crm_schema(conn, commit=False)
    sql = """
        SELECT id, COALESCE(company_name, name, '') AS name, email
        FROM customers
        WHERE email IS NOT NULL AND TRIM(email) != ''
    """
    params: list[Any] = []
    if customer_ids:
        ph = ','.join('?' for _ in customer_ids)
        sql += f' AND id IN ({ph})'
        params.extend(int(x) for x in customer_ids)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(int(limit))
    return [_row(r) for r in conn.execute(sql, params).fetchall()]


def log_crm_email(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref_id: int | None,
    to_email: str,
    subject: str,
    status: str,
    error: str | None = None,
) -> None:
    ensure_crm_schema(conn, commit=False)
    try:
        from Services.crm_schema import ensure_crm_email_logs
        ensure_crm_email_logs(conn)
        conn.execute(
            """
            INSERT INTO crm_email_logs (kind, ref_id, to_email, subject, status, error)
            VALUES (?,?,?,?,?,?)
            """,
            (kind, ref_id, to_email, subject, status, error),
        )
    except sqlite3.Error as exc:
        logger.warning('crm_email_logs: %s', exc)


def _business_name(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute(
            "SELECT COALESCE(business_name, '') AS n FROM business_info LIMIT 1"
        ).fetchone()
        if row:
            d = _row(row)
            return (d.get('n') or '').strip()
    except sqlite3.Error:
        pass
    return ''


def resolve_supplier_email(
    conn: sqlite3.Connection,
    *,
    supplier_id: int | None = None,
    supplier_name: str | None = None,
    supplier_tax_code: str | None = None,
) -> str:
    """Email NCC — ưu tiên supplier_id, fallback theo MST / tên."""
    try:
        if supplier_id:
            row = conn.execute(
                "SELECT COALESCE(email, '') AS email FROM suppliers WHERE id = ?",
                (int(supplier_id),),
            ).fetchone()
            em = (_row(row).get('email') or '').strip()
            if em:
                return em
        tax = (supplier_tax_code or '').strip()
        if tax:
            row = conn.execute(
                """
                SELECT email FROM suppliers
                WHERE TRIM(COALESCE(tax_code, '')) = ?
                  AND TRIM(COALESCE(email, '')) != ''
                LIMIT 1
                """,
                (tax,),
            ).fetchone()
            em = (_row(row).get('email') or '').strip()
            if em:
                return em
        name = (supplier_name or '').strip()
        if name:
            row = conn.execute(
                """
                SELECT email FROM suppliers
                WHERE TRIM(name) = ?
                  AND TRIM(COALESCE(email, '')) != ''
                LIMIT 1
                """,
                (name,),
            ).fetchone()
            em = (_row(row).get('email') or '').strip()
            if em:
                return em
    except sqlite3.Error:
        pass
    return ''


def build_purchase_order_email(
    po: dict,
    *,
    business_name: str = '',
) -> tuple[str, str, str]:
    """subject, text, html — đơn đặt hàng gửi NCC."""
    po_no = po.get('po_no') or ''
    sup = po.get('supplier_name') or 'Quý NCC'
    po_date = (po.get('po_date') or '')[:10]
    exp = (po.get('expected_date') or '')[:10]
    biz = (business_name or '').strip() or 'Chúng tôi'
    subject = f'Đơn đặt hàng {po_no} — {biz}'
    lines = po.get('lines') or []
    rows_txt = []
    rows_html = []
    for i, ln in enumerate(lines, 1):
        name = ln.get('product_name') or ''
        unit = ln.get('unit') or ''
        qty = ln.get('qty') or 0
        price = ln.get('unit_price') or 0
        amt = float(qty or 0) * float(price or 0)
        rows_txt.append(
            f'{i}. {name} | {unit} | SL {_money(qty)} | ĐG {_money(price)} | TT {_money(amt)}'
        )
        rows_html.append(
            f'<tr><td>{i}</td><td>{name}</td><td>{unit}</td>'
            f'<td style="text-align:right">{_money(qty)}</td>'
            f'<td style="text-align:right">{_money(price)}</td>'
            f'<td style="text-align:right">{_money(amt)}</td></tr>'
        )
    total = po.get('total_amount') or 0
    note = po.get('note') or '—'
    exp_line = f'Ngày giao dự kiến: {exp}\n' if exp else ''
    text = (
        f'Kính gửi {sup},\n\n'
        f'{biz} gửi đơn đặt hàng số {po_no} ngày {po_date}.\n'
        f'{exp_line}\n'
        + '\n'.join(rows_txt)
        + f'\n\nTổng giá trị: {_money(total)} đ\n'
        f'Ghi chú: {note}\n\n'
        f'Vui lòng xác nhận đơn và thông báo khi giao hàng / xuất hóa đơn.\n'
        f'Khi xuất HĐ, vui lòng ghi số đơn {po_no} vào mục GChu (ghi chú) trên hóa đơn điện tử.\n\nTrân trọng.'
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;font-size:14px;color:#111">
      <p>Kính gửi <b>{sup}</b>,</p>
      <p><b>{biz}</b> gửi <b>Đơn đặt hàng {po_no}</b> ngày {po_date}.</p>
      {f'<p>Ngày giao dự kiến: <b>{exp}</b></p>' if exp else ''}
      <table style="border-collapse:collapse;width:100%;margin:12px 0" border="1" cellpadding="6">
        <thead style="background:#f1f5f9"><tr>
          <th>STT</th><th>Hàng hóa</th><th>ĐVT</th><th>SL</th><th>Đơn giá</th><th>Thành tiền</th>
        </tr></thead>
        <tbody>{''.join(rows_html) or '<tr><td colspan="6">—</td></tr>'}</tbody>
      </table>
      <p><b>Tổng giá trị: {_money(total)} đ</b></p>
      <p>Ghi chú: {note}</p>
      <p>Vui lòng xác nhận đơn và thông báo khi giao hàng / xuất hóa đơn.</p>
      <p><em>Khi xuất HĐ, vui lòng ghi số đơn <b>{po_no}</b> vào mục <b>GChu</b> (ghi chú) trên hóa đơn điện tử.</em></p>
      <p>Trân trọng.</p>
    </div>
    """
    return subject, text, html
