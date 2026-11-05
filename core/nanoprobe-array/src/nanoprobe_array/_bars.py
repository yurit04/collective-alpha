"""Minute bars -> regular-session bars of any size, with an explicit rule for empty bars."""

from __future__ import annotations

from collections.abc import Iterable

import polars as pl

from ._clock import clock, parse_freq

BAR_COLUMNS = ["date", "security_id", "ticker", "bar", "bar_start", "bar_end", "minutes_from_open",
               "minutes_to_close", "last", "open", "high", "low", "close", "volume", "vwap",
               "dollar_volume", "transactions", "minutes", "last_trade", "stale"]
CLOCK_COLUMNS = ["bar_start", "bar_end", "minutes_from_open", "minutes_to_close", "last"]


def _typical() -> pl.Expr:
    return (pl.col("high") + pl.col("low") + pl.col("close")) / 3


def resample(minutes: pl.DataFrame | pl.LazyFrame, sessions: pl.DataFrame, freq: str | int = "5min") -> pl.DataFrame:
    """Aggregate minute bars into regular-session bars.

    minutes: security_id, ticker, date, timestamp (minute start, UTC), open, high, low, close,
    volume[, transactions]. Minutes outside the regular session are dropped. A bar holds the
    minutes that start in [bar_start, bar_end); only bars with at least one minute are returned
    (see `grid` for every bar). `vwap` is the volume-weighted typical price (h + l + c) / 3 of
    its minutes: the flat files carry no VWAP. `last_trade` is the start of the last minute with
    a print.
    """
    n = parse_freq(freq)
    clk = clock(sessions, n)
    lf = minutes.lazy()
    if "transactions" not in lf.collect_schema().names():
        lf = lf.with_columns(transactions=pl.lit(None, pl.Int64))
    out = (
        lf.join(sessions.lazy().select("date", "market_open", "market_close"), on="date")
        .filter((pl.col("timestamp") >= pl.col("market_open")) & (pl.col("timestamp") < pl.col("market_close")))
        .with_columns(bar=((pl.col("timestamp") - pl.col("market_open")).dt.total_minutes() // n).cast(pl.Int32),
                      _dv=_typical() * pl.col("volume"))
        .sort("security_id", "timestamp")
        .group_by("date", "security_id", "bar")
        .agg(pl.col("ticker").last(), pl.col("open").first(), pl.col("high").max(), pl.col("low").min(),
             pl.col("close").last(), pl.col("volume").sum(), pl.col("_dv").sum().alias("dollar_volume"),
             pl.col("transactions").sum(), pl.len().cast(pl.Int32).alias("minutes"),
             pl.col("timestamp").last().alias("last_trade"))
        .join(clk.lazy(), on=["date", "bar"])
        .with_columns(vwap=pl.when(pl.col("volume") > 0).then(pl.col("dollar_volume") / pl.col("volume")),
                      stale=pl.lit(False))
        .select(BAR_COLUMNS)
        .sort("security_id", "date", "bar")
    )
    return out.collect()


def grid(bars: pl.DataFrame, sessions: pl.DataFrame, freq: str | int = "5min",
         security_ids: Iterable[str] | None = None) -> pl.DataFrame:
    """Every bar of every session for each security: the frame strategies work on.

    Securities are those with a bar that day, or `security_ids` on every day. An empty bar has
    volume 0, `stale` true, vwap null and the previous bar's close as open/high/low/close, carried
    forward within the day only: prices before a security's first print of the day stay null.
    A halt is a run of stale bars.
    """
    clk = clock(sessions, freq)
    if security_ids is None:
        keys = bars.select("date", "security_id").unique()
    else:
        ids = pl.DataFrame({"security_id": list(dict.fromkeys(security_ids))}, schema={"security_id": pl.String})
        keys = clk.select("date").unique().join(ids, how="cross")
    data = bars.drop([c for c in CLOCK_COLUMNS if c in bars.columns])
    per_day = ["security_id", "date"]
    return (
        keys.lazy()
        .join(clk.lazy(), on="date")
        .join(data.lazy(), on=["date", "security_id", "bar"], how="left")
        .sort("security_id", "date", "bar")
        .with_columns(stale=pl.col("minutes").is_null(),
                      _carry=pl.col("close").forward_fill().over(per_day))
        .with_columns([pl.coalesce(c, "_carry").alias(c) for c in ("open", "high", "low", "close")]
                      + [pl.col(c).fill_null(0) for c in ("volume", "dollar_volume", "transactions", "minutes")]
                      + [pl.col("ticker").forward_fill().backward_fill().over(per_day),
                         pl.col("last_trade").forward_fill().over(per_day)])
        .select(BAR_COLUMNS)
        .collect()
    )
