"""Short interest (bi-monthly, published with a lag) and daily short-sale volume."""

from __future__ import annotations

import polars as pl

from collective_alpha.features.base import FeatureContext
from collective_alpha.panel.corporate_actions import map_events_to_security

SI_PUBLISH_LAG_DAYS = 10  # FINRA publishes ~8 business days after settlement; be conservative


def short_interest_features(
    si: pl.DataFrame, master: pl.DataFrame, keys: pl.DataFrame, shares: pl.DataFrame | None = None
) -> pl.DataFrame:
    """keys: (security_id, date[, shares]). Values become available `SI_PUBLISH_LAG_DAYS` after settlement."""
    ev = si.select(
        "ticker",
        pl.col("settlement_date").cast(pl.Utf8).str.to_date(strict=False).alias("date"),
        pl.col("short_interest").cast(pl.Float64).alias("si_shares"),
        pl.col("days_to_cover").cast(pl.Float64).alias("si_days_to_cover"),
        pl.col("avg_daily_volume").cast(pl.Float64).alias("si_adv"),
    ).filter(pl.col("date").is_not_null())
    ev = map_events_to_security(ev, master, grace_days=14)
    ev = (
        ev.sort(["security_id", "date"])
        .with_columns(si_change=pl.col("si_shares") / pl.col("si_shares").shift(1).over("security_id") - 1)
        .with_columns(avail=pl.col("date") + pl.duration(days=SI_PUBLISH_LAG_DAYS))
        .select(
            "security_id",
            "avail",
            pl.col("date").alias("si_settlement"),
            "si_shares",
            "si_days_to_cover",
            "si_adv",
            "si_change",
        )
        .sort(["security_id", "avail"])
        .unique(subset=["security_id", "avail"], keep="last", maintain_order=True)
    )
    out = keys.sort(["security_id", "date"]).join_asof(
        ev, left_on="date", right_on="avail", by="security_id", strategy="backward", check_sortedness=False
    )
    stale = (pl.col("date") - pl.col("si_settlement")).dt.total_days() > 45
    out = out.with_columns(
        [
            pl.when(stale).then(None).otherwise(pl.col(c)).alias(c)
            for c in ("si_shares", "si_days_to_cover", "si_adv", "si_change")
        ]
    ).drop(["avail", "si_settlement"])
    if "shares" in out.columns:
        out = out.with_columns(si_ratio=pl.col("si_shares") / pl.col("shares")).drop("shares")
    return out


def short_volume_features(sv: pl.DataFrame, master: pl.DataFrame, keys: pl.DataFrame) -> pl.DataFrame:
    ev = sv.select(
        "ticker",
        pl.col("date").cast(pl.Utf8).str.to_date(strict=False).alias("date"),
        (pl.col("short_volume").cast(pl.Float64) / pl.col("total_volume").cast(pl.Float64)).alias("sv_ratio"),
        pl.col("total_volume").cast(pl.Float64).alias("sv_total_volume"),
    ).filter(pl.col("date").is_not_null() & (pl.col("sv_total_volume") > 0))
    ev = (
        map_events_to_security(ev, master)
        .select("security_id", "date", "sv_ratio")
        .unique(subset=["security_id", "date"], keep="first")
    )
    out = keys.join(ev, on=["security_id", "date"], how="left").sort(["security_id", "date"])
    return out.with_columns(
        sv_ratio_5d=pl.col("sv_ratio").rolling_mean(5, min_samples=3).over("security_id"),
        sv_ratio_21d=pl.col("sv_ratio").rolling_mean(21, min_samples=15).over("security_id"),
    )


def build(ctx: FeatureContext) -> pl.DataFrame:
    from collective_alpha.features.size import size_features

    keys = ctx.panel.select("security_id", "date")
    sh = size_features(ctx.panel, ctx.security_cik, ctx.shares).select("security_id", "date", "shares")
    si = short_interest_features(ctx.short_interest, ctx.master, keys.join(sh, on=["security_id", "date"], how="left"))
    sv = short_volume_features(ctx.short_volume, ctx.master, keys)
    return si.join(sv, on=["security_id", "date"], how="left")
