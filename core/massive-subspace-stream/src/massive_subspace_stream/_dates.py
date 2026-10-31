"""Date helpers. All stored timestamps are UTC; trading dates are US/Eastern."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DateLike = str | date | datetime | Any


def to_date(value: DateLike | None) -> date | None:
    """Accept 'YYYY-MM-DD', date, datetime, or pandas Timestamp."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date") and callable(value.date):  # pandas.Timestamp, numpy datetime64 wrappers
        return value.date()
    return date.fromisoformat(str(value)[:10])


def today_et() -> date:
    return datetime.now(ET).date()


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def years(start: date, end: date) -> list[int]:
    return list(range(start.year, end.year + 1))
