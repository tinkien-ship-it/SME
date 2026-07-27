"""Tính kỳ thu phòng và các tháng còn nợ theo ngày bắt đầu thuê."""
import calendar
import json
from datetime import date, datetime, timedelta


def parse_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")[:19]).date()
    except ValueError:
        return None


def _last_day_of_month(d):
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _add_months_anchor(year, month, day):
    month_idx = month - 1 + 1
    year += month_idx // 12
    month = month_idx % 12 + 1
    day = min(day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def iter_billing_periods(start_date, until_date=None):
    """
    Sinh các kỳ thu từ ngày bắt đầu đến until_date (mặc định hôm nay).
    - Nhận phòng ngày 1: kỳ = từ mùng 1 đến cuối tháng đó.
    - Nhận phòng ngày D (>1): kỳ = từ ngày D tháng M đến ngày (D-1) tháng M+1.
    """
    start = parse_date(start_date)
    if not start:
        return []

    if until_date is None:
        until_date = date.today()
    else:
        until_date = parse_date(until_date) or date.today()

    anchor_day = start.day
    periods = []
    cur_start = start

    while cur_start <= until_date:
        if anchor_day == 1:
            cur_end = _last_day_of_month(cur_start)
        else:
            cur_end = _add_months_anchor(cur_start.year, cur_start.month, anchor_day) - timedelta(days=1)

        periods.append(
            {
                "month": cur_start.month,
                "year": cur_start.year,
                "start": cur_start,
                "end": cur_end,
            }
        )
        cur_start = cur_end + timedelta(days=1)

    return periods


def format_unpaid_months(unpaid_periods):
    if not unpaid_periods:
        return ""
    years = {p["year"] for p in unpaid_periods}
    if len(years) == 1:
        return ", ".join(str(p["month"]) for p in unpaid_periods)
    return ", ".join(f"{p['month']}/{p['year']}" for p in unpaid_periods)


def period_has_payment(period, payment_dates):
    for pay_date in payment_dates:
        if period["start"] <= pay_date <= period["end"]:
            return True
    return False


def compute_rental_debt(start_date, payment_dates, until_date=None, period_type="month"):
    if period_type == "day":
        return {
            "unpaid_months": [],
            "unpaid_months_display": "—",
            "current_period_paid": True,
            "current_billing_month": None,
        }

    periods = iter_billing_periods(start_date, until_date)
    if not periods:
        return {
            "unpaid_months": [],
            "unpaid_months_display": "",
            "current_period_paid": True,
            "current_billing_month": None,
        }

    unpaid = [p for p in periods if not period_has_payment(p, payment_dates)]
    current = periods[-1]
    current_paid = period_has_payment(current, payment_dates)

    return {
        "unpaid_months": [p["month"] for p in unpaid],
        "unpaid_months_display": format_unpaid_months(unpaid),
        "current_period_paid": current_paid,
        "current_billing_month": current["month"],
    }


def fetch_renter_payment_dates(conn, renter_id, room_no):
    """Lấy ngày phiếu thu / thanh toán thuê phòng của khách."""
    rows = conn.execute(
        """
        SELECT s.date AS sale_date, s.note, s.status,
               pt.date AS pt_date, pt.reason
        FROM sale s
        LEFT JOIN phieu_thu pt ON pt.sale_id = s.id
        WHERE s.business_line = 'rental_service'
          AND COALESCE(s.status, 'completed') = 'completed'
        ORDER BY s.date
        """
    ).fetchall()

    room_token = f"phòng số {room_no}" if room_no else None
    dates = []

    for row in rows:
        matched = False
        note = row["note"] or ""

        try:
            meta = json.loads(note)
            rid = meta.get("renter_id")
            if rid is not None and int(rid) == int(renter_id):
                matched = True
        except (json.JSONDecodeError, TypeError, ValueError):
            rid_text = str(renter_id)
            if f'"renter_id": {rid_text}' in note or f'"renter_id":{rid_text}' in note:
                matched = True

        if not matched and room_token and row["reason"] and room_token in row["reason"]:
            matched = True

        if not matched:
            continue

        pay_date = parse_date(row["pt_date"] or row["sale_date"])
        if pay_date:
            dates.append(pay_date)

    return dates
