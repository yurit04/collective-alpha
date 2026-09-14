"""Price/volume features from the daily panel. Windows are in sessions (rows per security)."""

from __future__ import annotations

import math

import polars as pl

from collective_alpha.features.base import FeatureContext

HORIZONS = (1, 5, 21, 63, 126, 252)


def _over(expr: pl.Expr) -> pl.Expr:
    return expr.over("security_id")


def price_features(panel: pl.DataFrame) -> pl.DataFrame:
    """panel must be sorted by (security_id, date) and carry ret, tr_index, open, high, low, close,
    volume, prev_close, adj_close, suspect_split."""
    p = panel.sort(["security_id", "date"])
    log_tr = pl.col("tr_index").log()
    dv = pl.col("close") * pl.col("volume")
    exprs: list[pl.Expr] = []
    for h in HORIZONS:
        exprs.append(_over(log_tr - log_tr.shift(h)).exp().sub(1).alias(f"ret_{h}d"))
    exprs += [
        # 12-1 momentum: return over sessions [t-252, t-21]
        _over(log_tr.shift(21) - log_tr.shift(252)).exp().sub(1).alias("mom_12_1"),
        _over(pl.col("ret").rolling_std(21, min_samples=15)).alias("vol_21d"),
        _over(pl.col("ret").rolling_std(63, min_samples=42)).alias("vol_63d"),
        # Parkinson range volatility (annualisation left to the user)
        _over(((pl.col("high") / pl.col("low")).log().pow(2) / (4 * math.log(2))).rolling_mean(21, min_samples=15))
        .sqrt()
        .alias("parkinson_21d"),
        _over(pl.col("ret").rolling_max(21, min_samples=15)).alias("max_ret_21d"),
        _over(pl.col("ret").rolling_min(21, min_samples=15)).alias("min_ret_21d"),
        (pl.col("adj_close") / _over(pl.col("adj_close").rolling_max(252, min_samples=126)) - 1).alias("dist_52w_high"),
        (pl.col("adj_close") / _over(pl.col("adj_close").rolling_min(252, min_samples=126)) - 1).alias("dist_52w_low"),
        (pl.col("open") / pl.col("prev_close") - 1).alias("gap_overnight"),
        (pl.col("close") / pl.col("open") - 1).alias("ret_intraday"),
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("range_1d"),
        dv.alias("dollar_vol"),
        _over(dv.rolling_mean(21, min_samples=15)).alias("adv_21d"),
        _over(dv.rolling_mean(63, min_samples=42)).alias("adv_63d"),
        _over((pl.col("ret").abs() / dv).rolling_mean(21, min_samples=15)).alias("amihud_21d"),
        pl.col("close").log().alias("log_price"),
        pl.col("gap_sessions"),
        pl.col("suspect_split"),
        _over(pl.col("suspect_split").cast(pl.Int32).rolling_sum(252, min_samples=1)).gt(0).alias("suspect_split_252d"),
    ]
    out = p.with_columns(exprs).with_columns(
        volume_ratio_21d=(pl.col("dollar_vol") / pl.col("adv_21d")),
        # 5-day reversal signal is just -ret_5d; keep the raw return here
    )
    cols = ["security_id", "date"] + [c for c in out.columns if c not in p.columns] + []
    return out.select(cols)


def build(ctx: FeatureContext) -> pl.DataFrame:
    return price_features(ctx.panel)
