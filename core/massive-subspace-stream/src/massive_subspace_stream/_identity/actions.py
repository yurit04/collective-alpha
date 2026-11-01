"""Map Massive's splits and dividends to securities and compute dividend factors per security.

Massive files a company's historical actions under its *current* ticker (United Technologies'
2004-2020 dividends appear under RTX), and sometimes under the old ticker too. So an action is
assigned to:
1. the security whose last ticker is the action's ticker and whose life covers the date
   (when several do, the one still trading, else the ticker's holder on that date);
2. otherwise the security holding that ticker on that date (with a few days of grace, since a
   final dividend can come after the last trade);
3. otherwise nothing (counted as unmapped or ambiguous).
Duplicates are then dropped per security.
"""

from __future__ import annotations

import math

import polars as pl

from .build import map_to_security

GRACE_DAYS = 7


def map_actions(actions: pl.DataFrame, date_col: str, master: pl.DataFrame,
                securities: pl.DataFrame, grace_days: int = GRACE_DAYS) -> pl.DataFrame:
    """Add `security_id` and `mapped_by` to an actions table that has `ticker` and `date_col`."""
    a = actions.filter(pl.col(date_col).is_not_null()).with_row_index("__row")
    sec = securities.select("security_id", "last_ticker", "first_trade", "last_trade", "active")
    cur = (a.select("__row", "ticker", date_col)
           .join(sec, left_on="ticker", right_on="last_ticker")
           .filter(pl.col(date_col) >= pl.col("first_trade"),
                   pl.col("active") | (pl.col(date_col) <= pl.col("last_trade") + pl.duration(days=grace_days)))
           .group_by("__row")
           .agg(pl.col("security_id").alias("__cand"),
                pl.col("security_id").filter(pl.col("active")).alias("__cand_active")))
    holder = (map_to_security(a.select("__row", "ticker", date_col), master, date_col, grace_days)
              .select("__row", pl.col("security_id").alias("__holder")))
    j = a.join(cur, on="__row", how="left").join(holder, on="__row", how="left")
    n = pl.col("__cand").list.len().fill_null(0)
    n_active = pl.col("__cand_active").list.len().fill_null(0)
    holder_in = pl.col("__cand").list.contains(pl.col("__holder")).fill_null(False)
    sid = (pl.when(n == 1).then(pl.col("__cand").list.first())
           .when((n > 1) & (n_active == 1)).then(pl.col("__cand_active").list.first())
           .when((n > 1) & holder_in).then(pl.col("__holder"))
           .when(n == 0).then(pl.col("__holder"))
           .otherwise(None))
    how = (pl.when(n == 1).then(pl.lit("current_ticker"))
           .when((n > 1) & (n_active == 1)).then(pl.lit("current_ticker"))
           .when((n > 1) & holder_in).then(pl.lit("holder"))
           .when((n == 0) & pl.col("__holder").is_not_null()).then(pl.lit("holder"))
           .when(n > 1).then(pl.lit("ambiguous"))
           .otherwise(pl.lit("unmapped")))
    return (j.with_columns(sid.alias("security_id"), how.alias("mapped_by"))
            .drop("__row", "__cand", "__cand_active", "__holder"))


MALFORMED_STOCK_DIVIDEND = 10.0


def fix_stock_dividends(splits: pl.DataFrame) -> pl.DataFrame:
    """Massive records some stock dividends as 1 -> N instead of (N-1) -> N (TotalEnergies'
    scrip dividends appear as 1 -> 91.6, a 91-for-1 split). A stock dividend above 900% is not
    plausible: read those as (N-1) -> N."""
    if "adjustment_type" not in splits.columns or splits.is_empty():
        return splits
    bad = ((pl.col("adjustment_type") == "stock_dividend") & (pl.col("split_from") == 1)
           & (pl.col("split_to") > MALFORMED_STOCK_DIVIDEND))
    return splits.with_columns(pl.when(bad).then(pl.col("split_to") - 1).otherwise(pl.col("split_from"))
                               .alias("split_from"))


def mapped_splits(splits: pl.DataFrame, master: pl.DataFrame, securities: pl.DataFrame,
                  overrides: pl.DataFrame | None = None) -> pl.DataFrame:
    """security_id, ticker, execution_date, split_from, split_to, source (one row per security and date)."""
    cols = ["ticker", "execution_date", "split_from", "split_to"]
    if splits.height:
        splits = fix_stock_dividends(splits.cast({"split_from": pl.Float64, "split_to": pl.Float64}))
    base = splits.select(cols).with_columns(pl.lit("massive").alias("source")) if splits.height \
        else pl.DataFrame(schema={"ticker": pl.String, "execution_date": pl.Date, "split_from": pl.Float64,
                                  "split_to": pl.Float64, "source": pl.String})
    m = map_actions(base.cast({"split_from": pl.Float64, "split_to": pl.Float64}), "execution_date",
                    master, securities)
    if overrides is not None and overrides.height:
        ov = overrides.filter(pl.col("action") == "split").select(
            "ticker", pl.col("date").alias("execution_date"), pl.col("split_from").cast(pl.Float64),
            pl.col("split_to").cast(pl.Float64), pl.lit("override").alias("source"))
        if ov.height:
            ovm = map_to_security(ov, master, "execution_date").with_columns(pl.lit("override").alias("mapped_by"))
            m = pl.concat([ovm, m], how="diagonal_relaxed")  # overrides win the de-duplication
    return (m.filter(pl.col("security_id").is_not_null() & (pl.col("split_to") > 0))
            .unique(["security_id", "execution_date"], keep="first", maintain_order=True)
            .select("security_id", "ticker", "execution_date", "split_from", "split_to", "source", "mapped_by")
            .sort("security_id", "execution_date"))


PRICE_CHECK_MIN = 1.25   # splits closer to 1 than this are not checked against prices
PRICE_CHECK_GAP = 10     # days: the bars on both sides must be this close to the split


def check_splits_against_prices(splits: pl.DataFrame, closes: pl.LazyFrame) -> pl.DataFrame:
    """Drop mapped splits the security's own price contradicts: a real split moves the close
    by about the split ratio; when the price is flat across it (or moves the other way), the
    record belongs to another company or is wrong (Health Care REIT never had the 1-for-165
    reverse split mapped to it). Splits without bars close on both sides, and overrides, stay.
    Adds `price_check` (confirmed, unchecked)."""
    if splits.is_empty():
        return splits.with_columns(pl.lit(None, pl.String).alias("price_check"))
    k = (pl.col("split_to") / pl.col("split_from")).log()
    test = splits.filter((k.abs() > math.log(PRICE_CHECK_MIN)) & (pl.col("source") != "override"))
    px = (closes.filter(pl.col("security_id").is_in(test["security_id"].unique().to_list())
                        & pl.col("close").gt(0))
          .select("security_id", "date", "close").collect().sort("date"))
    t = test.select("security_id", "execution_date", k.alias("__k")).sort("execution_date")
    t = t.with_columns((pl.col("execution_date") - pl.duration(days=1)).alias("__before")).join_asof(
        px.rename({"date": "__d0", "close": "__c0"}), left_on="__before", right_on="__d0", by="security_id",
        strategy="backward", check_sortedness=False)
    t = t.join_asof(px.rename({"date": "__d1", "close": "__c1"}), left_on="execution_date", right_on="__d1",
                    by="security_id", strategy="forward", check_sortedness=False)
    seen = (pl.col("__c0") / pl.col("__c1")).log()
    testable = pl.col("__c0").is_not_null() & pl.col("__c1").is_not_null() & (
        (pl.col("__d1") - pl.col("__d0")).dt.total_days() <= PRICE_CHECK_GAP)
    t = t.with_columns(pl.when(~testable).then(pl.lit("unchecked"))
                       .when((seen - pl.col("__k")).abs() < seen.abs()).then(pl.lit("confirmed"))
                       .otherwise(pl.lit("contradicted")).alias("price_check"))
    out = splits.join(t.select("security_id", "execution_date", "price_check"),
                      on=["security_id", "execution_date"], how="left")
    return (out.with_columns(pl.col("price_check").fill_null("unchecked"))
            .filter(pl.col("price_check") != "contradicted"))


def _massive_step_factor(dividends: pl.DataFrame) -> pl.DataFrame:
    """Per-dividend factor implied by Massive's cumulative `historical_adjustment_factor`."""
    if "historical_adjustment_factor" not in dividends.columns:
        return dividends.with_columns(pl.lit(None, pl.Float64).alias("__massive_factor"))
    d = dividends.sort("ticker", "ex_dividend_date")
    haf = pl.col("historical_adjustment_factor")
    nxt = haf.shift(-1).over("ticker")
    return d.with_columns(pl.when(nxt.is_null()).then(haf).otherwise(haf / nxt).alias("__massive_factor"))


def mapped_dividends(dividends: pl.DataFrame, master: pl.DataFrame, securities: pl.DataFrame,
                     closes: pl.LazyFrame) -> pl.DataFrame:
    """One row per security, ex-date and amount, with `factor` = 1 - cash / previous close.

    `closes` has security_id, date, close (raw daily closes). Non-USD dividends and rows
    without a usable previous close fall back to the factor implied by Massive's cumulative one.
    """
    want = ["ticker", "ex_dividend_date", "cash_amount", "currency", "distribution_type",
            "historical_adjustment_factor"]
    d = dividends.select([c for c in want if c in dividends.columns])
    for c, t in (("currency", pl.String), ("distribution_type", pl.String),
                 ("historical_adjustment_factor", pl.Float64)):
        if c not in d.columns:
            d = d.with_columns(pl.lit(None, t).alias(c))
    d = _massive_step_factor(d.filter(pl.col("ex_dividend_date").is_not_null()))
    m = (map_actions(d, "ex_dividend_date", master, securities)
         .filter(pl.col("security_id").is_not_null())
         .with_columns(pl.col("cash_amount").round(6).alias("__cash"))
         .unique(["security_id", "ex_dividend_date", "__cash"], keep="first", maintain_order=True)
         .drop("__cash"))
    prev = (m.lazy().with_columns((pl.col("ex_dividend_date") - pl.duration(days=1)).alias("__d")).sort("__d")
            .join_asof(closes.select("security_id", pl.col("date").alias("__d"), pl.col("close").alias("prev_close"))
                       .sort("__d"), on="__d", by="security_id", strategy="backward", check_sortedness=False)
            .drop("__d").collect())
    usd = pl.col("currency").is_null() | (pl.col("currency") == "USD")
    own_ok = usd & (pl.col("prev_close") > 0) & (pl.col("cash_amount") > 0) & (pl.col("cash_amount") < pl.col("prev_close"))
    mf = pl.col("__massive_factor")
    massive_ok = (mf > 0) & (mf <= 1)
    return (prev.with_columns(
                pl.when(own_ok).then(1 - pl.col("cash_amount") / pl.col("prev_close"))
                .when(massive_ok).then(mf).otherwise(1.0).alias("factor"),
                pl.when(own_ok).then(pl.lit("price")).when(massive_ok).then(pl.lit("massive"))
                .otherwise(pl.lit("none")).alias("factor_source"))
            .rename({"historical_adjustment_factor": "massive_cumulative_factor",
                     "__massive_factor": "massive_factor"})
            .sort("security_id", "ex_dividend_date"))
