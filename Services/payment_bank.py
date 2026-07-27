"""Tích hợp nhận biết chuyển khoản qua SePay / Casso (webhook + poll)."""
import json
import logging
import re
from datetime import datetime, timedelta

import requests

from db_utils import get_db_connection
from helpers import get_setting

logger = logging.getLogger(__name__)

SALE_CODE_RE = re.compile(r'DH(\d{1,10})', re.IGNORECASE)

# Mã BIN ngân hàng phổ biến tại Việt Nam (VietQR / Napas)
VN_BANKS = {
    '970436': 'Vietcombank',
    '970407': 'Techcombank',
    '970418': 'BIDV',
    '970415': 'VietinBank',
    '970405': 'Agribank',
    '970422': 'MB Bank',
    '970416': 'ACB',
    '970403': 'Sacombank',
    '970432': 'VPBank',
    '970423': 'TPBank',
    '970437': 'HDBank',
    '970443': 'SHB',
    '970431': 'Eximbank',
    '970426': 'MSB',
    '970448': 'OCB',
    '970449': 'LienVietPostBank',
    '970440': 'SeABank',
    '970441': 'VIB',
    '970409': 'Bac A Bank',
    '970412': 'PVcomBank',
    '970454': 'VietCapitalBank',
    '970428': 'Nam A Bank',
    '970419': 'NCB',
    '970414': 'OceanBank',
    '970427': 'VietABank',
    '970433': 'VietBank',
    '970438': 'BaoViet Bank',
    '970446': 'COOPBANK',
    '970452': 'Kienlongbank',
    '970429': 'SCB',
    '970400': 'Saigonbank',
}


def sale_payment_code(sale_id):
    return f"DH{int(sale_id):06d}"


def extract_sale_id_from_text(text):
    if not text:
        return None
    m = SALE_CODE_RE.search(str(text).upper())
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def amount_matches(expected, received, tolerance=None):
    exp = int(round(float(expected or 0)))
    rec = int(round(float(received or 0)))
    if tolerance is None:
        tol = int(get_setting('payment_amount_tolerance', '1000') or 1000)
    else:
        tol = int(tolerance)
    return abs(exp - rec) <= tol


def get_payment_config():
    provider = (get_setting('payment_provider', 'none') or 'none').strip().lower()
    biz = {}
    try:
        conn = get_db_connection()
        conn.row_factory = __import__('sqlite3').Row
        row = conn.execute(
            "SELECT bank_name, bank_account, bank_code, account_holder, business_name FROM business_info LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            biz = dict(row)
    except Exception:
        pass
    return {
        'provider': provider,
        'sepay_api_key': get_setting('sepay_api_key', ''),
        'casso_api_key': get_setting('casso_api_key', ''),
        'casso_webhook_token': get_setting('casso_webhook_token', ''),
        'payment_amount_tolerance': get_setting('payment_amount_tolerance', '1000'),
        'bank_name': biz.get('bank_name') or '',
        'bank_account': biz.get('bank_account') or '',
        'bank_code': biz.get('bank_code') or '',
        'account_holder': biz.get('account_holder') or '',
        'business_name': biz.get('business_name') or '',
    }


def validate_vietqr_setup(cfg=None):
    cfg = cfg or get_payment_config()
    missing = []
    if not (cfg.get('bank_account') or '').strip():
        missing.append('Số tài khoản ngân hàng')
    if not (cfg.get('bank_code') or '').strip():
        missing.append('Mã BIN ngân hàng')
    if not (cfg.get('account_holder') or '').strip():
        missing.append('Chủ tài khoản')
    return {
        'ready': len(missing) == 0,
        'missing': missing,
    }


def get_tax_default_bank_accounts():
    """STK mặc định cho phụ lục 01/BK-STK — tài khoản VietQR khi bán hàng (POS)."""
    cfg = get_payment_config()
    so_tk = (cfg.get('bank_account') or '').strip()
    if not so_tk:
        return []
    bank_name = (cfg.get('bank_name') or '').strip()
    if not bank_name:
        bank_code = str(cfg.get('bank_code') or '').strip()
        bank_name = VN_BANKS.get(bank_code, bank_code)
    return [{
        'ten_ddkd': (cfg.get('business_name') or '').strip(),
        'ma_ddkd': '',
        'so_tk': so_tk,
        'chu_tk': (cfg.get('account_holder') or '').strip(),
        'noi_mo': bank_name,
        'trang_thai': 'KhaiLanDau',
        'source': 'vietqr_pos',
    }]


def validate_payment_provider_setup(cfg=None):
    cfg = cfg or get_payment_config()
    provider = cfg.get('provider', 'none')
    if provider == 'none':
        return {'ready': True, 'mode': 'manual', 'message': 'Xác nhận thủ công (không dùng ngân hàng điện tử)'}
    if provider == 'sepay':
        if not (cfg.get('sepay_api_key') or '').strip():
            return {'ready': False, 'message': 'Chưa nhập SePay API Key'}
    elif provider == 'casso':
        if not (cfg.get('casso_api_key') or '').strip():
            return {'ready': False, 'message': 'Chưa nhập Casso API Key'}
    else:
        return {'ready': False, 'message': 'Nhà cung cấp không hợp lệ'}
    vqr = validate_vietqr_setup(cfg)
    if not vqr['ready']:
        return {'ready': False, 'message': 'Chưa đủ thông tin VietQR: ' + ', '.join(vqr['missing'])}
    return {'ready': True, 'mode': provider, 'message': f'Đã cấu hình {provider.upper()}'}


def get_full_payment_setup():
    cfg = get_payment_config()
    vqr = validate_vietqr_setup(cfg)
    provider = validate_payment_provider_setup(cfg)
    return {
        'success': True,
        'vietqr': {
            'bank_name': cfg.get('bank_name', ''),
            'bank_account': cfg.get('bank_account', ''),
            'bank_code': cfg.get('bank_code', ''),
            'account_holder': cfg.get('account_holder', ''),
            'ready': vqr['ready'],
            'missing': vqr['missing'],
        },
        'provider': cfg.get('provider', 'none'),
        'payment_amount_tolerance': cfg.get('payment_amount_tolerance', '1000'),
        'has_sepay_key': bool((cfg.get('sepay_api_key') or '').strip()),
        'has_casso_key': bool((cfg.get('casso_api_key') or '').strip()),
        'provider_status': provider,
        'banks': [{'bin': k, 'name': v} for k, v in sorted(VN_BANKS.items(), key=lambda x: x[1])],
    }


def test_provider_connection(provider=None):
    cfg = get_payment_config()
    provider = (provider or cfg.get('provider') or 'none').strip().lower()
    if provider == 'none':
        vqr = validate_vietqr_setup(cfg)
        if not vqr['ready']:
            return {'success': False, 'message': 'Chưa đủ thông tin VietQR', 'missing': vqr['missing']}
        return {'success': True, 'message': 'VietQR đã sẵn sàng (chế độ xác nhận thủ công)'}

    if provider == 'sepay':
        api_key = (cfg.get('sepay_api_key') or '').strip()
        if not api_key:
            return {'success': False, 'message': 'Chưa cấu hình SePay API Key'}
        account = (cfg.get('bank_account') or '').strip()
        txns = poll_sepay(api_key, account or None, limit=3)
        return {
            'success': True,
            'message': f'Kết nối SePay thành công. STK theo dõi: {account or "tất cả"}. '
                       f'Nhận {len(txns)} giao dịch gần nhất từ API.',
            'account': account,
            'sample_count': len(txns),
        }

    if provider == 'casso':
        api_key = (cfg.get('casso_api_key') or '').strip()
        if not api_key:
            return {'success': False, 'message': 'Chưa cấu hình Casso API Key'}
        headers = {'Authorization': f'Apikey {api_key}'}
        try:
            resp = requests.get('https://oauth.casso.vn/v2/sync/bank-accounts', headers=headers, timeout=15)
            if resp.status_code != 200:
                return {'success': False, 'message': f'Casso API lỗi HTTP {resp.status_code}'}
            data = resp.json()
            accounts = data.get('data') or []
            linked = [str(a.get('accountNumber', '')).strip() for a in accounts if isinstance(a, dict)]
            biz_acc = (cfg.get('bank_account') or '').strip()
            matched = biz_acc in linked if biz_acc and linked else None
            msg = f'Kết nối Casso thành công. {len(linked)} tài khoản liên kết.'
            if biz_acc:
                msg += f' STK VietQR: {biz_acc}'
                if matched is True:
                    msg += ' — khớp với Casso.'
                elif matched is False:
                    msg += ' — chưa thấy trong Casso, hãy liên kết đúng STK trên casso.vn.'
            return {'success': True, 'message': msg, 'linked_accounts': linked, 'account_matched': matched}
        except Exception as e:
            return {'success': False, 'message': f'Lỗi kết nối Casso: {e}'}

    return {'success': False, 'message': 'Nhà cung cấp không hợp lệ'}


def save_payment_settings(data):
    """Lưu cấu hình SePay/Casso vào bảng settings. Bỏ qua API key rỗng (giữ giá trị cũ)."""
    keys = {
        'payment_provider': str(data.get('payment_provider', 'none')).strip().lower(),
        'payment_amount_tolerance': str(data.get('payment_amount_tolerance', '1000')).strip() or '1000',
        'casso_webhook_token': str(data.get('casso_webhook_token', '') or '').strip(),
    }
    if keys['payment_provider'] not in ('none', 'sepay', 'casso'):
        keys['payment_provider'] = 'none'

    conn = get_db_connection()
    try:
        for key, val in keys.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))

        for key in ('sepay_api_key', 'casso_api_key'):
            val = str(data.get(key, '') or '').strip()
            if val:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))

        conn.commit()
        return True
    finally:
        conn.close()


def _record_bank_match(sale_id, provider, external_id, amount, content):
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_payment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER,
                provider TEXT,
                external_id TEXT,
                amount REAL,
                content TEXT,
                created_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO bank_payment_log (sale_id, provider, external_id, amount, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (sale_id, provider, str(external_id or ''), float(amount or 0), str(content or '')[:500],
              datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
    finally:
        conn.close()


def ensure_bank_transactions_table(conn=None):
    own = conn is None
    if own:
        conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            content TEXT,
            transaction_date TEXT,
            account_number TEXT,
            counterparty_name TEXT,
            counterparty_account TEXT,
            direction TEXT DEFAULT 'in',
            sale_id INTEGER,
            extracted_sale_id INTEGER,
            match_status TEXT DEFAULT 'unmatched',
            match_reason TEXT,
            source TEXT,
            raw_json TEXT,
            created_at TEXT,
            synced_at TEXT,
            UNIQUE(provider, external_id)
        )
    """)
    if own:
        conn.commit()
        conn.close()


def _txn_external_key(provider, txn):
    eid = str(txn.get('external_id') or '').strip()
    if eid:
        return eid
    import hashlib
    fp = '|'.join([
        provider,
        str(txn.get('amount') or 0),
        str(txn.get('content') or '')[:200],
        str(txn.get('transaction_date') or ''),
    ])
    return 'fp_' + hashlib.md5(fp.encode('utf-8')).hexdigest()[:20]


MATCH_STATUS_LABELS = {
    'matched': 'Đã khớp đơn',
    'partial': 'Khớp một phần',
    'unmatched': 'Chưa khớp',
    'ignored': 'Bỏ qua',
}


def ingest_bank_transaction(provider, txn, source='webhook'):
    """Lưu hoặc cập nhật giao dịch ngân hàng (idempotent theo provider+external_id)."""
    ensure_bank_transactions_table()
    external_id = _txn_external_key(provider, txn)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    content = str(txn.get('content') or '')
    raw = txn.get('raw')
    raw_json = json.dumps(raw, ensure_ascii=False) if raw else ''
    extracted_sale_id = extract_sale_id_from_text(content)

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id FROM bank_transactions WHERE provider = ? AND external_id = ?",
            (provider, external_id),
        ).fetchone()
        fields = (
            float(txn.get('amount') or 0),
            content[:2000],
            str(txn.get('transaction_date') or '')[:19],
            str(txn.get('account_number') or '')[:64],
            str(txn.get('counterparty_name') or '')[:255],
            str(txn.get('counterparty_account') or '')[:64],
            str(txn.get('direction') or 'in')[:8],
            extracted_sale_id,
            source,
            raw_json,
            now,
        )
        if row:
            conn.execute("""
                UPDATE bank_transactions SET
                    amount = ?, content = ?, transaction_date = ?, account_number = ?,
                    counterparty_name = ?, counterparty_account = ?, direction = ?,
                    extracted_sale_id = ?, source = ?, raw_json = ?, synced_at = ?
                WHERE id = ?
            """, fields + (row[0],))
            txn_id = row[0]
            is_new = False
        else:
            cur = conn.execute("""
                INSERT INTO bank_transactions (
                    provider, external_id, amount, content, transaction_date,
                    account_number, counterparty_name, counterparty_account, direction,
                    extracted_sale_id, match_status, source, raw_json, created_at, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unmatched', ?, ?, ?, ?)
            """, (
                provider, external_id,
                float(txn.get('amount') or 0),
                content[:2000],
                str(txn.get('transaction_date') or '')[:19],
                str(txn.get('account_number') or '')[:64],
                str(txn.get('counterparty_name') or '')[:255],
                str(txn.get('counterparty_account') or '')[:64],
                str(txn.get('direction') or 'in')[:8],
                extracted_sale_id,
                source,
                raw_json,
                now,
                now,
            ))
            txn_id = cur.lastrowid
            is_new = True
        conn.commit()
        return {'id': txn_id, 'is_new': is_new, 'external_id': external_id}
    finally:
        conn.close()


def _update_transaction_match(provider, external_id, sale_id, match_status, match_reason):
    if not external_id:
        return
    ensure_bank_transactions_table()
    conn = get_db_connection()
    try:
        conn.execute("""
            UPDATE bank_transactions SET
                sale_id = ?, match_status = ?, match_reason = ?, synced_at = ?
            WHERE provider = ? AND external_id = ?
        """, (
            sale_id,
            match_status,
            str(match_reason or '')[:255],
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            provider,
            str(external_id),
        ))
        conn.commit()
    finally:
        conn.close()


def _resolve_match_status(result):
    if result.get('completed'):
        return 'matched'
    if result.get('matched'):
        return 'partial'
    reason = result.get('reason') or ''
    if reason in ('no_sale_code',):
        return 'unmatched'
    return 'unmatched'


def process_bank_transaction(provider, txn, source='webhook'):
    """Ingest giao dịch rồi thử khớp với đơn pending."""
    ing = ingest_bank_transaction(provider, txn, source=source)
    ext_id = ing['external_id']
    content = txn.get('content') or ''
    amount = txn.get('amount')
    result = try_match_transaction(content, amount, ext_id, provider, update_bank_row=False)
    status = _resolve_match_status(result)
    reason = result.get('reason') or result.get('error') or ('completed' if result.get('completed') else '')
    _update_transaction_match(provider, ext_id, result.get('sale_id'), status, reason)
    return {**ing, **result, 'match_status': status}


def sync_bank_transactions_from_provider(limit=50):
    """Poll SePay/Casso và lưu toàn bộ giao dịch gần nhất."""
    cfg = get_payment_config()
    provider = (cfg.get('provider') or 'none').strip().lower()
    if provider == 'sepay':
        if not (cfg.get('sepay_api_key') or '').strip():
            return {'success': False, 'error': 'Chưa cấu hình SePay API Key'}
        txns = poll_sepay(cfg['sepay_api_key'], cfg.get('bank_account') or None, limit=limit)
    elif provider == 'casso':
        if not (cfg.get('casso_api_key') or '').strip():
            return {'success': False, 'error': 'Chưa cấu hình Casso API Key'}
        txns = poll_casso(cfg['casso_api_key'], page_size=limit)
    else:
        return {'success': False, 'error': 'Chưa bật SePay hoặc Casso trong Cài đặt thanh toán'}

    processed = []
    for txn in txns:
        processed.append(process_bank_transaction(provider, txn, source='sync'))

    matched = sum(1 for r in processed if r.get('completed'))
    return {
        'success': True,
        'provider': provider,
        'total': len(processed),
        'new_matched': matched,
        'details': processed,
    }


def list_bank_transactions(start_date=None, end_date=None, match_status=None, q=None, limit=200, offset=0):
    ensure_bank_transactions_table()
    conn = get_db_connection()
    conn.row_factory = __import__('sqlite3').Row
    try:
        sql = """
            SELECT bt.*,
                   s.sale_no, s.status AS sale_status, s.total_amount AS sale_amount,
                   s.payment_method, s.business_line, s.customer_name
            FROM bank_transactions bt
            LEFT JOIN sale s ON s.id = bt.sale_id
            WHERE 1=1
        """
        params = []
        if start_date:
            sql += " AND COALESCE(bt.transaction_date, bt.created_at, '') >= ?"
            params.append(start_date + ' 00:00:00')
        if end_date:
            sql += " AND COALESCE(bt.transaction_date, bt.created_at, '') <= ?"
            params.append(end_date + ' 23:59:59')
        if match_status:
            sql += " AND bt.match_status = ?"
            params.append(match_status)
        if q:
            like = f'%{q.strip()}%'
            sql += """ AND (
                bt.content LIKE ? OR bt.counterparty_name LIKE ?
                OR bt.counterparty_account LIKE ? OR bt.external_id LIKE ?
                OR CAST(bt.extracted_sale_id AS TEXT) LIKE ? OR s.sale_no LIKE ?
            )"""
            params.extend([like] * 6)

        count_sql = "SELECT COUNT(*) FROM (" + sql + ")"
        total = conn.execute(count_sql, params).fetchone()[0]

        sql += " ORDER BY COALESCE(bt.transaction_date, bt.created_at) DESC, bt.id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        rows = conn.execute(sql, params).fetchall()

        data = []
        for r in rows:
            item = dict(r)
            item['match_status_label'] = MATCH_STATUS_LABELS.get(item.get('match_status'), item.get('match_status'))
            item['payment_code'] = sale_payment_code(item['extracted_sale_id']) if item.get('extracted_sale_id') else ''
            try:
                item['raw'] = json.loads(item.pop('raw_json') or '{}')
            except Exception:
                item['raw'] = {}
            data.append(item)

        summary = conn.execute("""
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(amount), 0) AS total_amount,
                SUM(CASE WHEN match_status = 'matched' THEN 1 ELSE 0 END) AS matched_count,
                SUM(CASE WHEN match_status = 'unmatched' THEN 1 ELSE 0 END) AS unmatched_count
            FROM bank_transactions
        """).fetchone()

        return {
            'success': True,
            'data': data,
            'total': total,
            'summary': dict(summary) if summary else {},
        }
    finally:
        conn.close()


def get_bank_transaction_detail(txn_id):
    ensure_bank_transactions_table()
    conn = get_db_connection()
    conn.row_factory = __import__('sqlite3').Row
    try:
        row = conn.execute("""
            SELECT bt.*,
                   s.sale_no, s.status AS sale_status, s.total_amount AS sale_amount,
                   s.payment_method, s.business_line, s.customer_name, s.note AS sale_note,
                   s.created_at AS sale_created_at
            FROM bank_transactions bt
            LEFT JOIN sale s ON s.id = bt.sale_id
            WHERE bt.id = ?
        """, (txn_id,)).fetchone()
        if not row:
            return {'success': False, 'error': 'Không tìm thấy giao dịch'}
        item = dict(row)
        item['match_status_label'] = MATCH_STATUS_LABELS.get(item.get('match_status'), item.get('match_status'))
        item['payment_code'] = sale_payment_code(item['extracted_sale_id']) if item.get('extracted_sale_id') else ''
        try:
            item['raw'] = json.loads(item.pop('raw_json') or '{}')
        except Exception:
            item['raw'] = {}
        cfg = get_payment_config()
        item['provider_label'] = (item.get('provider') or '').upper()
        item['bank_account'] = cfg.get('bank_account') or ''
        item['bank_name'] = cfg.get('bank_name') or ''
        return {'success': True, 'data': item}
    finally:
        conn.close()


def complete_pending_bank_payment(sale_id, source='manual'):
    """Hoàn tất đơn pending theo business_line. Idempotent nếu đã completed."""
    conn = get_db_connection()
    conn.row_factory = __import__('sqlite3').Row
    cursor = conn.cursor()
    try:
        sale = cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            return {'success': False, 'error': 'Không tìm thấy hóa đơn'}
        if sale['status'] == 'completed':
            return {'success': True, 'already_completed': True, 'sale_id': sale_id}

        if sale['status'] != 'pending':
            return {'success': False, 'error': 'Đơn không ở trạng thái chờ thanh toán'}

        business_line = (sale['business_line'] or '').strip()
    finally:
        conn.close()

    if business_line == 'rental_service':
        from routes.rental import complete_rental_bank_payment
        result = complete_rental_bank_payment(sale_id)
    elif business_line == 'fb_service':
        from routes.fb import complete_fb_bank_payment
        result = complete_fb_bank_payment(sale_id)
    elif business_line == 'subscription_renewal':
        from Services.subscription_service import complete_subscription_renewal
        result = complete_subscription_renewal(sale_id)
    else:
        from routes.sale import complete_pos_bank_payment
        result = complete_pos_bank_payment(sale_id)

    if result.get('success'):
        logger.info("Hoàn tất thanh toán CK sale_id=%s source=%s line=%s", sale_id, source, business_line or 'pos')
    return result


def try_match_transaction(content, amount, external_id, provider, update_bank_row=True):
    """Khớp giao dịch ngân hàng với đơn pending và tự hoàn tất."""
    sale_id = extract_sale_id_from_text(content)
    if not sale_id:
        result = {'matched': False, 'reason': 'no_sale_code'}
        if update_bank_row:
            _update_transaction_match(provider, external_id, None, 'unmatched', 'no_sale_code')
        return result

    conn = get_db_connection()
    conn.row_factory = __import__('sqlite3').Row
    try:
        sale = conn.execute(
            "SELECT id, status, total_amount, payment_method FROM sale WHERE id = ?",
            (sale_id,)
        ).fetchone()
        if not sale:
            result = {'matched': False, 'reason': 'sale_not_found', 'sale_id': sale_id}
            if update_bank_row:
                _update_transaction_match(provider, external_id, sale_id, 'unmatched', 'sale_not_found')
            return result
        if sale['status'] != 'pending':
            result = {'matched': False, 'reason': 'not_pending', 'sale_id': sale_id}
            if update_bank_row:
                _update_transaction_match(provider, external_id, sale_id, 'unmatched', 'not_pending')
            return result
        if str(sale['payment_method']) != '112':
            result = {'matched': False, 'reason': 'not_bank_transfer', 'sale_id': sale_id}
            if update_bank_row:
                _update_transaction_match(provider, external_id, sale_id, 'unmatched', 'not_bank_transfer')
            return result
        if not amount_matches(sale['total_amount'], amount):
            result = {'matched': False, 'reason': 'amount_mismatch', 'sale_id': sale_id,
                    'expected': sale['total_amount'], 'received': amount}
            if update_bank_row:
                _update_transaction_match(provider, external_id, sale_id, 'unmatched', 'amount_mismatch')
            return result
    finally:
        conn.close()

    result = complete_pending_bank_payment(sale_id, source=f'{provider}_webhook')
    if result.get('success'):
        _record_bank_match(sale_id, provider, external_id, amount, content)
        if update_bank_row:
            _update_transaction_match(provider, external_id, sale_id, 'matched', 'completed')
        return {'matched': True, 'sale_id': sale_id, 'completed': True}
    partial = {'matched': True, 'sale_id': sale_id, 'completed': False, 'error': result.get('error')}
    if update_bank_row:
        _update_transaction_match(provider, external_id, sale_id, 'partial', result.get('error') or 'complete_failed')
    return partial


def parse_sepay_webhook(payload):
    if not payload:
        return []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = [payload]
    else:
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get('transferType', 'in')).lower() not in ('in', 'credit'):
            continue
        content = row.get('code') or row.get('content') or row.get('description') or ''
        amount = row.get('transferAmount') or row.get('amount') or 0
        external_id = row.get('id') or row.get('referenceCode') or ''
        txn_date = (
            row.get('transactionDate') or row.get('when') or row.get('created_at') or ''
        )
        out.append({
            'content': content,
            'amount': amount,
            'external_id': external_id,
            'transaction_date': str(txn_date)[:19] if txn_date else '',
            'account_number': str(row.get('accountNumber') or row.get('subAccount') or '').strip(),
            'counterparty_name': str(
                row.get('counterAccountName') or row.get('senderName') or row.get('gateway') or ''
            ).strip(),
            'counterparty_account': str(row.get('counterAccountNumber') or '').strip(),
            'direction': 'in',
            'raw': row,
        })
    return out


def parse_casso_webhook(payload):
    if not payload or not isinstance(payload, dict):
        return []
    data = payload.get('data')
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = [data]
    else:
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if int(row.get('amount', 0) or 0) <= 0:
            continue
        out.append({
            'content': row.get('description') or row.get('when') or '',
            'amount': row.get('amount') or 0,
            'external_id': row.get('id') or row.get('tid') or '',
            'transaction_date': str(row.get('when') or row.get('transactionDate') or '')[:19],
            'account_number': str(row.get('bank_sub_acc_id') or row.get('subAccId') or '').strip(),
            'counterparty_name': str(row.get('counterPartyName') or row.get('senderName') or '').strip(),
            'counterparty_account': str(row.get('counterPartyAccount') or '').strip(),
            'direction': 'in',
            'raw': row,
        })
    return out


def poll_sepay(api_key, account_number=None, limit=30):
    if not api_key:
        return []
    headers = {'Authorization': f'Bearer {api_key}'}
    params = {'limit': limit}
    if account_number:
        params['account_number'] = account_number
    try:
        resp = requests.get(
            'https://my.sepay.vn/userapi/transactions/list',
            headers=headers, params=params, timeout=15
        )
        if resp.status_code != 200:
            logger.warning('SePay poll HTTP %s: %s', resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        transactions = data.get('transactions') if isinstance(data, dict) else data
        if not isinstance(transactions, list):
            return []
        return parse_sepay_webhook(transactions)
    except Exception as e:
        logger.warning('SePay poll error: %s', e)
        return []


def poll_casso(api_key, page_size=30):
    if not api_key:
        return []
    headers = {'Authorization': f'Apikey {api_key}'}
    params = {'pageSize': page_size, 'sort': 'DESC'}
    try:
        resp = requests.get(
            'https://oauth.casso.vn/v2/transactions',
            headers=headers, params=params, timeout=15
        )
        if resp.status_code != 200:
            logger.warning('Casso poll HTTP %s: %s', resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        return parse_casso_webhook(data)
    except Exception as e:
        logger.warning('Casso poll error: %s', e)
        return []


def poll_and_match_pending(sale_id=None):
    """Poll provider và khớp giao dịch. Nếu sale_id có, chỉ kiểm tra mã đơn đó."""
    cfg = get_payment_config()
    provider = cfg['provider']
    if provider not in ('sepay', 'casso'):
        return []

    if provider == 'sepay':
        txns = poll_sepay(cfg['sepay_api_key'], cfg.get('bank_account') or None)
    else:
        txns = poll_casso(cfg['casso_api_key'])

    results = []
    target_code = sale_payment_code(sale_id) if sale_id else None

    for txn in txns:
        content = txn.get('content') or ''
        if target_code and target_code.upper() not in str(content).upper():
            sid = extract_sale_id_from_text(content)
            if sid != sale_id:
                continue
        r = process_bank_transaction(provider, txn, source='poll')
        if r.get('matched'):
            results.append(r)
            if sale_id and r.get('completed'):
                break
    return results


def get_sale_payment_status(sale_id):
    conn = get_db_connection()
    conn.row_factory = __import__('sqlite3').Row
    try:
        sale = conn.execute(
            "SELECT id, status, total_amount, payment_method, sale_no, business_line, note FROM sale WHERE id = ?",
            (sale_id,)
        ).fetchone()
        if not sale:
            return {'success': False, 'error': 'Không tìm thấy hóa đơn'}
        status = sale['status']
        cfg = get_payment_config()
        if status == 'pending' and sale['payment_method'] == '112' and cfg['provider'] in ('sepay', 'casso'):
            poll_and_match_pending(sale_id)
            sale = conn.execute(
                "SELECT id, status, total_amount, payment_method, sale_no, business_line, note FROM sale WHERE id = ?",
                (sale_id,)
            ).fetchone()
            status = sale['status']

        payload = {
            'success': True,
            'sale_id': sale_id,
            'status': status,
            'paid': status == 'completed',
            'sale_no': sale['sale_no'] or sale_payment_code(sale_id),
            'amount': float(sale['total_amount'] or 0),
            'payment_code': sale_payment_code(sale_id),
            'provider': cfg['provider'],
        }
        if status == 'completed' and (sale['business_line'] or '') == 'subscription_renewal':
            from Services.subscription_service import get_tenant_record, parse_renewal_note
            meta = parse_renewal_note(sale['note'])
            tenant_id = meta.get('tenant_id')
            tenant = get_tenant_record(tenant_id, include_inactive=True) if tenant_id else None
            payload['renewal'] = {
                'tenant_id': tenant_id,
                'expiry_date': tenant.get('expiry_date') if tenant else None,
                'plan_code': meta.get('plan'),
            }
        return payload
    finally:
        conn.close()
