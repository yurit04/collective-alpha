"""Portfolio builders: signal panel -> target weights per date (fractions of capital).

Every builder orients the signal by its direction (neural_processor.SIGNALS; +1 for anything
else, such as a composite), so "top" is always the side expected to do better. Long-short
portfolios have long weights summing to +1 and short weights to -1; long-only to +1. With
`sector_neutral`, each sector's long and short legs are the same size (its share of the
names), so every sector nets to zero. Output: date, security_id, weight, plus the panel's
ticker, sector and issuer_market_cap (for reports).
"""

from __future__ import annotations

from collections.abc import Iterable

import polars as pl
from neural_processor import SIGNALS

KEEP = ["ticker", "sector", "issuer_market_cap"]
Weighting = str  # "equal", "cap" or "signal"


def _oriented(panel: pl.DataFrame, col: str, direction: int | None) -> pl.DataFrame:
    sign = direction if direction is not None else (SIGNALS[col].direction if col in SIGNALS else 1)
    cols = ["date", "security_id", *[c for c in KEEP if c in panel.columns]]
    return (panel.select(*cols, (pl.col(col) * sign).alias("__x")).filter(pl.col("__x").is_not_null()
                                                                          & pl.col("__x").is_finite()))


def _raw(weighting: Weighting) -> pl.Expr:
    if weighting == "equal":
        return pl.lit(1.0)
    if weighting == "cap":
        return pl.when(pl.col("issuer_market_cap") > 0).then(pl.col("issuer_market_cap")).otherwise(0.0)
    if weighting == "signal":
        return pl.col("__x").abs()
    raise ValueError("weighting must be 'equal', 'cap' or 'signal'")


def cap_weights(w: pl.DataFrame, max_weight: float | None, by: Iterable[str] = ("date", "__leg")) -> pl.DataFrame:
    """Clip |weight| at max_weight within each leg and give the excess to the uncapped names
    (a few rounds); a leg too small to respect the cap keeps equal weights."""
    if not max_weight:
        return w
    by = list(by)
    # A leg with fewer than |total| / max_weight names cannot respect the cap: equal weights are
    # the closest it gets, and the leg keeps its total.
    n, total = pl.len().over(by), pl.col("weight").sum().over(by)
    w = w.with_columns((n * max_weight < total.abs() * (1 - 1e-9)).alias("__inf"))
    w = w.with_columns(pl.when("__inf").then(total / n).otherwise(pl.col("weight")).alias("weight"))
    for _ in range(20):
        capped = pl.col("weight").abs() >= max_weight * 0.999999
        excess = (pl.col("weight").abs() - max_weight).clip(lower_bound=0).sum().over(by)
        free = pl.when(~capped).then(pl.col("weight").abs()).otherwise(0.0).sum().over(by)
        w = w.with_columns(pl.when("__inf").then(pl.col("weight"))
                           .when(capped).then(pl.col("weight").sign() * max_weight)
                           .otherwise(pl.col("weight") * (1 + pl.when(free > 0).then(excess / free).otherwise(0.0)))
                           .alias("weight"))
        if w.select((~pl.col("__inf") & (pl.col("weight").abs() > max_weight * 1.0001)).any()).item() is False:
            break
    return w.drop("__inf")


def _finish(w: pl.DataFrame, max_weight: float | None, panel_cols: list[str]) -> pl.DataFrame:
    w = cap_weights(w, max_weight)
    keep = [c for c in KEEP if c in panel_cols]
    return w.filter(pl.col("weight") != 0).select("date", "security_id", "weight", *keep).sort("date", "security_id")


def quantile_weights(panel: pl.DataFrame, col: str, q: float = 0.2, long_short: bool = True,
                     weighting: Weighting = "equal", sector_neutral: bool = False, max_weight: float | None = None,
                     direction: int | None = None) -> pl.DataFrame:
    """Long the top `q` of names on each date (and short the bottom `q`)."""
    if not 0 < q <= 0.5:
        raise ValueError("q must be in (0, 0.5]")
    x = _oriented(panel, col, direction)
    grp = ["date", "sector"] if sector_neutral else ["date"]
    if sector_neutral:
        x = x.with_columns(pl.col("sector").fill_null("(none)"))
    pct = (pl.col("__x").rank("ordinal").over(grp) - 0.5) / pl.len().over(grp)
    x = x.with_columns(pct.alias("__p"))
    leg = pl.when(pl.col("__p") > 1 - q).then(1).when(long_short & (pl.col("__p") <= q)).then(-1).otherwise(0)
    x = x.with_columns(leg.alias("__leg")).filter(pl.col("__leg") != 0).with_columns(_raw(weighting).alias("__r"))
    if sector_neutral:  # each sector's legs sized by its share of all names that date
        share = pl.len().over(["date", "sector"]) / pl.len().over("date")
        x = x.with_columns((pl.col("__r") / pl.col("__r").sum().over(["date", "sector", "__leg"]) * share).alias("__r"))
    w = x.with_columns((pl.col("__leg") * pl.col("__r") / pl.col("__r").sum().over(["date", "__leg"]))
                       .fill_nan(0).alias("weight"))
    return _finish(w, max_weight, panel.columns)


def rank_weights(panel: pl.DataFrame, col: str, sector_neutral: bool = False, max_weight: float | None = None,
                 direction: int | None = None) -> pl.DataFrame:
    """Weights proportional to the centred rank (per date, or per date and sector), scaled to
    +1 long and -1 short."""
    x = _oriented(panel, col, direction)
    grp = ["date", "sector"] if sector_neutral else ["date"]
    if sector_neutral:
        x = x.with_columns(pl.col("sector").fill_null("(none)"))
    r = pl.col("__x").rank("average").over(grp)
    x = x.with_columns((r - r.mean().over(grp)).alias("__c"))
    if sector_neutral:
        share = pl.len().over(["date", "sector"]) / pl.len().over("date")
        x = x.with_columns((pl.col("__c") / pl.col("__c").abs().sum().over(["date", "sector"]) * 2 * share).alias("__c"))
    x = x.with_columns(pl.col("__c").sign().cast(pl.Int8).alias("__leg"))
    w = x.with_columns((pl.col("__c") / pl.col("__c").filter(pl.col("__c") > 0).sum().over("date")
                        ).alias("weight")).filter(pl.col("__leg") != 0)
    w = w.with_columns(pl.when(pl.col("__leg") > 0).then(pl.col("weight"))
                       .otherwise(pl.col("__c") / pl.col("__c").filter(pl.col("__c") < 0).sum().over("date").abs())
                       .alias("weight"))
    return _finish(w, max_weight, panel.columns)


def top_n_weights(panel: pl.DataFrame, col: str, n: int = 50, weighting: Weighting = "equal",
                  max_weight: float | None = None, direction: int | None = None) -> pl.DataFrame:
    """Long-only: the top `n` names on each date."""
    x = _oriented(panel, col, direction)
    x = (x.with_columns(pl.col("__x").rank("ordinal", descending=True).over("date").alias("__k"))
         .filter(pl.col("__k") <= n).with_columns(pl.lit(1).alias("__leg"), _raw(weighting).alias("__r")))
    w = x.with_columns((pl.col("__r") / pl.col("__r").sum().over("date")).alias("weight"))
    return _finish(w, max_weight, panel.columns)
