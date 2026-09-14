"""NYSE trading calendar helpers."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd


@lru_cache(maxsize=1)
def nyse() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS")


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Inclusive list of NYSE sessions between start and end."""
    sessions = nyse().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [s.date() for s in sessions]


def previous_trading_day(ref: dt.date | None = None) -> dt.date:
    ref = ref or dt.date.today()
    cal = nyse()
    ts = pd.Timestamp(ref)
    if cal.is_session(ts):
        # If today is a session, the last *completed* session is the prior one.
        return cal.previous_session(ts).date()
    return cal.date_to_session(ts, direction="previous").date()


def is_trading_day(d: dt.date) -> bool:
    return bool(nyse().is_session(pd.Timestamp(d)))
