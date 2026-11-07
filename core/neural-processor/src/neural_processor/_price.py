"""Price signals from daily Massive bars (adjust="all": `close` is a total-return price,
`close / div_factor` the split-adjusted price, times `volume` the dollar volume).

Every value on day d uses bars up to and including d's close.
"""

from __future__ import annotations

import math

import polars as pl

from ._lineage import as_lookup, from_lookup, relabel

LOOKBACK_DAYS = 400       # calendar days of bars needed before the first panel date
STALE = "10d"             # a price lookup further back than this is missing (stopped trading)
VOL_WINDOW = 63
BETA_WINDOW = 252
HIGH_WINDOW = 252
BREAK = 3.0               # daily return above +300%: a data break
MARKET = "SPY"            # beta is measured against this ticker's daily total return


def daily(bars: pl.LazyFrame, caps: pl.LazyFrame | None, links: pl.DataFrame | None = None) -> pl.LazyFrame:
    """Daily table: security_id, date, tr (total-return index), px (split-adjusted price index),
    dv (dollar volume), ret, market_cap (when `caps` is given). With `links` (see _lineage), a predecessor's bars before the link continue the
    successor's series; the return across the link comes from the raw closes (a ~1:1 exchange),
    and tr / px are rebuilt from returns so their levels are continuous."""
    g = "security_id"
    d = (bars.select("security_id", "date", pl.col("close").alias("__tr"),
                     (pl.col("close") / pl.col("div_factor")).alias("__px"),
                     (pl.col("close") / (pl.col("div_factor") * pl.col("split_factor"))).alias("__raw"), "volume")
         .filter(pl.col("security_id").is_not_null() & (pl.col("__tr") > 0))
         .with_columns((pl.col("__px") * pl.col("volume")).alias("dv"), pl.col("security_id").alias("__orig")))
    if caps is not None:
        d = d.join(caps.select(pl.col("security_id").alias("__orig"), "date", "market_cap"),
                   on=["__orig", "date"], how="left")
    else:
        d = d.with_columns(pl.lit(None, pl.Float64).alias("market_cap"))
    if links is not None and links.height:
        d = relabel(d, links)
    d = d.sort(g, "date")
    same = pl.col("__orig") == pl.col("__orig").shift(1).over(g)
    step = lambda col: pl.when(same).then(pl.col(col) / pl.col(col).shift(1).over(g)).otherwise(  # noqa: E731
        pl.col("__raw") / pl.col("__raw").shift(1).over(g))
    # A daily return above +300% is a data break (bankruptcy emergences: pennies to $20+ under the
    # same security), not a return: count it as 0.
    brk = (step("__tr") - 1) > BREAK
    d = (d.with_columns(pl.when(brk).then(0.0).otherwise(step("__tr") - 1).alias("ret"),
                        pl.when(brk).then(1.0).otherwise(step("__px")).alias("__pxr"))
         .with_columns((1 + pl.col("ret").fill_null(0)).cum_prod().over(g).alias("tr"),
                       pl.col("__pxr").fill_null(1).cum_prod().over(g).alias("px")))
    return d


def market_returns(bars: pl.LazyFrame | pl.DataFrame) -> pl.LazyFrame:
    """One security's daily bars (adjust="all") -> date, mkt (daily total return)."""
    return (bars.lazy().filter(pl.col("close") > 0).sort("date")
            .select("date", (pl.col("close") / pl.col("close").shift(1) - 1).alias("mkt")))


def rolling_stats(d: pl.LazyFrame, names: list[str]) -> pl.LazyFrame:
    """Add the rolling columns the requested signals need (rows sorted first: joins do not keep
    row order, and the windows run over each security's rows in date order)."""
    g = "security_id"
    d = d.sort(g, "date")
    cols = []
    if "volatility_3m" in names:
        cols.append((pl.col("ret").rolling_std(VOL_WINDOW, min_samples=50).over(g) * math.sqrt(252))
                    .alias("volatility_3m"))
    if "high_52w" in names:
        cols.append((pl.col("px") / pl.col("px").rolling_max(HIGH_WINDOW, min_samples=200).over(g))
                    .alias("high_52w"))
    if "turnover" in names:
        cols.append((pl.col("dv").rolling_mean(VOL_WINDOW, min_samples=50).over(g)
                     / pl.when(pl.col("market_cap") > 0).then(pl.col("market_cap"))).alias("turnover"))
    if "illiquidity" in names:
        amihud = pl.when(pl.col("dv") > 0).then(pl.col("ret").abs() / pl.col("dv"))
        cols.append((amihud.rolling_mean(VOL_WINDOW, min_samples=50).over(g) * 1e6).alias("illiquidity"))
    if "beta_12m" in names:
        rm = lambda e: e.rolling_mean(BETA_WINDOW, min_samples=200).over(g)  # noqa: E731
        r, m = pl.col("ret"), pl.col("mkt")
        both = r.is_not_null() & m.is_not_null()
        r, m = pl.when(both).then(r), pl.when(both).then(m)
        var = rm(m * m) - rm(m) ** 2
        cols.append(pl.when(var > 0).then((rm(r * m) - rm(r) * rm(m)) / var).alias("beta_12m"))
    return d.with_columns(cols) if cols else d


def _price_at(daily_tr: pl.DataFrame, keys: pl.DataFrame, offset: str, name: str) -> pl.DataFrame:
    """keys (__key, security_id, date) + `name` = last total-return price on or before date + offset."""
    k = keys.with_columns(pl.col("date").dt.offset_by(offset).alias("__at")).sort("__at")
    return (k.join_asof(daily_tr.rename({"date": "__d"}), left_on="__at", right_on="__d", by="security_id",
                        strategy="backward", tolerance=STALE, check_sortedness=False)
            .select("__key", "security_id", "date", pl.col("tr").alias(name)))


def price_signals(bars: pl.LazyFrame, caps: pl.LazyFrame | None, keys: pl.DataFrame, names: list[str],
                  links: pl.DataFrame | None = None, market: pl.LazyFrame | None = None) -> pl.DataFrame:
    """Price signals for keys (security_id, date). `bars`: lazy daily bars (adjust="all") of
    those securities (and their predecessors) from LOOKBACK_DAYS before the first date; `caps`:
    security_id, date, market_cap (for turnover); `links`: security handoffs to follow (see
    _lineage); `market`: date, mkt (market_returns of MARKET), needed for beta_12m, so a
    security's beta never depends on which other securities were asked for."""
    keys = keys.select("security_id", "date").unique()
    out = keys
    look = as_lookup(keys, links)       # a predecessor's own rows find its history under the successor
    need_daily = [n for n in names if n not in ("momentum_12_1", "reversal_1m")]
    d = daily(bars, caps if "turnover" in names else None, links)
    if "beta_12m" in names:
        if market is None:
            raise ValueError("beta_12m needs the market's daily returns (market=market_returns(...))")
        d = d.join(market.lazy(), on="date", how="left")
    if need_daily:
        stats = (rolling_stats(d, need_daily).select("security_id", "date", *need_daily)
                 .join(look.lazy(), on=["security_id", "date"]).collect())
        out = out.join(from_lookup(stats), on=["security_id", "date"], how="left")
    if {"momentum_12_1", "reversal_1m"} & set(names):
        tr = d.select("security_id", "date", "tr").collect().sort("date")
        p0 = _price_at(tr, look, "0d", "__p0")
        p1 = _price_at(tr, look, "-1mo", "__p1")
        p12 = _price_at(tr, look, "-12mo", "__p12")
        on = ["__key", "security_id", "date"]
        px = from_lookup(p0.join(p1, on=on).join(p12, on=on))
        sig = []
        if "momentum_12_1" in names:
            sig.append((pl.col("__p1") / pl.col("__p12") - 1).alias("momentum_12_1"))
        if "reversal_1m" in names:
            sig.append((pl.col("__p0") / pl.col("__p1") - 1).alias("reversal_1m"))
        out = out.join(px.select("security_id", "date", *sig), on=["security_id", "date"], how="left")
    return out
