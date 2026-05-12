from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class MarketHoursState:
    is_open: bool
    reason: str
    local_now: datetime
    next_open: datetime | None = None
    next_close: datetime | None = None


def _parse_hhmm(value: str, *, field_name: str) -> time:
    text = str(value or "").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must use HH:MM 24-hour format") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{field_name} must use HH:MM 24-hour format")
    return time(hour=hour, minute=minute)


def _format_local(dt: datetime) -> str:
    # strftime %-I is not portable, so strip a leading zero manually.
    return dt.strftime("%a %I:%M %p %Z").replace(" 0", " ")


def _next_weekday_open_after(local_now: datetime, open_at: time) -> datetime:
    days_ahead = 0
    while True:
        candidate_date = local_now.date() + timedelta(days=days_ahead)
        candidate = datetime.combine(candidate_date, open_at, tzinfo=local_now.tzinfo)
        if candidate.weekday() < 5 and candidate > local_now:
            return candidate
        days_ahead += 1


def equity_market_hours_state(
    *,
    now: datetime | None = None,
    timezone_name: str = "America/New_York",
    open_time: str = "04:00",
    close_time: str = "20:00",
) -> MarketHoursState:
    """Return a lightweight weekday US-equity session state.

    This intentionally avoids provider/API calls. It models the extended US-equity
    data/trading session, 04:00-20:00 America/New_York on weekdays. During DST,
    that close is 6:00 PM Mountain. It is a runtime throttle, not a full
    holiday/early-close exchange calendar.
    """

    zone = ZoneInfo(str(timezone_name or "America/New_York"))
    local_now = datetime.now(zone) if now is None else (now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone))
    open_at = _parse_hhmm(open_time, field_name="market_open")
    close_at = _parse_hhmm(close_time, field_name="market_close")
    if open_at >= close_at:
        raise ValueError("market_open must be earlier than market_close for same-day equity sessions")

    today_open = datetime.combine(local_now.date(), open_at, tzinfo=zone)
    today_close = datetime.combine(local_now.date(), close_at, tzinfo=zone)

    if local_now.weekday() < 5 and today_open <= local_now < today_close:
        return MarketHoursState(
            is_open=True,
            reason=f"US equity session open until {_format_local(today_close)}",
            local_now=local_now,
            next_close=today_close,
        )

    next_open = _next_weekday_open_after(local_now, open_at)
    if local_now.weekday() >= 5:
        reason = f"US equity session closed for weekend; next open {_format_local(next_open)}"
    elif local_now < today_open:
        reason = f"US equity session not open yet; next open {_format_local(next_open)}"
    else:
        reason = f"US equity session closed at {_format_local(today_close)}; next open {_format_local(next_open)}"
    return MarketHoursState(is_open=False, reason=reason, local_now=local_now, next_open=next_open)


def seconds_until_next_open(state: MarketHoursState) -> int | None:
    if state.next_open is None:
        return None
    return max(0, int((state.next_open - state.local_now).total_seconds()))
