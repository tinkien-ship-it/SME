"""Khóa sổ / chốt kỳ SME.

Hai tầng riêng biệt:

1) **Chốt kỳ kê khai** (tháng hoặc quý theo cấu hình GTGT)
   - Giữa tháng (kê khai tháng) / giữa quý (kê khai quý): không được chốt.
   - Đánh dấu ``sme_filing_closes`` khi đã quyết toán GTGT cuối kỳ kê khai.
   - Không khóa cứng sổ năm; dùng để buộc đảo bút toán khi sửa sau kê khai.

2) **Khóa sổ kế toán năm**
   - Chỉ được khóa vào/ ngày cuối năm (sau 31/12 của năm tài chính).
   - Khóa cả 12 tháng của năm (``sme_period_locks``).

3) **Mở sổ trở lại** (điều chỉnh / kê khai bổ sung / sửa BCTC)
   - «Mở lại kỳ kê khai»: gỡ chốt cửa sổ GTGT → cho phép sửa/xóa bút toán tại chỗ
     trong các tháng thuộc cửa sổ đó (theo hướng dẫn CQT: hạch toán đúng rồi kê khai bổ sung).
   - «Mở lại sổ năm»: gỡ khóa 12 tháng **và** gỡ mọi chốt kê khai trong năm →
     mở toàn bộ để sửa sổ / BCTC, rồi khóa / chốt lại khi xong.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime
from typing import Any

from Services.sme.filing_period import resolve_filing_window
from Services.tenant_profile import normalize_vat_filing_period


def ensure_period_lock_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_period_locks (
            fiscal_year INTEGER NOT NULL,
            period INTEGER NOT NULL,
            locked_at TEXT NOT NULL,
            locked_by TEXT,
            reason TEXT,
            PRIMARY KEY (fiscal_year, period)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_filing_closes (
            fiscal_year INTEGER NOT NULL,
            period_to INTEGER NOT NULL,
            filing_mode TEXT NOT NULL,
            period_from INTEGER NOT NULL,
            closed_at TEXT NOT NULL,
            closed_by TEXT,
            reason TEXT,
            PRIMARY KEY (fiscal_year, period_to, filing_mode)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_book_reopens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year INTEGER NOT NULL,
            reopen_type TEXT NOT NULL,
            period_from INTEGER,
            period_to INTEGER,
            reason TEXT NOT NULL,
            opened_by TEXT,
            opened_at TEXT NOT NULL
        )
        """
    )
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# Ngày / cửa sổ kê khai
# ---------------------------------------------------------------------------

def _resolve_filing_mode(
    filing_mode: str | None = None,
    features: dict | None = None,
    accounting_regime: str | None = None,
) -> str:
    from Services.tenant_profile import default_vat_filing_period_for_regime

    regime_default = default_vat_filing_period_for_regime(accounting_regime or 'SME_TT99')
    if filing_mode:
        return normalize_vat_filing_period(filing_mode, default=regime_default)
    feat = features or {}
    raw = feat.get('vat_filing_period') or feat.get('filing_period')
    if raw:
        return normalize_vat_filing_period(raw, default=regime_default)
    if feat.get('monthly_vat_filing') is True:
        return 'monthly'
    if feat.get('monthly_vat_filing') is False:
        return 'quarterly'
    return regime_default


def period_end_date(fiscal_year: int, period: int) -> date:
    last = calendar.monthrange(fiscal_year, period)[1]
    return date(fiscal_year, period, last)


def year_end_date(fiscal_year: int) -> date:
    return date(fiscal_year, 12, 31)


def filing_window_for_period(
    period: int,
    *,
    filing_mode: str | None = None,
    features: dict | None = None,
) -> dict[str, Any]:
    mode = _resolve_filing_mode(filing_mode, features)
    return resolve_filing_window(filing_mode=mode, period=period)


def is_calendar_past_period_end(
    fiscal_year: int,
    period: int,
    *,
    today: date | None = None,
) -> bool:
    today = today or date.today()
    return today >= period_end_date(fiscal_year, period)


def assert_filing_close_allowed(
    fiscal_year: int,
    period: int,
    *,
    filing_mode: str | None = None,
    features: dict | None = None,
    today: date | None = None,
    action: str = 'chốt kỳ kê khai',
) -> dict[str, Any]:
    """
    Chặn chốt giữa tháng (kê khai tháng) hoặc giữa quý (kê khai quý).
    Chỉ cho phép từ ngày cuối cửa sổ kê khai trở đi.
    """
    today = today or date.today()
    window = filing_window_for_period(period, filing_mode=filing_mode, features=features)
    mode = window['filing_mode']
    p_to = int(window['period_to'])

    # Phải đang ở tháng cuối cửa sổ (T3/T6/T9/T12 nếu quý; đúng tháng nếu tháng)
    if int(period) != p_to:
        raise ValueError(
            f'Kê khai GTGT theo {"tháng" if mode == "monthly" else "quý"} — '
            f'chỉ được {action} vào cuối {window["label"]} (tháng {p_to}), '
            f'không chốt giữa kỳ (đang chọn tháng {period}).'
        )

    end = period_end_date(fiscal_year, p_to)
    if today < end:
        raise ValueError(
            f'Chưa đến ngày cuối kỳ kê khai {window["label"]} '
            f'({end.strftime("%d/%m/%Y")}) — không được {action} giữa kỳ '
            f'để tránh hạch toán sai.'
        )
    return {**window, 'close_date': end.isoformat()}


def assert_year_lock_allowed(
    fiscal_year: int,
    *,
    today: date | None = None,
    action: str = 'khóa sổ năm',
) -> None:
    """Khóa sổ kế toán chỉ sau ngày cuối năm (31/12)."""
    today = today or date.today()
    end = year_end_date(fiscal_year)
    if today < end:
        raise ValueError(
            f'Khóa sổ kế toán chỉ được phép từ ngày cuối năm '
            f'({end.strftime("%d/%m/%Y")}) — hiện chưa đến hạn {action} năm {fiscal_year}.'
        )


# ---------------------------------------------------------------------------
# Khóa sổ năm (sme_period_locks)
# ---------------------------------------------------------------------------

def is_period_locked(conn: sqlite3.Connection, fiscal_year: int, period: int) -> bool:
    ensure_period_lock_schema(conn, commit=False)
    row = conn.execute(
        "SELECT 1 FROM sme_period_locks WHERE fiscal_year = ? AND period = ?",
        (fiscal_year, period),
    ).fetchone()
    return bool(row)


def is_year_locked(conn: sqlite3.Connection, fiscal_year: int) -> bool:
    """Năm khóa khi cả 12 tháng đều có bản ghi khóa (hoặc ít nhất T12 — tương thích dữ liệu cũ)."""
    ensure_period_lock_schema(conn, commit=False)
    row = conn.execute(
        "SELECT COUNT(*) FROM sme_period_locks WHERE fiscal_year = ?",
        (fiscal_year,),
    ).fetchone()
    n = int(row[0] or 0) if row else 0
    if n >= 12:
        return True
    # Dữ liệu cũ: chỉ khóa từng tháng — coi T12 đã khóa là năm đã chốt
    return is_period_locked(conn, fiscal_year, 12)


def get_period_lock(
    conn: sqlite3.Connection, fiscal_year: int, period: int,
) -> dict[str, Any] | None:
    ensure_period_lock_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sme_period_locks WHERE fiscal_year = ? AND period = ?",
        (fiscal_year, period),
    ).fetchone()
    return dict(row) if row else None


def lock_period(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    locked_by: str | None = None,
    reason: str = 'Chốt kỳ',
    allow_mid_year: bool = False,
) -> dict[str, Any]:
    """
    Ghi khóa một tháng. Mặc định chỉ dùng nội bộ khi khóa cả năm.
    ``allow_mid_year=False`` (mặc định): từ chối nếu chưa đến cuối năm /
    không phải đang khóa năm (period != thao tác năm).
    """
    ensure_period_lock_schema(conn, commit=False)
    if not allow_mid_year:
        assert_year_lock_allowed(fiscal_year, action='khóa sổ')
        if int(period) != 12:
            raise ValueError(
                'Khóa sổ kế toán chỉ áp dụng cho cả năm (cuối năm). '
                'Dùng «Khóa sổ năm» — không khóa từng tháng giữa năm.'
            )
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """
        INSERT INTO sme_period_locks (fiscal_year, period, locked_at, locked_by, reason)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fiscal_year, period) DO UPDATE SET
            locked_at = excluded.locked_at,
            locked_by = excluded.locked_by,
            reason = excluded.reason
        """,
        (fiscal_year, period, now, locked_by, reason),
    )
    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'locked_at': now,
        'locked_by': locked_by,
        'reason': reason,
    }


def lock_year(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    locked_by: str | None = None,
    reason: str = 'Khóa sổ năm',
    today: date | None = None,
) -> dict[str, Any]:
    """Khóa cả 12 tháng — chỉ sau 31/12."""
    assert_year_lock_allowed(fiscal_year, today=today, action='khóa sổ năm')
    ensure_period_lock_schema(conn, commit=False)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    periods = []
    for p in range(1, 13):
        conn.execute(
            """
            INSERT INTO sme_period_locks (fiscal_year, period, locked_at, locked_by, reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fiscal_year, period) DO UPDATE SET
                locked_at = excluded.locked_at,
                locked_by = excluded.locked_by,
                reason = excluded.reason
            """,
            (fiscal_year, p, now, locked_by, reason),
        )
        periods.append(p)
    return {
        'fiscal_year': fiscal_year,
        'locked_periods': periods,
        'locked_at': now,
        'locked_by': locked_by,
        'reason': reason,
        'kind': 'year_lock',
    }


def unlock_period(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
) -> bool:
    ensure_period_lock_schema(conn, commit=False)
    cur = conn.execute(
        "DELETE FROM sme_period_locks WHERE fiscal_year = ? AND period = ?",
        (fiscal_year, period),
    )
    return cur.rowcount > 0


def clear_all_filing_closes_for_year(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
) -> int:
    """Gỡ mọi chốt kỳ kê khai trong năm tài chính."""
    ensure_period_lock_schema(conn, commit=False)
    cur = conn.execute(
        'DELETE FROM sme_filing_closes WHERE fiscal_year = ?',
        (int(fiscal_year),),
    )
    return int(cur.rowcount or 0)


def record_book_reopen(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    reopen_type: str,
    reason: str,
    opened_by: str | None = None,
    period_from: int | None = None,
    period_to: int | None = None,
) -> dict[str, Any]:
    """Ghi nhật ký mở sổ trở lại (truy vết kê khai bổ sung / sửa BCTC)."""
    ensure_period_lock_schema(conn, commit=False)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cur = conn.execute(
        """
        INSERT INTO sme_book_reopens(
            fiscal_year, reopen_type, period_from, period_to, reason, opened_by, opened_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            int(fiscal_year),
            (reopen_type or 'year').strip().lower(),
            period_from,
            period_to,
            (reason or '').strip() or 'Mở sổ điều chỉnh',
            opened_by,
            now,
        ),
    )
    return {
        'id': cur.lastrowid,
        'fiscal_year': int(fiscal_year),
        'reopen_type': reopen_type,
        'period_from': period_from,
        'period_to': period_to,
        'reason': reason,
        'opened_by': opened_by,
        'opened_at': now,
    }


def unlock_year(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    unlocked_by: str | None = None,
    reason: str = 'Mở lại sổ năm để điều chỉnh / kê khai bổ sung / sửa BCTC',
    clear_filing: bool = True,
) -> dict[str, Any]:
    """Mở khóa cả năm + (mặc định) gỡ chốt kê khai — cho phép sửa/xóa bút toán tại chỗ."""
    ensure_period_lock_schema(conn, commit=False)
    cur = conn.execute(
        "DELETE FROM sme_period_locks WHERE fiscal_year = ?",
        (fiscal_year,),
    )
    filing_cleared = 0
    if clear_filing:
        filing_cleared = clear_all_filing_closes_for_year(conn, fiscal_year=fiscal_year)
    reopen = record_book_reopen(
        conn,
        fiscal_year=fiscal_year,
        reopen_type='year',
        reason=reason,
        opened_by=unlocked_by,
        period_from=1,
        period_to=12,
    )
    return {
        'fiscal_year': fiscal_year,
        'unlocked_count': cur.rowcount,
        'filing_closes_cleared': filing_cleared,
        'unlocked_by': unlocked_by,
        'reason': reason,
        'kind': 'year_unlock',
        'books_reopened': True,
        'allow_inplace_edit_delete': True,
        'reopen': reopen,
        'hint': (
            'Sổ năm đã mở: được sửa/xóa bút toán sai tại chỗ (không bắt buộc đảo). '
            'Sau khi hạch toán đúng: kê khai bổ sung GTGT (nếu đã nộp) và cập nhật BCTC, '
            'rồi chốt kỳ / khóa sổ lại khi hoàn tất.'
        ),
    }


def assert_period_open(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
    *,
    action: str = 'ghi sổ',
) -> None:
    if is_period_locked(conn, fiscal_year, period):
        raise ValueError(
            f'Kỳ {period:02d}/{fiscal_year} thuộc năm đã khóa sổ — không thể {action}. '
            f'Dùng «Mở lại sổ năm» tại /SME_auto_posting (ghi lý do) rồi chỉnh sửa; '
            f'sau đó khóa lại cuối năm nếu cần.'
        )


def list_locked_periods(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
) -> list[dict[str, Any]]:
    ensure_period_lock_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    if fiscal_year:
        rows = conn.execute(
            "SELECT * FROM sme_period_locks WHERE fiscal_year = ? ORDER BY period",
            (fiscal_year,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sme_period_locks ORDER BY fiscal_year DESC, period DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Chốt kỳ kê khai (mềm) — sau QTGT
# ---------------------------------------------------------------------------

def mark_filing_closed(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    filing_mode: str | None = None,
    features: dict | None = None,
    closed_by: str | None = None,
    reason: str = 'Chốt kỳ kê khai GTGT',
) -> dict[str, Any]:
    """Đánh dấu đã chốt cửa sổ kê khai (không khóa sổ năm)."""
    ensure_period_lock_schema(conn, commit=False)
    window = filing_window_for_period(period, filing_mode=filing_mode, features=features)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """
        INSERT INTO sme_filing_closes (
            fiscal_year, period_to, filing_mode, period_from, closed_at, closed_by, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fiscal_year, period_to, filing_mode) DO UPDATE SET
            closed_at = excluded.closed_at,
            closed_by = excluded.closed_by,
            reason = excluded.reason,
            period_from = excluded.period_from
        """,
        (
            fiscal_year,
            int(window['period_to']),
            window['filing_mode'],
            int(window['period_from']),
            now,
            closed_by,
            reason,
        ),
    )
    return {
        'fiscal_year': fiscal_year,
        'filing_mode': window['filing_mode'],
        'period_from': int(window['period_from']),
        'period_to': int(window['period_to']),
        'label': window['label'],
        'closed_at': now,
        'closed_by': closed_by,
        'reason': reason,
    }


def clear_filing_close(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    filing_mode: str | None = None,
    features: dict | None = None,
    cleared_by: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Gỡ chốt kỳ kê khai — mở sổ cửa sổ GTGT để sửa/xóa bút toán và kê khai bổ sung."""
    ensure_period_lock_schema(conn, commit=False)
    window = filing_window_for_period(period, filing_mode=filing_mode, features=features)
    cur = conn.execute(
        """
        DELETE FROM sme_filing_closes
        WHERE fiscal_year = ? AND period_to = ? AND filing_mode = ?
        """,
        (fiscal_year, int(window['period_to']), window['filing_mode']),
    )
    cleared = cur.rowcount > 0
    reopen = None
    if cleared or reason:
        reopen = record_book_reopen(
            conn,
            fiscal_year=fiscal_year,
            reopen_type='filing',
            reason=reason or 'Mở lại kỳ kê khai để điều chỉnh / kê khai bổ sung',
            opened_by=cleared_by,
            period_from=int(window['period_from']),
            period_to=int(window['period_to']),
        )
    return {
        'cleared': cleared,
        'fiscal_year': fiscal_year,
        'filing_mode': window['filing_mode'],
        'period_from': int(window['period_from']),
        'period_to': int(window['period_to']),
        'label': window.get('label'),
        'books_reopened': True,
        'allow_inplace_edit_delete': True,
        'reopen': reopen,
        'hint': (
            f"Đã mở lại kỳ kê khai {window.get('label') or ''}: được sửa/xóa bút toán "
            f"sai trong các tháng {int(window['period_from'])}–{int(window['period_to'])}/{fiscal_year}. "
            f'Sau khi hạch toán đúng → kê khai bổ sung theo quy định CQT.'
        ),
    }


def get_filing_close_for_month(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
) -> dict[str, Any] | None:
    """Trả về bản ghi chốt kê khai bao phủ tháng ``period`` (nếu có)."""
    ensure_period_lock_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT * FROM sme_filing_closes
        WHERE fiscal_year = ?
          AND period_from <= ? AND period_to >= ?
        ORDER BY period_to DESC
        LIMIT 1
        """,
        (fiscal_year, period, period),
    ).fetchone()
    return dict(row) if row else None


def is_filing_closed(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
) -> bool:
    return get_filing_close_for_month(conn, fiscal_year, period) is not None


def is_period_sealed(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
) -> bool:
    """Đã khóa năm hoặc đã chốt kỳ kê khai — sửa/xóa tại chỗ bị chặn, dùng đảo.

    Sau «Mở lại kỳ kê khai» / «Mở lại sổ năm» (gỡ chốt +/hoặc gỡ khóa),
    hàm trả về False → được sửa/xóa bút toán tại chỗ để kê khai bổ sung / sửa BCTC.
    """
    return is_period_locked(conn, fiscal_year, period) or is_filing_closed(
        conn, fiscal_year, period,
    )


def period_amendment_status(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
) -> dict[str, Any]:
    """Trạng thái mở/khóa phục vụ UI — có được sửa/xóa tại chỗ hay không."""
    locked = is_period_locked(conn, fiscal_year, period)
    filing = is_filing_closed(conn, fiscal_year, period)
    sealed = locked or filing
    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'year_locked': locked,
        'filing_closed': filing,
        'sealed': sealed,
        'allow_inplace_edit_delete': not sealed,
        'mode': (
            'open_amendment'
            if not sealed
            else ('year_locked' if locked else 'filing_closed')
        ),
        'hint': (
            'Kỳ đang mở: được sửa/xóa bút toán sai tại chỗ (không bắt buộc đảo). '
            'Nếu đã kê khai: sau khi sửa phải kê khai bổ sung; BCTC cập nhật tương ứng.'
            if not sealed
            else (
                'Kỳ đã khóa/chốt: chỉ đảo bút toán để điều chỉnh, '
                'hoặc bật «Mở lại kỳ kê khai» / «Mở lại sổ năm» để sửa/xóa tại chỗ.'
            )
        ),
    }


def list_filing_closes(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
) -> list[dict[str, Any]]:
    ensure_period_lock_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    if fiscal_year:
        rows = conn.execute(
            "SELECT * FROM sme_filing_closes WHERE fiscal_year = ? ORDER BY period_to",
            (fiscal_year,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sme_filing_closes ORDER BY fiscal_year DESC, period_to DESC"
        ).fetchall()
    return [dict(r) for r in rows]
