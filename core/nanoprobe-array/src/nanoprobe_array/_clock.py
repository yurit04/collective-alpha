"""The regular-session bar clock: bars anchored at the open, labelled by their end."""

from __future__ import annotations

import re

import polars as pl

UTC_NS = pl.Datetime("ns", "UTC")
MAX_MINUTES = 390


def parse_freq(freq: str | int) -> int:
    """'5min', '5m', '1h', 15 -> minutes. One minute up to a whole session (390)."""
    if isinstance(freq, bool):
        raise ValueError(f"bad bar frequency {freq!r}")
    if isinstance(freq, int):
        n = freq
    else:
        m = re.fullmatch(r"\s*(\d+)\s*(min|m|h)?\s*", str(freq).lower())
        if not m:
            raise ValueError(f"bad bar frequency {freq!r}: use e.g. '1min', '5min', '30min', '1h'")
        n = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    if not 1 <= n <= MAX_MINUTES:
        raise ValueError(f"bar frequency must be 1-{MAX_MINUTES} minutes, got {n}")
    return n


def clock(sessions: pl.DataFrame, freq: str | int = "5min") -> pl.DataFrame:
    """One row per bar of each session.

    sessions: date, market_open, market_close (UTC), as `Store.sessions` returns them.
    Returns date, bar (0-based), bar_start, bar_end, minutes_from_open (to bar_end),
    minutes_to_close (from bar_end), last. A bar covers [bar_start, bar_end); the last bar ends
    at the close, so it is shorter when the frequency does not divide the session (early closes
    included).
    """
    n = parse_freq(freq)
    step = pl.duration(minutes=pl.col("bar") * n)
    return (
        sessions.lazy()
        .with_columns(minutes=(pl.col("market_close") - pl.col("market_open")).dt.total_minutes())
        .with_columns(bar=pl.int_ranges(0, (pl.col("minutes") + n - 1) // n))
        .explode("bar")
        .with_columns(bar=pl.col("bar").cast(pl.Int32))
        .with_columns(bar_start=(pl.col("market_open") + step).cast(UTC_NS),
                      bar_end=pl.min_horizontal(pl.col("market_open") + step + pl.duration(minutes=n),
                                                pl.col("market_close")).cast(UTC_NS))
        .with_columns(minutes_from_open=(pl.col("bar_end") - pl.col("market_open")).dt.total_minutes().cast(pl.Int32),
                      minutes_to_close=(pl.col("market_close") - pl.col("bar_end")).dt.total_minutes().cast(pl.Int32))
        .with_columns(last=pl.col("minutes_to_close") == 0)
        .select("date", "bar", "bar_start", "bar_end", "minutes_from_open", "minutes_to_close", "last")
        .sort("date", "bar")
        .collect()
    )
