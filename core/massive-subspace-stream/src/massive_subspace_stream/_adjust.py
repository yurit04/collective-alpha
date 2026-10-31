"""Split and dividend adjustment, applied at query time to unadjusted bars.

Split factor for a bar on date d = product of split_from / split_to over all
splits with execution_date > d. Prices are multiplied by it, volume divided.

Dividend factor for a bar on date d = Massive's cumulative
`historical_adjustment_factor` of the earliest dividend with
ex_dividend_date > d (it already compounds every later dividend). Used only
with adjust="all", which gives total-return prices.
"""

from __future__ import annotations

import polars as pl

PRICE_COLUMNS = ("open", "high", "low", "close", "vwap")
VOLUME_COLUMNS = ("volume",)


def split_factor_table(splits: pl.DataFrame) -> pl.DataFrame:
    """ticker, eff_date, split_factor: factor for bars dated <= eff_date (= execution_date - 1 day)."""
    if splits.is_empty():
        return pl.DataFrame(schema={"ticker": pl.String, "eff_date": pl.Date, "split_factor": pl.Float64})
    ratio = (pl.col("split_from") / pl.col("split_to")).fill_nan(1.0).fill_null(1.0)
    return (
        splits.filter(pl.col("execution_date").is_not_null() & (pl.col("split_to") > 0))
        .group_by("ticker", "execution_date")
        .agg(ratio.product().alias("ratio"))
        .sort("ticker", "execution_date", descending=[False, True])
        .with_columns(pl.col("ratio").cum_prod().over("ticker").alias("split_factor"))
        .select("ticker", (pl.col("execution_date") - pl.duration(days=1)).alias("eff_date"), "split_factor")
        .sort("eff_date")
    )


def dividend_factor_table(dividends: pl.DataFrame) -> pl.DataFrame:
    if dividends.is_empty() or "historical_adjustment_factor" not in dividends.columns:
        return pl.DataFrame(schema={"ticker": pl.String, "eff_date": pl.Date, "div_factor": pl.Float64})
    return (
        dividends.filter(pl.col("ex_dividend_date").is_not_null() & pl.col("historical_adjustment_factor").is_not_null())
        .group_by("ticker", "ex_dividend_date")
        .agg(pl.col("historical_adjustment_factor").min().alias("div_factor"))
        .select("ticker", (pl.col("ex_dividend_date") - pl.duration(days=1)).alias("eff_date"), "div_factor")
        .sort("eff_date")
    )


def _attach(bars: pl.LazyFrame, table: pl.DataFrame, factor: str) -> pl.LazyFrame:
    # Forward as-of join: first event whose eff_date >= bar date, i.e. execution date > bar date.
    joined = bars.sort("date").join_asof(
        table.lazy().select("ticker", "eff_date", factor),
        left_on="date",
        right_on="eff_date",
        by="ticker",
        strategy="forward",
        check_sortedness=False,
    )
    return joined.with_columns(pl.col(factor).fill_null(1.0)).drop("eff_date")


def adjust_bars(bars: pl.LazyFrame, mode: str | None, splits: pl.DataFrame,
                dividends: pl.DataFrame | None = None) -> pl.LazyFrame:
    """Return bars with adjusted price/volume columns. `mode` is None, 'splits', or 'all'."""
    if mode in (None, "none", False):
        return bars
    if mode not in ("splits", "all"):
        raise ValueError("adjust must be None, 'splits', or 'all'")
    names = bars.collect_schema().names()
    order = names
    out = _attach(bars, split_factor_table(splits), "split_factor")
    factor = pl.col("split_factor")
    if mode == "all":
        out = _attach(out, dividend_factor_table(dividends if dividends is not None else pl.DataFrame()), "div_factor")
        price_factor = factor * pl.col("div_factor")
    else:
        price_factor = factor
    exprs = [(pl.col(c) * price_factor).alias(c) for c in PRICE_COLUMNS if c in names]
    exprs += [(pl.col(c) / factor).alias(c) for c in VOLUME_COLUMNS if c in names]
    out = out.with_columns(exprs)
    keep = [*order, "split_factor"] + (["div_factor"] if mode == "all" else [])
    sort_cols = [c for c in ("ticker", "timestamp") if c in names] or ["ticker", "date"]
    return out.select(keep).sort(sort_cols)
