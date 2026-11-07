"""Per-bar intraday features. Every value at a bar uses only that day's bars up to it (and, for
`vol_ratio`, earlier days)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class Feature:
    name: str
    description: str
    needs: str = ""          # "prev_close": bars need the prev_close column; "history": earlier days


FEATURES: dict[str, Feature] = {f.name: f for f in [
    Feature("ret_bar", "close / previous bar's close - 1 (first bar of the day: close / open - 1)"),
    Feature("ret_open", "close / the day's first open - 1 (the first open is the opening auction)"),
    Feature("ret_prev", "close / previous day's official close - 1", "prev_close"),
    Feature("gap", "the day's first open / previous day's official close - 1", "prev_close"),
    Feature("vwap_dev", "close / VWAP since the open - 1"),
    Feature("rvol", "realized volatility since the open: sqrt(sum of squared log bar returns), not annualized"),
    Feature("range_pos", "(close - low so far) / (high so far - low so far); null while the range is 0"),
    Feature("or_high", "high of the opening range (bars ending within or_minutes of the open); null before it ends"),
    Feature("or_low", "low of the opening range; null before it ends"),
    Feature("or_break", "+1 above the opening range, -1 below it, 0 inside; null before it ends"),
    Feature("vol_ratio", "volume since the open / its average at the same bar over the previous `lookback` days", "history"),
]}


def _check(names: Iterable[str] | str | None) -> list[str]:
    if names is None:
        return list(FEATURES)
    names = [names] if isinstance(names, str) else list(names)
    unknown = [n for n in names if n not in FEATURES]
    if unknown:
        raise ValueError(f"unknown features {unknown}; known: {', '.join(FEATURES)}")
    return names


def features(bars: pl.DataFrame, names: Iterable[str] | str | None = None, or_minutes: int = 30,
             lookback: int = 20, history: pl.DataFrame | None = None) -> pl.DataFrame:
    """Add feature columns to bars (output of `resample` or `grid`; `grid` is needed for
    `vol_ratio`, which compares the same bar index across days).

    names      features to add (default all; those needing `prev_close` are skipped when the
               column is missing unless asked for by name, which raises).
    or_minutes opening range length; bars that end within it form the range.
    lookback   days averaged for `vol_ratio` (a quarter of them, at least min(5, lookback), must exist).
    history    earlier days' bars (same layout) for `vol_ratio` when `bars` is a single day;
               only their volume is used and their rows are not returned.
    """
    asked = names is not None
    names = _check(names)
    has_prev = "prev_close" in bars.columns
    missing = [n for n in names if FEATURES[n].needs == "prev_close" and not has_prev]
    if missing and asked:
        raise ValueError(f"{missing} need the prev_close column: pass prev_close=True when loading bars")
    names = [n for n in names if n not in missing]

    g = ["security_id", "date"]
    df = bars.sort("security_id", "date", "bar")
    prev = pl.col("close").shift(1).over(g)
    ret = (pl.col("close") / pl.coalesce(prev, pl.col("open")) - 1)
    # The day's first open is known only from the bar that printed it (null before).
    seen = pl.col("open").is_not_null().cum_sum().over(g) > 0
    first_open = pl.when(seen).then(pl.col("open").drop_nulls().first().over(g))
    exprs: dict[str, pl.Expr] = {
        "ret_bar": ret,
        "ret_open": pl.col("close") / first_open - 1,
        "ret_prev": pl.col("close") / pl.col("prev_close") - 1,
        "gap": first_open / pl.col("prev_close") - 1,
        "vwap_dev": pl.when(pl.col("volume").cum_sum().over(g) > 0)
                      .then(pl.col("close") / (pl.col("dollar_volume").cum_sum().over(g)
                                               / pl.col("volume").cum_sum().over(g)) - 1),
        "rvol": pl.when(pl.col("close").is_not_null())
                  .then(ret.log1p().pow(2).fill_null(0).cum_sum().over(g).sqrt()),
    }
    lo, hi = pl.col("low").cum_min().over(g), pl.col("high").cum_max().over(g)
    exprs["range_pos"] = pl.when(hi > lo).then((pl.col("close") - lo) / (hi - lo))
    in_range = pl.col("minutes_from_open") <= or_minutes
    done = (pl.col("minutes_from_open") >= or_minutes) & in_range.any().over(g)
    or_hi = pl.col("high").filter(in_range).max().over(g)
    or_lo = pl.col("low").filter(in_range).min().over(g)
    exprs["or_high"] = pl.when(done).then(or_hi)
    exprs["or_low"] = pl.when(done).then(or_lo)
    exprs["or_break"] = (pl.when(~done | pl.col("close").is_null()).then(None)
                         .when(pl.col("close") > or_hi).then(1)
                         .when(pl.col("close") < or_lo).then(-1)
                         .otherwise(0).cast(pl.Int8))

    out = df.with_columns([exprs[n].alias(n) for n in names if n != "vol_ratio"])
    if "vol_ratio" in names:
        out = out.join(_vol_ratio(df, history, lookback), on=["security_id", "date", "bar"], how="left")
    return out


def _vol_ratio(bars: pl.DataFrame, history: pl.DataFrame | None, lookback: int) -> pl.DataFrame:
    cols = ["security_id", "date", "bar", "volume"]
    frames = [bars.select(cols)]
    if history is not None and history.height:
        days = set(bars["date"].unique().to_list())
        frames.insert(0, history.select(cols).filter(~pl.col("date").is_in(list(days))))
    if lookback < 1:
        raise ValueError("lookback must be at least 1 day")
    min_obs = min(lookback, max(5, lookback // 4))
    return (
        pl.concat(frames)
        .sort("security_id", "date", "bar")
        .with_columns(cum=pl.col("volume").cum_sum().over("security_id", "date"))
        .sort("security_id", "bar", "date")
        .with_columns(base=pl.col("cum").shift(1).rolling_mean(lookback, min_samples=min_obs)
                      .over("security_id", "bar"))
        .filter(pl.col("date").is_in(bars["date"].unique().to_list()))
        .select("security_id", "date", "bar",
                vol_ratio=pl.when(pl.col("base") > 0).then(pl.col("cum") / pl.col("base")))
    )
