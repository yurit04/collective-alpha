"""Which days to measure, in what order.

Sets (decision 0016):
- recent: every session in the last `recent_months` months up to `end`;
- sample: one session a month back to the first tick file (2003-09-10), the first Tuesday,
  Wednesday or Thursday on or after the 10th that is a full session (spreads across eras for
  the backtests, which run from 2004);
- events: stress days and heavy closes (index reconstitutions, quarterly expiries) of the last
  three years.

Order: the last three months first (newest first), then the monthly sample (newest first), then
the rest of the recent window, then the events, so that a run cut short by the end of the
subscription has the most useful days.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

FIRST_TICK_DAY = date(2003, 9, 10)
STRESS_DAYS = (date(2007, 2, 27), date(2008, 9, 29), date(2008, 10, 10), date(2010, 5, 6), date(2011, 8, 8),
               date(2015, 8, 24), date(2018, 2, 5), date(2020, 3, 12), date(2020, 3, 16), date(2024, 8, 5),
               date(2025, 4, 4), date(2025, 4, 7))
EVENT_YEARS = 3


def _months_back(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, 28))


def _fridays(year: int, month: int) -> list[date]:
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    out = []
    while d.month == month:
        out.append(d)
        d += timedelta(days=7)
    return out


def event_days(end: date) -> list[date]:
    """Stress days, plus the third Friday of each quarter-end month (expiries and S&P rebalances)
    and the fourth Friday of June (Russell reconstitution) of the last EVENT_YEARS years."""
    days = [d for d in STRESS_DAYS if d <= end]
    for year in range(end.year - EVENT_YEARS + 1, end.year + 1):
        for month in (3, 6, 9, 12):
            days.append(_fridays(year, month)[2])
        days.append(_fridays(year, 6)[3])
    return sorted({d for d in days if d <= end})


def schedule(sessions: pl.DataFrame, end: date, recent_months: int = 6,
             first: date = FIRST_TICK_DAY) -> pl.DataFrame:
    """day, set ("recent" | "sample" | "event"), priority (lower first) and order within it.

    `sessions`: date, market_open, market_close (UTC), as massive-subspace-stream's
    `Store.sessions` returns them; early closes are never sample days."""
    s = (sessions.filter((pl.col("date") >= first) & (pl.col("date") <= end))
         .with_columns(pl.col("market_close").dt.convert_time_zone("America/New_York").dt.hour().alias("__close_h"))
         .sort("date"))
    days = s["date"].to_list()
    if not days:
        return pl.DataFrame(schema={"day": pl.Date, "set": pl.String, "priority": pl.Int8})
    last = days[-1]
    recent_start = _months_back(last, recent_months)
    near_start = _months_back(last, min(3, recent_months))
    recent = [d for d in days if d > recent_start]

    full = s.filter((pl.col("__close_h") == 16) & pl.col("date").dt.weekday().is_in([2, 3, 4])
                    & (pl.col("date").dt.day() >= 10) & (pl.col("date") <= recent_start))
    sample = (full.group_by(pl.col("date").dt.strftime("%Y-%m").alias("__m")).agg(pl.col("date").min())
              ["date"].to_list())
    open_days = set(days)
    events = [d for d in event_days(last) if d in open_days]

    rows = ([(d, "recent", 1) for d in recent if d > near_start]
            + [(d, "sample", 2) for d in sample]
            + [(d, "recent", 3) for d in recent if d <= near_start]
            + [(d, "event", 4) for d in events])
    df = pl.DataFrame(rows, schema={"day": pl.Date, "set": pl.String, "priority": pl.Int8}, orient="row")
    return (df.sort("priority", "day", descending=[False, True])
            .unique("day", keep="first", maintain_order=True))
