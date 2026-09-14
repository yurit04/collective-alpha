"""Clean splits and cash dividends and key them by security_id."""

from __future__ import annotations

import logging

import polars as pl

log = logging.getLogger(__name__)

CASH_DIVIDEND_TYPES = ("CD", "SC")  # regular cash, special cash (LT/ST capital gains excluded)


def clean_splits(splits: pl.DataFrame) -> pl.DataFrame:
    """(ticker, date, split_ratio) with ratio = new shares per old share (2 for a 2-for-1, 0.1 for a 1-for-10)."""
    df = (
        splits.select(
            "ticker",
            pl.col("execution_date").cast(pl.Utf8).str.to_date(strict=False).alias("date"),
            (pl.col("split_to").cast(pl.Float64) / pl.col("split_from").cast(pl.Float64)).alias("split_ratio"),
        )
        .filter(pl.col("date").is_not_null() & pl.col("split_ratio").is_finite() & (pl.col("split_ratio") > 0))
        .group_by(["ticker", "date"])
        .agg(pl.col("split_ratio").product())  # two events on one day compound
        .filter(pl.col("split_ratio") != 1.0)
    )
    return df.sort(["ticker", "date"])


def clean_dividends(dividends: pl.DataFrame, currency: str = "USD") -> pl.DataFrame:
    """(ticker, date, dividend) cash per share on the ex-date, summed over same-day cash dividends."""
    df = (
        dividends.filter(
            pl.col("dividend_type").is_in(list(CASH_DIVIDEND_TYPES))
            & (pl.col("currency").fill_null(currency) == currency)
            & (pl.col("cash_amount").cast(pl.Float64) > 0)
        )
        .select(
            "ticker",
            pl.col("ex_dividend_date").cast(pl.Utf8).str.to_date(strict=False).alias("date"),
            pl.col("cash_amount").cast(pl.Float64).alias("dividend"),
        )
        .filter(pl.col("date").is_not_null())
        .unique()  # vendor duplicates with different ids but identical economics
        .group_by(["ticker", "date"])
        .agg(pl.col("dividend").sum())
    )
    return df.sort(["ticker", "date"])


def map_events_to_security(events: pl.DataFrame, master: pl.DataFrame, grace_days: int = 7) -> pl.DataFrame:
    """Attach security_id to (ticker, date) events. An event dated a few days after a ticker's last
    trade (e.g. a delisting dividend) still maps, within `grace_days`."""
    m = master.select("ticker", "valid_from", "valid_to", "security_id").sort(["ticker", "valid_from"])
    out = (
        events.sort(["ticker", "date"])
        .join_asof(m, left_on="date", right_on="valid_from", by="ticker", strategy="backward", check_sortedness=False)
        .with_columns(
            security_id=pl.when(pl.col("date") <= pl.col("valid_to") + pl.duration(days=grace_days))
            .then(pl.col("security_id"))
            .otherwise(None)
        )
        .drop(["valid_from", "valid_to"])
    )
    unmapped = out["security_id"].null_count()
    if unmapped:
        log.info("%d of %d events did not map to a security (ticker never traded in window)", unmapped, out.height)
    return out.filter(pl.col("security_id").is_not_null())
