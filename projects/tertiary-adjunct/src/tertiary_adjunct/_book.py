"""The two forms as borg-queen constraints, the long-only benchmark, whole shares at the
protocol's capital, and IBKR per-share commission on a backtest's trades."""

from __future__ import annotations

from typing import Any

import polars as pl

FORMS = ("long_short", "long_only")


def constraints(protocol: dict[str, Any], form: str):
    """borg_queen.Constraints for a form, from the protocol."""
    from borg_queen import Constraints

    if form == "long_short":
        c = protocol["long_short"]
        return Constraints(gross=c["gross"], target_vol=c["target_vol"], sector_net=c["sector_net"], beta=c["beta"],
                           styles=(("size", c["size"]),), max_weight=c["max_weight"],
                           horizon_days=protocol["alpha"]["horizon_days"])
    if form == "long_only":
        c = protocol["long_only"]
        return Constraints.long_only(max_weight=c["max_weight"], target_vol=c["tracking_error"],
                                     sector_net=c["sector_active"], beta=c["beta_active"],
                                     styles=(("size", c["size_active"]),), horizon_days=protocol["alpha"]["horizon_days"])
    raise ValueError(f"form must be one of {FORMS}")


def benchmark(panel: pl.DataFrame, top: int) -> pl.DataFrame:
    """Per date, the `top` largest names of the panel by issuer market cap, cap weighted:
    date, security_id, weight."""
    p = panel.filter(pl.col("issuer_market_cap").is_not_null() & (pl.col("issuer_market_cap") > 0))
    p = p.with_columns(pl.col("issuer_market_cap").rank("ordinal", descending=True).over("date").alias("__r"))
    p = p.filter(pl.col("__r") <= top)
    return p.select("date", "security_id",
                    (pl.col("issuer_market_cap") / pl.col("issuer_market_cap").sum().over("date")).alias("weight")
                    ).sort("date", "security_id")


def round_to_shares(weights: pl.DataFrame, prices: pl.DataFrame, capital: pl.DataFrame | float,
                    max_weight: float, min_position: float, min_shares_at_max: int,
                    long_only: bool = False) -> pl.DataFrame:
    """Targets as whole shares. weights: date, security_id, weight (+ other columns, kept).
    prices: security_id, date, raw_close (the signal date's close is used, or the last before
    it). capital: dollars, or date, capital per date (the book's value then). A name priced
    above `max_weight` x capital / `min_shares_at_max` is left out; a rounded position under
    `min_position` dollars is dropped. Long-only books are scaled down if rounding took them
    over 1. Adds shares and price; weight becomes the rounded value / capital."""
    w = weights.sort("date")
    px = prices.select("security_id", "date", pl.col("raw_close").alias("price")).drop_nulls().sort("date")
    w = w.join_asof(px, on="date", by="security_id", strategy="backward", tolerance="10d", check_sortedness=False)
    if isinstance(capital, pl.DataFrame):
        w = w.join(capital.select("date", "capital"), on="date", how="left")
    else:
        w = w.with_columns(pl.lit(float(capital)).alias("capital"))
    w = w.filter(pl.col("price").is_not_null() & (pl.col("price") > 0)
                 & (pl.col("price") <= max_weight * pl.col("capital") / min_shares_at_max))
    w = w.with_columns((pl.col("weight") * pl.col("capital") / pl.col("price")).round(0).alias("shares"))
    w = w.with_columns((pl.col("shares") * pl.col("price") / pl.col("capital")).alias("weight"))
    w = w.filter((pl.col("shares") != 0) & ((pl.col("weight") * pl.col("capital")).abs() >= min_position))
    if long_only:
        total = pl.col("weight").sum().over("date")
        w = w.with_columns(pl.when(total > 1).then(pl.col("weight") / total).otherwise(pl.col("weight")).alias("weight"))
    return w.drop("capital")


def ib_commission(trades: pl.DataFrame, daily: pl.DataFrame, prices: pl.DataFrame, capital: float,
                  per_share: float, minimum: float, max_pct: float) -> pl.DataFrame:
    """IBKR Pro tiered commission per order on a Backtest's trades: shares = |after - before| x
    book value / price, rounded (a trade of 0 shares sends no order); commission =
    min(max(per_share x shares, minimum), max_pct x value). Returns date, commission (dollars),
    commission_frac (of the book that day), orders."""
    nav = daily.select("date", (pl.col("nav") * capital).alias("book"))
    px = prices.select("security_id", "date", pl.col("raw_close").alias("price")).drop_nulls()
    t = (trades.join(nav, on="date", how="left").join(px, on=["security_id", "date"], how="left")
         .filter(pl.col("price").is_not_null() & (pl.col("price") > 0)))
    t = t.with_columns(((pl.col("after") - pl.col("before")).abs() * pl.col("book") / pl.col("price")).round(0)
                       .alias("shares"))
    t = t.filter(pl.col("shares") > 0).with_columns((pl.col("shares") * pl.col("price")).alias("value"))
    t = t.with_columns(pl.min_horizontal(pl.max_horizontal(pl.col("shares") * per_share, pl.lit(minimum)),
                                         pl.col("value") * max_pct).alias("commission"))
    out = t.group_by("date").agg(pl.col("commission").sum(), pl.len().alias("orders"), pl.col("book").first())
    return out.select("date", "commission", (pl.col("commission") / pl.col("book")).alias("commission_frac"),
                      "orders").sort("date")


def with_commission(daily: pl.DataFrame, commission: pl.DataFrame) -> pl.DataFrame:
    """The backtest's daily frame with the commission taken from net (and the engine's own
    commission, if any, left as it was): adds commission_frac; net is after it."""
    d = daily.join(commission.select("date", "commission_frac"), on="date", how="left").with_columns(
        pl.col("commission_frac").fill_null(0.0))
    return d.with_columns((pl.col("net") - pl.col("commission_frac")).alias("net"))
