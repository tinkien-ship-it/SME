# -*- coding: utf-8 -*-
"""Đồng bộ hóa đơn mua (đầu vào) qua API Mắt Bảo Purchase Inv.

Hai nguồn:
  - portal: GET /hoa-don-dau-vao/load-data  (kênh HĐĐT Mắt Bảo)
  - tct:    captcha + login-tct + load-data-tct (cổng CQT)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import requests
from dateutil import parser
from dateutil.relativedelta import relativedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

SOURCE_PORTAL = 'portal'
SOURCE_TCT = 'tct'
SOURCE_BOTH = 'both'
VALID_SOURCES = frozenset({SOURCE_PORTAL, SOURCE_TCT, SOURCE_BOTH})

# Tải HĐ mua: đúng 1 tháng/request (đầu → cuối tháng). Không chia phase.
FULL_MONTH_TIMEOUT_SEC = 120
FULL_MONTH_RETRY_TIMEOUT_SEC = 180


def _month_range(month_str: str) -> tuple[str, str]:
    """Trả (fromDate, toDate) đúng đầu → cuối tháng kế toán.

    Lưu ý: không dùng ``dt + 1 month`` khi ``dt`` còn ngày trong tháng
    (vd. parse ``08/2026`` thành 23/08 → toDate bị lệch sang 22/09).
    """
    raw = str(month_str).strip().replace('/', '-')
    dt = parser.parse(raw, fuzzy=True)
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + relativedelta(months=1)) - timedelta(seconds=1)
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d 23:59:59')


def resolve_purchase_base_url(config: dict) -> str:
    """Ưu tiên purchase_api_url; fallback api_url (cùng host nếu MB gộp API)."""
    for key in ('purchase_api_url', 'api_url'):
        url = (config.get(key) or '').strip().rstrip('/')
        if url:
            return url
    raise ValueError('Thiếu API URL hóa đơn đầu vào (purchase_api_url / api_url)')


class MatbaoPurchaseProvider:
    def __init__(self, config: dict):
        self.config = config or {}
        self.base_url = resolve_purchase_base_url(self.config)
        self.api_key = (self.config.get('api_key') or '').strip()
        self.name = self.config.get('name') or self.config.get('provider_name') or 'matbao_purchase'

        if not self.api_key:
            raise ValueError(f'Thiếu api_key cho {self.name}')

        self._bearer_token = None
        self.session = requests.Session()
        # Không retry khi read timeout — tránh treo 3×45s khi demo API chậm.
        # Chỉ retry lỗi mạng/connect và 5xx.
        retries = Retry(
            total=2,
            connect=2,
            read=False,
            status=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS']),
        )
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        self.session.mount('http://', HTTPAdapter(max_retries=retries))

    def _get_bearer_token(self) -> bool:
        if self._bearer_token:
            return True
        try:
            url = f'{self.base_url}/auth/token'
            r = self.session.post(url, json={'token': self.api_key}, timeout=15, verify=False)
            r.raise_for_status()
            data = r.json()
            if data.get('Success') and data.get('Data'):
                self._bearer_token = data['Data']
                return True
            logger.error('[%s] Server từ chối cấp token: %s', self.name, data.get('Data'))
            return False
        except Exception as exc:
            logger.error('[%s] Lỗi lấy Bearer token: %s', self.name, exc)
            return False

    def _get_headers(self) -> dict:
        if not self._bearer_token and not self._get_bearer_token():
            raise RuntimeError('Không thể xác thực API Mắt Bảo (Bearer Token)')
        return {
            'Authorization': f'Bearer {self._bearer_token}',
            'Content-Type': 'application/json',
        }

    def _request(self, method: str, path: str, *, json_body=None, timeout=60, _retry=True):
        headers = self._get_headers()
        url = f'{self.base_url}{path}'
        r = self.session.request(
            method=method,
            url=url,
            json=json_body,
            headers=headers,
            timeout=timeout,
            verify=False,
        )
        if r.status_code == 401 and _retry:
            self._bearer_token = None
            return self._request(method, path, json_body=json_body, timeout=timeout, _retry=False)
        return r

    def get_tct_captcha(self) -> dict:
        try:
            r = self._request('GET', '/hoa-don-dau-vao/get-captcha', timeout=20)
            r.raise_for_status()
            data = r.json()
            if not data.get('Success'):
                return {'success': False, 'error': data.get('Data') or 'Không lấy được captcha'}
            payload = data.get('Data') or {}
            key = payload.get('key') or payload.get('ckey') or ''
            content = payload.get('content') or payload.get('captcha') or ''
            return {'success': True, 'key': key, 'content': content}
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

    def _load_etax_row(self) -> dict | None:
        from db_utils import get_db_connection
        from Services.einvoice_registry import normalize_provider_code

        provider_key = normalize_provider_code(
            self.config.get('provider_name') or self.config.get('name') or 'matbao'
        )
        conn = get_db_connection()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT tax_code, etax_password, etax_cvalue, etax_ckey
                FROM invoice_settings
                WHERE LOWER(TRIM(provider_name)) = ?
                LIMIT 1
                """,
                (provider_key,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT tax_code, etax_password, etax_cvalue, etax_ckey
                    FROM invoice_settings WHERE is_active = 1 LIMIT 1
                    """
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _persist_captcha(self, cvalue: str, ckey: str) -> None:
        from db_utils import get_db_connection, with_sqlite_write
        from Services.einvoice_registry import normalize_provider_code

        provider_key = normalize_provider_code(
            self.config.get('provider_name') or self.config.get('name') or 'matbao'
        )
        conn = get_db_connection()

        def _write(c: sqlite3.Connection) -> None:
            c.execute(
                """
                UPDATE invoice_settings
                SET etax_cvalue = ?, etax_ckey = ?, updated_at = datetime('now')
                WHERE LOWER(TRIM(provider_name)) = ?
                """,
                (cvalue, ckey, provider_key),
            )
            if c.total_changes == 0:
                c.execute(
                    """
                    UPDATE invoice_settings
                    SET etax_cvalue = ?, etax_ckey = ?, updated_at = datetime('now')
                    WHERE is_active = 1
                    """,
                    (cvalue, ckey),
                )

        with_sqlite_write(conn, _write, commit=True, label='persist_etax_captcha')

    def login_tct(
        self,
        *,
        cvalue: str | None = None,
        ckey: str | None = None,
        password: str | None = None,
        username: str | None = None,
        persist_captcha: bool = True,
    ) -> dict:
        try:
            row = self._load_etax_row() or {}
            payload = {
                'username': (username if username is not None else str(row.get('tax_code') or '')).strip(),
                'password': (password if password is not None else str(row.get('etax_password') or '')).strip(),
                'cvalue': (cvalue if cvalue is not None else str(row.get('etax_cvalue') or '')).strip(),
                'ckey': (ckey if ckey is not None else str(row.get('etax_ckey') or '')).strip(),
            }
            if not payload['username'] or not payload['password']:
                return {
                    'success': False,
                    'error': 'Thiếu MST hoặc mật khẩu eTax trong Settings → HĐĐT',
                    'need_captcha': False,
                }
            if not payload['cvalue'] or not payload['ckey']:
                return {
                    'success': False,
                    'error': 'Cần captcha CQT (ckey + cvalue) trước khi đăng nhập cổng thuế',
                    'need_captcha': True,
                }

            r = self._request(
                'POST',
                '/hoa-don-dau-vao/login-tct',
                json_body=payload,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if data.get('Success'):
                if persist_captcha:
                    try:
                        self._persist_captcha(payload['cvalue'], payload['ckey'])
                    except Exception as exc:
                        logger.warning('[%s] Không lưu captcha: %s', self.name, exc)
                return {'success': True, 'message': data.get('Data')}
            err = data.get('Data') or 'Đăng nhập CQT thất bại'
            need = 'captcha' in str(err).lower() or 'mã xác nhận' in str(err).lower()
            return {'success': False, 'error': err, 'need_captcha': need}
        except Exception as exc:
            logger.error('[%s] Lỗi Login TCT: %s', self.name, exc)
            return {'success': False, 'error': str(exc), 'need_captcha': False}

    def _build_load_payload(self, path: str, from_date: str, to_date: str) -> dict:
        """Theo Matbao Purchase Inv API.docx.

        typeDataPDF=0: không nhúng PDF Base64 (tài liệu mặc định).
        typeDataPDF=1 làm response cực lớn → demo API hay ReadTimeout.
        PDF lấy sau qua LinkDownloadPDF khi cần.
        """
        payload = {
            'comName': '',
            'comTaxCode': '',
            'no': 0,
            'fromDateYMD': from_date,
            'toDateYMD': to_date,
            'trangthai': -1,
            'pattern': '',
            'serial': '',
            'typeDataPDF': 0,
        }
        if path.endswith('load-data-tct'):
            payload['loaihoadon'] = -1
        else:
            # 2 = tìm theo ngày lập (đồng bộ theo tháng kế toán)
            payload['typeSearchDate'] = 2
        return payload

    def _parse_load_response(self, resp: dict) -> dict:
        if resp.get('Success'):
            invoices = resp.get('Data') or []
            if not isinstance(invoices, list):
                invoices = []
            return {'success': True, 'invoices': invoices, 'count': len(invoices)}
        err = resp.get('Data') or resp.get('Message') or 'API trả về Success=False'
        need = any(x in str(err).lower() for x in ('đăng nhập', 'login', 'session', 'captcha'))
        return {'success': False, 'error': err, 'need_login': need, 'invoices': []}

    def _load_invoices_once(self, path: str, from_date: str, to_date: str, *, timeout: int = FULL_MONTH_TIMEOUT_SEC) -> dict:
        payload = self._build_load_payload(path, from_date, to_date)
        try:
            r = self._request('GET', path, json_body=payload, timeout=timeout)
            r.raise_for_status()
            return self._parse_load_response(r.json())
        except requests.exceptions.ReadTimeout as exc:
            logger.warning(
                '[%s] Timeout %s %s→%s (timeout=%ss): %s',
                self.name, path, from_date, to_date, timeout, exc,
            )
            return {
                'success': False,
                'error': (
                    f'Mắt Bão không trả danh sách HĐ trong {timeout}s '
                    f'({from_date[:10]} → {to_date[:10]}). '
                    'Có thể server/demo Mắt Bão đang chậm hoặc lỗi — '
                    'thử mở portal HĐ đầu vào của Mắt Bão để xác nhận.'
                ),
                'timeout': True,
                'invoices': [],
            }
        except Exception as exc:
            logger.error('[%s] Lỗi %s: %s', self.name, path, exc)
            return {'success': False, 'error': str(exc), 'invoices': []}

    def _load_invoices(self, path: str, from_date: str, to_date: str) -> dict:
        """Tải trọn 1 tháng (fromDate → toDate) trong một request."""
        logger.info(
            '[%s] Load %s TRỌN THÁNG: %s → %s (timeout=%ss)',
            self.name, path, from_date, to_date, FULL_MONTH_TIMEOUT_SEC,
        )
        whole = self._load_invoices_once(
            path, from_date, to_date, timeout=FULL_MONTH_TIMEOUT_SEC,
        )

        if not whole.get('success') and whole.get('timeout'):
            logger.info(
                '[%s] Trọn tháng timeout — thử lại 1 lần (timeout=%ss)',
                self.name, FULL_MONTH_RETRY_TIMEOUT_SEC,
            )
            whole = self._load_invoices_once(
                path, from_date, to_date, timeout=FULL_MONTH_RETRY_TIMEOUT_SEC,
            )

        phase = {
            'phase': 1,
            'from': from_date,
            'to': to_date,
            'ok': bool(whole.get('success')),
            'mode': 'full_month',
        }

        if whole.get('success'):
            count = int(whole.get('count') or len(whole.get('invoices') or []))
            phase['count'] = count
            logger.info(
                '[%s] Trọn tháng OK — %s HĐ (%s → %s)',
                self.name, count, from_date[:10], to_date[:10],
            )
            return {
                'success': True,
                'invoices': whole.get('invoices') or [],
                'count': count,
                'warnings': [],
                'need_login': False,
                'phases': [phase],
                'phases_ok': 1,
                'phases_total': 1,
                'partial': False,
                'mode': 'full_month',
            }

        phase['error'] = whole.get('error')
        logger.warning('[%s] Trọn tháng FAIL: %s', self.name, whole.get('error'))
        return {
            'success': False,
            'error': whole.get('error') or 'load-data thất bại',
            'need_login': bool(whole.get('need_login')),
            'timeout': bool(whole.get('timeout')),
            'invoices': [],
            'phases': [phase],
            'phases_ok': 0,
            'phases_total': 1,
            'mode': 'full_month',
        }

    def fetch_invoices(self, month_str: str, source: str = SOURCE_PORTAL) -> dict:
        source = (source or SOURCE_PORTAL).strip().lower()
        if source not in VALID_SOURCES:
            return {'success': False, 'error': f'Nguồn không hợp lệ: {source}', 'invoices': []}

        from_date, to_date = _month_range(month_str)
        logger.info(
            '[%s] Fetch %s source=%s TRỌN THÁNG %s → %s',
            self.name, month_str, source, from_date, to_date,
        )

        merged: list[dict] = []
        errors: list[str] = []
        need_login = False
        sources_ok: list[str] = []
        all_phases: list[dict] = []
        phases_ok = 0
        phases_total = 0
        modes: list[str] = []

        if source in (SOURCE_PORTAL, SOURCE_BOTH):
            res = self._load_invoices('/hoa-don-dau-vao/load-data', from_date, to_date)
            all_phases.extend([{**p, 'source': SOURCE_PORTAL} for p in (res.get('phases') or [])])
            phases_ok += int(res.get('phases_ok') or 0)
            phases_total += int(res.get('phases_total') or 0)
            modes.append(res.get('mode') or 'full_month')
            if res.get('success'):
                merged.extend(res.get('invoices') or [])
                sources_ok.append(SOURCE_PORTAL)
                if res.get('warnings'):
                    errors.extend(res['warnings'])
            else:
                errors.append(f'Portal: {res.get("error")}')

        if source in (SOURCE_TCT, SOURCE_BOTH):
            res = self._load_invoices('/hoa-don-dau-vao/load-data-tct', from_date, to_date)
            all_phases.extend([{**p, 'source': SOURCE_TCT} for p in (res.get('phases') or [])])
            phases_ok += int(res.get('phases_ok') or 0)
            phases_total += int(res.get('phases_total') or 0)
            modes.append(res.get('mode') or 'full_month')
            if res.get('success'):
                merged.extend(res.get('invoices') or [])
                sources_ok.append(SOURCE_TCT)
                if res.get('warnings'):
                    errors.extend(res['warnings'])
            else:
                errors.append(f'TCT: {res.get("error")}')
                need_login = need_login or bool(res.get('need_login'))

        # Dedup theo MST + ký hiệu + số HĐ
        seen = set()
        unique = []
        for inv in merged:
            if not isinstance(inv, dict):
                continue
            key = (
                str(inv.get('NBanMST') or '').strip(),
                str(inv.get('KHHDon') or '').strip(),
                str(inv.get('SHDon') or '').strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(inv)

        if not unique and errors:
            return {
                'success': False,
                'error': '; '.join(errors),
                'need_login': need_login,
                'invoices': [],
                'sources_ok': sources_ok,
                'phases': all_phases,
                'phases_ok': phases_ok,
                'phases_total': phases_total,
                'mode': modes[0] if len(modes) == 1 else ('mixed' if modes else None),
            }

        return {
            'success': True,
            'invoices': unique,
            'count': len(unique),
            'sources_ok': sources_ok,
            'warnings': errors,
            'need_login': need_login,
            'phases': all_phases,
            'phases_ok': phases_ok,
            'phases_total': phases_total,
            'partial': bool(errors) and bool(unique),
            'mode': modes[0] if len(modes) == 1 else ('mixed' if modes else None),
        }

    def sync_invoices_by_month(self, month_str: str, source: str = SOURCE_TCT) -> dict:
        """Tương thích API cũ — mặc định TCT như trước."""
        return self.fetch_invoices(month_str, source=source)


def prepare_invoice_data(inv: dict) -> tuple | None:
    """Map payload Mắt Bảo → tuple insert supplier_invoice."""
    try:
        n_lap = inv.get('NLap') or ''
        invoice_date = n_lap[:10] if n_lap else datetime.now().strftime('%Y-%m-%d')
        entry_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        serial = str(inv.get('KHHDon') or '').strip()
        invoice_no = str(inv.get('SHDon') or '').strip()
        seller_name = str(inv.get('NBanTen') or '').strip()
        seller_tax = str(inv.get('NBanMST') or '').strip()

        def safe_float(val):
            if val is None or val == '':
                return 0.0
            try:
                return float(Decimal(str(val).replace(',', '')))
            except Exception:
                return 0.0

        amount = safe_float(inv.get('TgTCThue', 0))
        discount_pct = safe_float(inv.get('TLCKhau', 0))
        discount_amount = safe_float(inv.get('STCKhau', 0))
        tax_amount = safe_float(inv.get('TgTThue', 0))
        total = safe_float(inv.get('TgTTTBSo', 0))
        pdf_url = inv.get('LinkDownloadPDF') or ''
        # address không có cột riêng trên một số schema — giữ trong xml_data

        tax_percent = 0.0
        if amount > 0:
            tax_percent = round((tax_amount / amount) * 100, 0)

        # Chuẩn hóa dòng hàng (DSHHDVu) để nhập kho / hạch toán không phải gõ tay
        inv_store = dict(inv) if isinstance(inv, dict) else {}
        try:
            from Services.inward_invoice_helpers import normalize_supplier_invoice_payload
            enriched = normalize_supplier_invoice_payload(inv_store)
            lines = enriched.get('DSHHDVu') or []
            if lines:
                inv_store['DSHHDVu'] = lines
            for k in ('NBanDChi', 'NBanTen', 'NBanMST', 'SHDon', 'KHHDon', 'NLap'):
                if enriched.get(k) and not inv_store.get(k):
                    inv_store[k] = enriched[k]
            seller_name = str(inv_store.get('NBanTen') or seller_name).strip()
            seller_tax = str(inv_store.get('NBanMST') or seller_tax).strip()
            serial = str(inv_store.get('KHHDon') or serial).strip()
            invoice_no = str(inv_store.get('SHDon') or invoice_no).strip()
            if inv_store.get('NLap'):
                invoice_date = str(inv_store['NLap'])[:10]
        except Exception as enrich_exc:
            logger.debug('enrich DSHHDVu skip: %s', enrich_exc)

        xml_data = json.dumps(inv_store, ensure_ascii=False)

        return (
            invoice_date,
            serial,
            invoice_no,
            seller_name,
            seller_tax,
            amount,
            discount_pct,
            discount_amount,
            tax_percent,
            tax_amount,
            total,
            'new',
            xml_data,
            entry_date,
            pdf_url,
        )
    except Exception as exc:
        logger.error('Lỗi format dữ liệu hóa đơn: %s', exc)
        return None


def persist_invoices(conn: sqlite3.Connection, invoices: list[dict]) -> dict[str, int]:
    """Insert/cập nhật HĐ vào supplier_invoice; trùng MST+serial+số → cập nhật số tiền/PDF."""
    from db_utils import with_sqlite_write

    stats: dict[str, int] = {}

    def _write(target: sqlite3.Connection) -> None:
        nonlocal stats
        stats = _persist_invoices_rows(target, invoices)

    with_sqlite_write(conn, _write, commit=True, label='persist_invoices')
    return stats


def _persist_invoices_rows(conn: sqlite3.Connection, invoices: list[dict]) -> dict[str, int]:
    cursor = conn.cursor()
    new_count = 0
    skip_count = 0
    updated_count = 0
    bad_count = 0

    for inv in invoices or []:
        row_data = prepare_invoice_data(inv)
        if not row_data:
            bad_count += 1
            continue
        if not row_data[2]:  # invoice_no
            bad_count += 1
            continue

        cursor.execute(
            """
            SELECT id, total, tax_amount, amount, pdf_url FROM supplier_invoice
            WHERE seller_tax_code = ? AND serial = ? AND invoice_no = ?
            """,
            (row_data[4], row_data[1], row_data[2]),
        )
        existing = cursor.fetchone()
        if existing:
            ex_id = existing[0] if not isinstance(existing, sqlite3.Row) else existing['id']
            cursor.execute(
                """
                UPDATE supplier_invoice SET
                    invoice_date = ?,
                    seller_name = ?,
                    amount = ?,
                    discount_percent = ?,
                    discount_amount = ?,
                    tax_percent = ?,
                    tax_amount = ?,
                    total = ?,
                    xml_data = ?,
                    pdf_url = COALESCE(NULLIF(?, ''), pdf_url)
                WHERE id = ?
                  AND COALESCE(status, 'new') IN ('new', '', 'pending')
                """,
                (
                    row_data[0], row_data[3], row_data[5], row_data[6], row_data[7],
                    row_data[8], row_data[9], row_data[10], row_data[12],
                    row_data[14] or '',
                    ex_id,
                ),
            )
            if cursor.rowcount:
                updated_count += 1
            else:
                skip_count += 1
            continue

        cursor.execute(
            """
            INSERT INTO supplier_invoice (
                invoice_date, serial, invoice_no, seller_name, seller_tax_code,
                amount, discount_percent, discount_amount, tax_percent, tax_amount,
                total, status, xml_data, date, pdf_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row_data,
        )
        new_count += 1

    return {
        'total_received': len(invoices or []),
        'new_inserted': new_count,
        'updated': updated_count,
        'duplicates_skipped': skip_count,
        'invalid_skipped': bad_count,
    }


def sync_month_to_db(
    conn: sqlite3.Connection | None,
    config: dict,
    month_str: str,
    *,
    source: str = SOURCE_PORTAL,
    login_first: bool = False,
    captcha: dict | None = None,
) -> dict[str, Any]:
    """Fetch HTTP trước (không giữ SQLite), rồi mới persist.

    ``conn`` có thể None — tự mở/đóng connection chỉ cho bước ghi.
    """
    from db_utils import _is_locked_error

    provider = MatbaoPurchaseProvider(config)

    if login_first:
        captcha = captcha or {}
        login_res = provider.login_tct(
            cvalue=captcha.get('cvalue'),
            ckey=captcha.get('ckey') or captcha.get('key'),
            password=captcha.get('password'),
            username=captcha.get('username'),
            persist_captcha=True,
        )
        if not login_res.get('success'):
            return {
                'success': False,
                'error': login_res.get('error') or 'Đăng nhập CQT thất bại',
                'need_captcha': login_res.get('need_captcha', True),
            }

    # Network only — không giữ conn request trong lúc chờ Matbao (timeout 90s)
    result = provider.fetch_invoices(month_str, source=source)
    if not result.get('success'):
        out = dict(result)
        if result.get('need_login') and source in (SOURCE_TCT, SOURCE_BOTH):
            out['need_captcha'] = True
        return out

    own_conn = False
    write_conn = conn
    if write_conn is None:
        from db_utils import open_sqlite, resolve_db_path
        # Connection riêng, đóng thật sau ghi — không dùng g._sme_db (close = no-op)
        write_conn = open_sqlite(resolve_db_path(), timeout=15.0)
        own_conn = True
    try:
        summary = persist_invoices(write_conn, result.get('invoices') or [])
    except sqlite3.OperationalError as exc:
        if _is_locked_error(exc):
            return {
                'success': False,
                'error': 'Database đang bận — thử đồng bộ lại sau vài giây',
            }
        raise
    finally:
        if own_conn and write_conn is not None:
            try:
                write_conn.close()
            except Exception:
                pass

    return {
        'success': True,
        'message': (
            f'Đồng bộ thành công tháng {month_str} (trọn tháng)'
            + (' — một số nguồn lỗi, đã lưu phần tải được' if result.get('partial') else '')
        ),
        'summary': summary,
        'count': summary['new_inserted'],
        'sources_ok': result.get('sources_ok') or [],
        'warnings': result.get('warnings') or [],
        'phases': result.get('phases') or [],
        'phases_ok': result.get('phases_ok'),
        'phases_total': result.get('phases_total'),
        'partial': bool(result.get('partial')),
    }
