"""Session boundaries and time-of-day helpers for minute bars.

Minute bars are labelled by the *start* of the bar in `ts_ny` (naive New York time). The regular
session is [open, close): the last regular bar of a normal day starts at 15:59. Boundaries come
from the exchange calendar, so early closes (about fifteen half-days a year, 13:00 New York) are
handled without hardcoding.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache

import pandas as pd
import polars as pl

from collective_alpha.calendar import nyse

NY = "America/New_York"


def minute_of_day(col: str = "ts_ny") -> pl.Expr:
    """Minutes since midnight. The hour/minute components are 8-bit, so `hour * 60` overflows
    unless both are cast first. Casting here keeps that bug out of every caller."""
    return pl.col(col).dt.hour().cast(pl.Int32) * 60 + pl.col(col).dt.minute().cast(pl.Int32)


@dataclass(frozen=True)
class SessionBounds:
    date: dt.date
    open_min: int  # minutes since New York midnight, inclusive
    close_min: int  # exclusive
    half_day: bool

    @property
    def n_minutes(self) -> int:
        return self.close_min - self.open_min

    @property
    def open_window_end(self) -> int:
        return min(self.open_min + 30, self.close_min)

    @property
    def close_window_start(self) -> int:
        return max(self.close_min - 30, self.open_min)


@lru_cache(maxsize=4096)
def session_bounds(date: dt.date) -> SessionBounds | None:
    """None when the date is not an exchange session."""
    cal = nyse()
    ts = pd.Timestamp(date)
    if not cal.is_session(ts):
        return None
    o = cal.session_open(ts).tz_convert(NY)
    c = cal.session_close(ts).tz_convert(NY)
    om = int(o.hour) * 60 + int(o.minute)
    cm = int(c.hour) * 60 + int(c.minute)
    return SessionBounds(date=date, open_min=om, close_min=cm, half_day=(cm - om) < 390)
