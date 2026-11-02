"""Business days (NYSE sessions) and FINRA's publication schedule."""

from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

CALENDAR_START = "2009-01-02"
# Short interest is published on the 7th business day after the settlement date (FINRA Rule
# 4560 schedule; e.g. 2025-12-31 -> 2026-01-12, 2026-07-15 -> 2026-07-24).
SI_PUBLICATION_LAG = 7


@lru_cache(maxsize=1)
def _sessions() -> tuple[date, ...]:
    import warnings

    import exchange_calendars as xcals

    with warnings.catch_warnings():  # the library trips pandas deprecation warnings internally
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.filterwarnings("ignore", message=".*Series.values.*")
        cal = xcals.get_calendar("XNYS", start=CALENDAR_START)
    return tuple(d.date() for d in cal.sessions)


def business_days(start: date, end: date) -> list[date]:
    """NYSE sessions in [start, end]; past the calendar's end, weekdays."""
    s = _sessions()
    out = list(s[bisect.bisect_left(s, start):bisect.bisect_right(s, end)])
    d = max(start, s[-1] + timedelta(days=1))
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def add_business_days(day: date, n: int) -> date:
    """The n-th business day after `day`."""
    s = _sessions()
    i = bisect.bisect_right(s, day) + n - 1
    if i < len(s):
        return s[i]
    out, left = max(day, s[-1]), i - len(s) + 1
    while left > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            left -= 1
    return out


def si_published(settlement: date) -> date:
    """When FINRA publishes the short interest for a settlement date."""
    return add_business_days(settlement, SI_PUBLICATION_LAG)


def today_et() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()
