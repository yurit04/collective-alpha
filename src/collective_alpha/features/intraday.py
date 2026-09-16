"""Features derived from the per-session intraday aggregates.

Two kinds live here. Measurements that are simply better than their daily equivalents: realised
volatility from five-minute returns, and the effective spread. And shapes of the trading day that
the daily bar cannot show: where volume sits, how the close relates to the day's volume-weighted
price, and the split of the day's return into its overnight and intraday parts.
"""

from __future__ import annotations

import polars as pl

from collective_alpha.features.base import FeatureContext

WINDOW = 21


def _roll_mean(col: str, window: int = WINDOW, min_frac: float = 0.6) -> pl.Expr:
    return pl.col(col).rolling_mean(window, min_samples=max(2, int(window * min_frac))).over("security_id")


def _roll_median(col: str, window: int = WINDOW, min_frac: float = 0.6) -> pl.Expr:
    return pl.col(col).rolling_median(window, min_samples=max(2, int(window * min_frac))).over("security_id")


def intraday_features(intraday: pl.DataFrame, panel: pl.DataFrame) -> pl.DataFrame:
    """intraday: the curated per-session table. panel: daily bars for the overnight/intraday split."""
    day = (
        panel.select("security_id", "date", "open", "close", "prev_close", "split_ratio")
        .sort(["security_id", "date"])
        .with_columns(
            # the split applies to the whole session, so both legs are split-adjusted
            overnight=(pl.col("open") * pl.col("split_ratio")) / pl.col("prev_close") - 1,
            intraday=pl.col("close") / pl.col("open") - 1,
        )
        .select("security_id", "date", "overnight", "intraday")
    )
    df = (
        intraday.join(day, on=["security_id", "date"], how="left")
        .sort(["security_id", "date"])
        .with_columns(
            # measurements
            rv_21d=_roll_mean("rv_5m"),
            spread_21d=_roll_median("spread_est"),
            bar_coverage_21d=_roll_mean("bar_coverage"),
            # day shape
            close_to_vwap_21d=_roll_mean("close_to_vwap"),
            share_open30_21d=_roll_mean("share_open30"),
            share_close30_21d=_roll_mean("share_close30"),
            share_auction_21d=_roll_mean("share_auction"),
            share_pre_21d=_roll_mean("share_pre"),
            share_post_21d=_roll_mean("share_post"),
            or_range_21d=_roll_mean("or_range_pct"),
            ret_open30_21d=_roll_mean("ret_open30"),
            ret_close30_21d=_roll_mean("ret_close30"),
            # overnight versus intraday: cumulative over the window, in logs
            overnight_21d=pl.col("overnight").log1p().rolling_sum(WINDOW, min_samples=13).over("security_id"),
            intraday_21d=pl.col("intraday").log1p().rolling_sum(WINDOW, min_samples=13).over("security_id"),
        )
        .with_columns(
            rv_ratio=pl.when(pl.col("rv_21d") > 0).then(pl.col("rv_5m") / pl.col("rv_21d")).otherwise(None),
            spread_ratio=pl.when(pl.col("spread_21d") > 0)
            .then(pl.col("spread_est") / pl.col("spread_21d"))
            .otherwise(None),
            overnight_minus_intraday_21d=pl.col("overnight_21d") - pl.col("intraday_21d"),
        )
    )
    return df.select(
        "security_id",
        "date",
        # per-session measurements worth keeping at daily frequency
        "rv_5m",
        "rv_21d",
        "rv_ratio",
        "spread_est",
        "spread_21d",
        "spread_ratio",
        "bar_coverage",
        "bar_coverage_21d",
        "close_to_vwap",
        "close_to_vwap_21d",
        "share_open30",
        "share_close30",
        "share_auction",
        "share_open30_21d",
        "share_close30_21d",
        "share_auction_21d",
        "share_pre_21d",
        "share_post_21d",
        "or_range_pct",
        "or_range_21d",
        "close_vs_or",
        "ret_open30",
        "ret_close30",
        "ret_open30_21d",
        "ret_close30_21d",
        "overnight",
        "intraday",
        "overnight_21d",
        "intraday_21d",
        "overnight_minus_intraday_21d",
    )


def build(ctx: FeatureContext) -> pl.DataFrame:
    return intraday_features(ctx.intraday, ctx.panel)
