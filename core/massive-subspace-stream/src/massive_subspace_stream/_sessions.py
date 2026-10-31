"""NYSE trading calendar: trading days and regular-session open/close times (incl. early closes)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import polars as pl

CALENDAR_START = "2003-01-02"


@lru_cache(maxsize=1)
def _calendar():
    import warnings

    import exchange_calendars as xcals

    with warnings.catch_warnings():  # the library trips pandas deprecation warnings internally
        warnings.simplefilter("ignore", DeprecationWarning)
        warnings.filterwarnings("ignore", message=".*Series.values.*")
        return xcals.get_calendar("XNYS", start=CALENDAR_START)


def session_table(start: date, end: date) -> pl.DataFrame:
    """date, market_open, market_close (UTC) for each NYSE session in [start, end]."""
    cal = _calendar()
    last = min(end, cal.last_session.date())
    if start > last:
        return pl.DataFrame(schema={"date": pl.Date,
                                    "market_open": pl.Datetime("ns", "UTC"),
                                    "market_close": pl.Datetime("ns", "UTC")})
    sched = cal.schedule.loc[str(start):str(last), ["open", "close"]].reset_index(names="session")
    utc_ns = pl.Datetime("ns", "UTC")
    return pl.from_pandas(sched).select(
        pl.col("session").dt.date().alias("date"),
        pl.col("open").dt.convert_time_zone("UTC").cast(utc_ns).alias("market_open"),
        pl.col("close").dt.convert_time_zone("UTC").cast(utc_ns).alias("market_close"),
    )


def trading_days(start: date, end: date) -> list[date]:
    return session_table(start, end)["date"].to_list()


def filter_session(bars: pl.LazyFrame, session: str, start: date, end: date,
                   ts_col: str = "timestamp") -> pl.LazyFrame:
    """Keep 'regular' (open <= bar start < close), 'extended' (everything else), or 'all'."""
    if session in (None, "all"):
        return bars
    if session not in ("regular", "extended"):
        raise ValueError("session must be 'all', 'regular', or 'extended'")
    sessions = session_table(start, end).lazy()
    in_regular = (pl.col(ts_col) >= pl.col("market_open")) & (pl.col(ts_col) < pl.col("market_close"))
    joined = bars.join(sessions, on="date", how="left")
    cond = in_regular.fill_null(False)
    return joined.filter(cond if session == "regular" else ~cond).drop("market_open", "market_close")
