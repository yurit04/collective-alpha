"""Size and turnover: point-in-time shares (SEC) x price."""

from __future__ import annotations

import polars as pl

from collective_alpha.features.base import FeatureContext
from collective_alpha.universe.marketcap import shares_asof


def size_features(panel: pl.DataFrame, security_cik: pl.DataFrame, shares: pl.DataFrame) -> pl.DataFrame:
    keys = panel.select("security_id", "date", "close", "volume").join(
        security_cik, on=["security_id", "date"], how="left"
    )
    with_cik = keys.filter(pl.col("cik").is_not_null())
    sh = shares_asof(shares, with_cik, "date")
    out = keys.join(sh.select("security_id", "date", "shares", "shares_source"), on=["security_id", "date"], how="left")
    out = out.sort(["security_id", "date"]).with_columns(
        cap=pl.col("shares") * pl.col("close"),
    )
    out = out.with_columns(
        log_cap=pl.col("cap").log(),
        turnover_1d=pl.col("volume") / pl.col("shares"),
    ).with_columns(
        turnover_21d=pl.col("turnover_1d").rolling_mean(21, min_samples=15).over("security_id"),
    )
    return out.select("security_id", "date", "shares", "shares_source", "cap", "log_cap", "turnover_1d", "turnover_21d")


def build(ctx: FeatureContext) -> pl.DataFrame:
    return size_features(ctx.panel, ctx.security_cik, ctx.shares)
