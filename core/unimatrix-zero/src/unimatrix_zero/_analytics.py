"""Performance statistics from a daily return series (no plotting)."""

from __future__ import annotations

import math

import polars as pl

DAYS = 252


def _ann(r: pl.Series) -> float | None:
    r = r.drop_nulls()
    if r.len() == 0:
        return None
    growth = float((1 + r).product())
    return growth ** (DAYS / r.len()) - 1 if growth > 0 else -1.0


def stats(r: pl.Series) -> dict:
    """Annualized return (geometric), volatility, Sharpe, t-stat, max drawdown."""
    r = r.drop_nulls()
    n = r.len()
    if n < 2:
        return {"ann_return": None, "ann_vol": None, "sharpe": None, "t_stat": None, "max_drawdown": None}
    mean, sd = float(r.mean()), float(r.std())
    nav = (1 + r).cum_prod()
    dd = float((nav / nav.cum_max() - 1).min())
    return {"ann_return": _ann(r), "ann_vol": sd * math.sqrt(DAYS),
            "sharpe": mean / sd * math.sqrt(DAYS) if sd > 0 else None,
            "t_stat": mean / sd * math.sqrt(n) if sd > 0 else None, "max_drawdown": dd}


def summary(daily: pl.DataFrame, market: pl.DataFrame | None = None, rebalances: int | None = None) -> pl.DataFrame:
    """One row per series (net, gross, long leg, short leg) with the statistics, turnover and
    cost drag; beta and alpha against `market` (date, ret) for the net series."""
    years = daily.height / DAYS
    rows = []
    for name, col in [("net", "net"), ("gross", "gross"), ("long leg", "long"), ("short leg", "short")]:
        if col in daily.columns:
            rows.append({"series": name, **stats(daily[col])})
    out = pl.DataFrame(rows)
    turnover = float(daily["turnover"].sum())
    extra = {
        "start": daily["date"].min(), "end": daily["date"].max(), "years": round(years, 2),
        "turnover_per_rebalance": turnover / rebalances if rebalances else None,
        "turnover_per_year": turnover / years if years else None,
        "cost_drag": (_ann(daily["gross"]) or 0) - (_ann(daily["net"]) or 0),
        "avg_gross_exposure": float(daily["gross_exposure"].mean()),
        "avg_net_exposure": float(daily["net_exposure"].mean()),
    }
    if market is not None and market.height:
        j = daily.select("date", "net").join(market.select("date", pl.col("ret").alias("mkt")), on="date").drop_nulls()
        if j.height > 20 and float(j["mkt"].var()) > 0:
            beta = float(j.select(pl.cov("net", "mkt")).item()) / float(j["mkt"].var())
            alpha = (float(j["net"].mean()) - beta * float(j["mkt"].mean())) * DAYS
            extra |= {"beta": beta, "alpha": alpha}
    # Portfolio-level figures go on the net row only.
    return out.with_columns([pl.when(pl.col("series") == "net").then(pl.lit(v)).alias(k) for k, v in extra.items()])


def by_year(daily: pl.DataFrame) -> pl.DataFrame:
    """Calendar-year compounded returns of each series, turnover and costs."""
    cols = [c for c in ("gross", "net", "long", "short") if c in daily.columns]
    return (daily.group_by(pl.col("date").dt.year().alias("year"))
            .agg(*[((1 + pl.col(c)).product() - 1).alias(c) for c in cols],
                 pl.col("turnover").sum(), pl.col("cost").sum(), pl.col("borrow").sum())
            .sort("year"))


def exposures(weights: pl.DataFrame) -> pl.DataFrame:
    """Per rebalance: names long and short, gross and net weight, the long-minus-short tilt in
    log market cap, and the net weight of each sector."""
    w = weights
    base = w.group_by("date").agg(
        (pl.col("weight") > 0).sum().alias("long_names"), (pl.col("weight") < 0).sum().alias("short_names"),
        pl.col("weight").abs().sum().alias("gross"), pl.col("weight").sum().alias("net"),
        *([(pl.col("weight") * pl.col("issuer_market_cap").log()).sum().alias("size_tilt")]
          if "issuer_market_cap" in w.columns else []))
    if "sector" in w.columns:
        sec = (w.with_columns(pl.col("sector").fill_null("(none)"))
               .group_by("date", "sector").agg(pl.col("weight").sum())
               .pivot(on="sector", index="date", values="weight").fill_null(0))
        base = base.join(sec, on="date", how="left")
    return base.sort("date")
