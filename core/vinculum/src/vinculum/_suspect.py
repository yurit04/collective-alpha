"""Days that look like a split the data does not have: the split-adjusted close falls to half
or less in one day while the company's SEC share count, filed before vs after that day, rises
by about the same ratio. Real crashes do not multiply the share count; a missed split does.

Validated 2026-11-11 on the 539 one-day drops of 50% or more in the top-3000 universe
(2005-2026): 5 hits, 4 of them real splits Massive lacked or misread (Alphabet 2022, O'Reilly
2025, Core-Mark and ARC Group Worldwide 2014), 1 an acquisition paid in new shares (CAMAC
2014). A data check, not a signal: it compares counts filed after the day.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl

MIN_PRICE_RATIO = 2.0      # the close falls to half or less
SHARE_TOLERANCE = 1.3      # the count moves within this factor of the price ratio
COUNT_WINDOW_DAYS = 200    # counts filed this close to the day on each side
LOOKBACK_DAYS = 10         # bars read before `start` for the previous close

SUSPECT_SCHEMA = {
    "security_id": pl.String, "ticker": pl.String, "cik": pl.String, "date": pl.Date, "date_before": pl.Date,
    "close_before": pl.Float64,
    "close": pl.Float64, "price_ratio": pl.Float64, "shares_before": pl.Float64, "shares_after": pl.Float64,
    "share_ratio": pl.Float64, "filed_before": pl.Date, "filed_after": pl.Date,
}


def suspect_splits(panel: pl.LazyFrame, counts: pl.DataFrame, start: date | None = None,
                   end: date | None = None) -> pl.DataFrame:
    """panel: the market-cap panel (security_id, ticker, date, raw close, split_factor; the
    split-adjusted close is their product). counts: SEC share counts per security (security_id,
    filed, shares as reported). One row per suspect day, oldest first; `close_before` and
    `close` are split-adjusted, so a split the data has never shows."""
    lf = panel.select("security_id", "ticker", "cik", "date",
                      (pl.col("close") * pl.col("split_factor")).alias("close"))
    if start:
        lf = lf.filter(pl.col("date") >= start - timedelta(days=LOOKBACK_DAYS))
    if end:
        lf = lf.filter(pl.col("date") <= end)
    prev = pl.col("close").shift(1).over("security_id")
    drops = (lf.filter(pl.col("close") > 0).sort("security_id", "date")
             .with_columns(prev.alias("close_before"), pl.col("date").shift(1).over("security_id").alias("date_before"))
             .filter((pl.col("close_before") / pl.col("close")) >= MIN_PRICE_RATIO)
             .collect())
    if start:
        drops = drops.filter(pl.col("date") >= start)
    if drops.is_empty():
        return pl.DataFrame(schema=SUSPECT_SCHEMA)
    c = (counts.filter((pl.col("shares") > 0) & pl.col("filed").is_not_null())
         .filter(pl.col("security_id").is_in(drops["security_id"].unique().to_list()))
         .sort("security_id", "filed", "shares").unique(["security_id", "filed"], keep="last")
         .select("security_id", "filed", "shares").sort("filed"))
    tol = timedelta(days=COUNT_WINDOW_DAYS)
    d = drops.with_columns((pl.col("date") - pl.duration(days=1)).alias("__day_before")).sort("__day_before")
    d = d.join_asof(c.rename({"filed": "filed_before", "shares": "shares_before"}), left_on="__day_before",
                    right_on="filed_before", by="security_id", strategy="backward", tolerance=tol,
                    check_sortedness=False).sort("date")
    d = d.join_asof(c.rename({"filed": "filed_after", "shares": "shares_after"}), left_on="date",
                    right_on="filed_after", by="security_id", strategy="forward", tolerance=tol,
                    check_sortedness=False)
    price = pl.col("close_before") / pl.col("close")
    share = pl.col("shares_after") / pl.col("shares_before")
    return (d.with_columns(price.alias("price_ratio"), share.alias("share_ratio"))
            .filter(((pl.col("share_ratio").log() - pl.col("price_ratio").log()).abs() < math.log(SHARE_TOLERANCE))
                    .fill_null(False))
            .select(list(SUSPECT_SCHEMA)).cast(SUSPECT_SCHEMA).sort("date", "security_id"))
