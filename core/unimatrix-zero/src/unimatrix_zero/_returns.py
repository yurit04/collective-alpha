"""Daily total returns per security, with what the engine needs to trade them.

- Bars adjusted for splits and dividends (Massive adjust="all"); `ret` is close-to-close.
- Successors of kind "security" (vinculum) are followed: the predecessor's bars before the
  link continue the successor's series and the link-day return comes from raw closes.
- A security that stops trading before the data ends gets one more row, on the next trading
  day, with its delisting return: 0 (exit at the last close) or, for a performance delisting
  (last raw close under $1, or down more than half over the last 63 trading days), the Shumway
  (1997) -30%. After that row it has no returns: the engine holds its value as cash.
- A one-day return above +300% is treated as a data break, not a tradable return (in the data
  these are bankruptcy emergences: old shares at pennies replaced by new equity at $20+ under
  the same security). That day's return is 0 and `break` is set; volatility skips breaks and
  clips daily returns at +/-50%.
- Per day also: raw close, 21-day mean dollar volume, 63-day volatility and an estimated
  half-spread (Abdi-Ranaldo 2017, from daily high, low and close over 63 days up to that one;
  noisy for liquid stocks, so the cost model uses dollar volume by default).
"""

from __future__ import annotations

from datetime import date

import polars as pl
from neural_processor import relabel

SHUMWAY = -0.30
BREAK = 3.0            # a daily return above +300% is a data break
VOL_CLIP = 0.5
PENNY = 1.0
CRASH = 0.5            # down to half or less over CRASH_DAYS trading days
CRASH_DAYS = 63
SPREAD_WINDOW = 63
ADV_WINDOW = 21
VOL_WINDOW = 63

RETURNS_SCHEMA = {
    "security_id": pl.String, "date": pl.Date, "ret": pl.Float64, "raw_close": pl.Float64,
    "adv": pl.Float64, "vol": pl.Float64, "half_spread": pl.Float64, "delisted": pl.Boolean, "break": pl.Boolean,
}


def daily_returns(bars: pl.LazyFrame, links: pl.DataFrame | None, calendar: list[date],
                  delisting: str = "shumway") -> pl.DataFrame:
    """bars: lazy daily bars (adjust="all": close, high, low, volume, split_factor, div_factor)
    of the securities and their predecessors. calendar: trading days of the whole data set (to
    date delisting rows and tell a delisting from the end of the data)."""
    if delisting not in ("shumway", "last"):
        raise ValueError("delisting must be 'shumway' or 'last'")
    g = "security_id"
    fac = pl.col("div_factor") * pl.col("split_factor")
    d = (bars.select("security_id", "date", pl.col("close").alias("__tr"), (pl.col("close") / fac).alias("raw_close"),
                     (pl.col("high") / fac).alias("__h"), (pl.col("low") / fac).alias("__l"),
                     ((pl.col("close") / fac) * pl.col("volume") * pl.col("split_factor")).alias("__dv"))
         .filter(pl.col("security_id").is_not_null() & (pl.col("__tr") > 0))
         .with_columns(pl.col("security_id").alias("__orig")))
    if links is not None and links.height:
        d = relabel(d, links)
    d = d.sort(g, "date")
    same = pl.col("__orig") == pl.col("__orig").shift(1).over(g)
    ret = (pl.when(same).then(pl.col("__tr") / pl.col("__tr").shift(1).over(g))
           .otherwise(pl.col("raw_close") / pl.col("raw_close").shift(1).over(g)) - 1)
    # Abdi-Ranaldo: s^2 = 4 (c_{t-1} - eta_{t-1}) (c_{t-1} - eta_t), logs, eta = mid-range.
    c, eta = pl.col("raw_close").log(), (pl.col("__h").log() + pl.col("__l").log()) / 2
    ar = 4 * (c.shift(1).over(g) - eta.shift(1).over(g)) * (c.shift(1).over(g) - eta)
    drop = pl.col("__tr") / pl.coalesce(pl.col("__tr").shift(CRASH_DAYS).over(g), pl.col("__tr").first().over(g))
    d = d.with_columns(ret.alias("ret"), ar.alias("__ar"), drop.alias("__drop"))
    d = d.with_columns((pl.col("ret") > BREAK).fill_null(False).alias("break"))
    d = d.with_columns(pl.when(pl.col("break")).then(0.0).otherwise(pl.col("ret")).alias("ret"))
    d = d.with_columns(
        pl.col("__dv").rolling_mean(ADV_WINDOW, min_samples=5).over(g).alias("adv"),
        pl.col("ret").clip(-VOL_CLIP, VOL_CLIP).rolling_std(VOL_WINDOW, min_samples=20).over(g).alias("vol"),
        (pl.col("__ar").rolling_mean(SPREAD_WINDOW, min_samples=20).over(g).clip(lower_bound=0).sqrt() / 2)
        .alias("half_spread"),
        pl.lit(False).alias("delisted"))
    d = d.collect()
    out = d.select(list(RETURNS_SCHEMA))
    if not calendar or d.is_empty():
        return out.cast(RETURNS_SCHEMA)
    # Delisting rows.
    end = max(calendar)
    nxt = pl.DataFrame({"date": calendar[:-1], "__next": calendar[1:]})
    last = (d.group_by(g).agg(pl.col("date").max().alias("date"), pl.col("raw_close").sort_by("date").last().alias("__c"),
                              pl.col("__drop").sort_by("date").last().alias("__drop"))
            .filter(pl.col("date") < end))
    if last.height:
        perf = (pl.col("__c") < PENNY) | (pl.col("__drop") <= CRASH)
        dret = pl.when(perf).then(pl.lit(SHUMWAY if delisting == "shumway" else 0.0)).otherwise(0.0)
        rows = (last.join(nxt, on="date", how="left").filter(pl.col("__next").is_not_null())
                .select(g, pl.col("__next").alias("date"), dret.alias("ret"), pl.col("__c").alias("raw_close"),
                        pl.lit(None, pl.Float64).alias("adv"), pl.lit(None, pl.Float64).alias("vol"),
                        pl.lit(0.0).alias("half_spread"), pl.lit(True).alias("delisted"),
                        pl.lit(False).alias("break")))
        out = pl.concat([out.cast(RETURNS_SCHEMA), rows.cast(RETURNS_SCHEMA)])
    return out.cast(RETURNS_SCHEMA).sort(g, "date")
