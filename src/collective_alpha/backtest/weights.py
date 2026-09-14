"""Turn a signal (security_id, date, signal) into target portfolio weights on rebalance dates."""

from __future__ import annotations

import datetime as dt

import polars as pl


def rebalance_dates(dates: list[dt.date], every: int | str = 21) -> set[dt.date]:
    """Every n sessions, or 'monthly' / 'weekly' (first session of the period)."""
    if isinstance(every, int):
        return set(dates[::every])
    firsts: dict[tuple, dt.date] = {}
    for d in dates:
        key = (d.year, d.month) if every == "monthly" else d.isocalendar()[:2]
        firsts.setdefault(key, d)
    return set(firsts.values())


def long_short_quantiles(
    signal: pl.DataFrame,
    n_quantiles: int = 10,
    long_q: int | None = None,
    short_q: int | None = 1,
    gross: float = 2.0,
    max_weight: float | None = None,
    signal_col: str = "signal",
) -> pl.DataFrame:
    """Equal-weight long top quantile, short bottom quantile, dollar neutral, gross exposure `gross`.
    short_q=None -> long-only (gross is the long exposure)."""
    long_q = long_q or n_quantiles
    s = signal.filter(pl.col(signal_col).is_not_null()).with_columns(
        q=(
            (pl.col(signal_col).rank(method="ordinal").over("date") - 1)
            * n_quantiles
            // pl.col(signal_col).count().over("date")
            + 1
        ).cast(pl.Int32)
    )
    if short_q is None:
        legs = s.filter(pl.col("q") == long_q).with_columns(side=pl.lit(1.0))
        leg_gross = gross
    else:
        legs = s.filter((pl.col("q") == long_q) | (pl.col("q") == short_q)).with_columns(
            side=pl.when(pl.col("q") == long_q).then(1.0).otherwise(-1.0)
        )
        leg_gross = gross / 2
    w = legs.with_columns(n=pl.len().over(["date", "side"])).with_columns(
        weight=pl.col("side") * leg_gross / pl.col("n")
    )
    if max_weight is not None:
        w = w.with_columns(weight=pl.col("weight").clip(-max_weight, max_weight))
    return w.select("security_id", "date", "weight")


def signal_weighted(
    signal: pl.DataFrame,
    gross: float = 2.0,
    net: float = 0.0,
    max_weight: float | None = None,
    signal_col: str = "signal",
) -> pl.DataFrame:
    """Weights proportional to the demeaned cross-sectional rank (so long and short legs balance),
    scaled to `gross`; `net` shifts the whole book long (0 = dollar neutral)."""
    s = signal.filter(pl.col(signal_col).is_not_null()).with_columns(
        r=pl.col(signal_col).rank().over("date") / pl.col(signal_col).count().over("date")
    )
    s = s.with_columns(x=pl.col("r") - pl.col("r").mean().over("date"))
    s = s.with_columns(weight=pl.col("x") / pl.col("x").abs().sum().over("date") * gross)
    if net:
        s = s.with_columns(weight=pl.col("weight") + net / pl.col("x").count().over("date"))
    if max_weight is not None:
        s = s.with_columns(weight=pl.col("weight").clip(-max_weight, max_weight))
    return s.select("security_id", "date", "weight")


def on_rebalance_dates(weights: pl.DataFrame, dates: list[dt.date], every: int | str) -> pl.DataFrame:
    rd = rebalance_dates(dates, every)
    return weights.filter(pl.col("date").is_in(list(rd)))
