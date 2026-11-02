"""Cross-sectional transforms: per date, never across dates (no look-ahead)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import polars as pl

from ._signals import SIGNALS

BY = "date"
Method = str | None  # "zscore", "rank" or None


def _zscore(e: pl.Expr, by: list[str]) -> pl.Expr:
    sd = e.std().over(by)
    return pl.when(sd > 0).then((e - e.mean().over(by)) / sd)


def _rank(e: pl.Expr, by: list[str]) -> pl.Expr:
    """Percentile rank in (0, 1): (rank - 0.5) / count, ties averaged."""
    return pl.when(e.is_not_null()).then((e.rank("average").over(by) - 0.5) / e.count().over(by))


def winsorize(df: pl.DataFrame, cols: Iterable[str], q: float, by: str = BY) -> pl.DataFrame:
    """Clip each column to its per-date [q, 1-q] quantiles."""
    return df.with_columns([pl.col(c).clip(pl.col(c).quantile(q).over(by), pl.col(c).quantile(1 - q).over(by))
                            .alias(c) for c in cols])


def neutralize(df: pl.DataFrame, cols: Iterable[str], on: Iterable[str], by: str = BY) -> pl.DataFrame:
    """Remove sector and/or size from each column, per date: with "sector", demean within the
    date's sectors (a missing sector is a group of its own); with "size", the residual of a
    regression on log issuer market cap (after the sector demeaning: the same as one regression
    on sector dummies and size)."""
    on = set(on)
    unknown = on - {"sector", "size"}
    if unknown:
        raise ValueError(f"neutralize on {sorted(unknown)}: use 'sector' and/or 'size'")
    cols = list(cols)
    out = df
    group = [by, "__sector"] if "sector" in on else [by]
    out = out.with_columns(pl.col("sector").fill_null("(none)").alias("__sector") if "sector" in on
                           else pl.lit(None).alias("__sector"))
    if "sector" in on:
        out = out.with_columns([(pl.col(c) - pl.col(c).mean().over(group)).alias(c) for c in cols])
    if "size" in on:
        x = pl.when(pl.col("issuer_market_cap") > 0).then(pl.col("issuer_market_cap").log())
        out = out.with_columns(x.alias("__x"))
        if "sector" in on:
            out = out.with_columns((pl.col("__x") - pl.col("__x").mean().over(group)).alias("__x"))
        exprs = []
        for c in cols:
            ok = pl.col(c).is_not_null() & pl.col("__x").is_not_null()
            y, xx = pl.when(ok).then(pl.col(c)), pl.when(ok).then(pl.col("__x"))
            cov = (y * xx).mean().over(by) - y.mean().over(by) * xx.mean().over(by)
            var = (xx * xx).mean().over(by) - xx.mean().over(by) ** 2
            beta = pl.when(var > 0).then(cov / var).otherwise(0.0)
            alpha = y.mean().over(by) - beta * xx.mean().over(by)
            exprs.append(pl.when(ok).then(pl.col(c) - alpha - beta * pl.col("__x")).alias(c))
        out = out.with_columns(exprs).drop("__x")
    return out.drop("__sector")


def transform(df: pl.DataFrame, cols: Iterable[str], method: Method = None, winsor: float | None = None,
              neutralize_on: Iterable[str] = (), by: str = BY) -> pl.DataFrame:
    """Winsorize, neutralize, then z-score or rank each column, per date."""
    cols = list(cols)
    if method not in (None, "zscore", "rank"):
        raise ValueError("method must be 'zscore', 'rank' or None")
    out = df
    if winsor:
        out = winsorize(out, cols, winsor, by)
    if neutralize_on:
        out = neutralize(out, cols, neutralize_on, by)
    if method == "zscore":
        out = out.with_columns([_zscore(pl.col(c), [by]).alias(c) for c in cols])
    elif method == "rank":
        out = out.with_columns([_rank(pl.col(c), [by]).alias(c) for c in cols])
    return out


def composite(df: pl.DataFrame, weights: Mapping[str, float] | Iterable[str], name: str = "composite",
              method: str = "zscore", winsor: float | None = 0.01, min_signals: int = 1,
              by: str = BY) -> pl.DataFrame:
    """Weighted mean of signals, each oriented by its `direction` (so higher is always better)
    and z-scored (or ranked) per date. A row uses the signals it has, reweighted; fewer than
    `min_signals` available gives null. Signals not in SIGNALS count with direction +1."""
    w = dict(weights) if isinstance(weights, Mapping) else {s: 1.0 for s in weights}
    if not w:
        raise ValueError("no signals given")
    if method not in ("zscore", "rank"):
        raise ValueError("method must be 'zscore' or 'rank'")
    tmp = {s: f"__c_{s}" for s in w}
    out = df.with_columns([(pl.col(s) * (SIGNALS[s].direction if s in SIGNALS else 1)).alias(t)
                           for s, t in tmp.items()])
    out = transform(out, tmp.values(), method=method, winsor=winsor, by=by)
    if method == "rank":  # centre ranks so a missing signal does not pull toward 0.5
        out = out.with_columns([(pl.col(t) - 0.5).alias(t) for t in tmp.values()])
    num = pl.sum_horizontal([pl.col(t).fill_null(0) * wt for (s, wt), t in zip(w.items(), tmp.values(), strict=True)])
    den = pl.sum_horizontal([pl.when(pl.col(t).is_not_null()).then(abs(wt)).otherwise(0.0)
                             for (s, wt), t in zip(w.items(), tmp.values(), strict=True)])
    n = pl.sum_horizontal([pl.col(t).is_not_null().cast(pl.Int32) for t in tmp.values()])
    return (out.with_columns(pl.when((n >= min_signals) & (den > 0)).then(num / den).alias(name))
            .drop(list(tmp.values())))
