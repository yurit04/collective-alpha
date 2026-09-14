"""Cross-sectional transforms turning features into signals.

All functions take a long frame (security_id, date, <value>) and operate within each date.
"""

from __future__ import annotations

import polars as pl


def winsorize(df: pl.DataFrame, col: str, lower: float = 0.01, upper: float = 0.99) -> pl.DataFrame:
    lo = pl.col(col).quantile(lower).over("date")
    hi = pl.col(col).quantile(upper).over("date")
    return df.with_columns(pl.col(col).clip(lo, hi).alias(col))


def cs_rank(df: pl.DataFrame, col: str, out: str | None = None) -> pl.DataFrame:
    """Percentile rank in (0, 1] within each date, nulls stay null."""
    out = out or f"{col}_rank"
    return df.with_columns(
        (pl.col(col).rank(method="average").over("date") / pl.col(col).count().over("date")).alias(out)
    )


def cs_zscore(df: pl.DataFrame, col: str, out: str | None = None, winsor: float | None = 0.01) -> pl.DataFrame:
    out = out or f"{col}_z"
    x = df if winsor is None else winsorize(df, col, winsor, 1 - winsor)
    z = (pl.col(col) - pl.col(col).mean().over("date")) / pl.col(col).std().over("date")
    return x.with_columns(z.alias(out))


def neutralize(df: pl.DataFrame, col: str, group: str, out: str | None = None) -> pl.DataFrame:
    """Demean within (date, group), e.g. a sector code; missing group -> demean within date only."""
    out = out or f"{col}_n"
    g = pl.col(group).fill_null("__none__")
    return df.with_columns((pl.col(col) - pl.col(col).mean().over(["date", g])).alias(out))


def combine(df: pl.DataFrame, weights: dict[str, float], out: str = "signal") -> pl.DataFrame:
    """Weighted sum of z-scored columns, ignoring nulls (renormalising weights per row)."""
    zs = []
    for c, w in weights.items():
        df = cs_zscore(df, c, f"__z_{c}", winsor=0.01)
        zs.append((f"__z_{c}", w))
    num = pl.sum_horizontal([pl.col(z).fill_null(0.0) * w for z, w in zs])
    den = pl.sum_horizontal([pl.col(z).is_not_null().cast(pl.Float64) * abs(w) for z, w in zs])
    df = df.with_columns(pl.when(den > 0).then(num / den).otherwise(None).alias(out))
    return df.drop([z for z, _ in zs])


def lag(df: pl.DataFrame, col: str, n: int = 1, out: str | None = None) -> pl.DataFrame:
    """Shift a value n sessions later within each security (use to trade a signal with a delay)."""
    out = out or f"{col}_lag{n}"
    return df.sort(["security_id", "date"]).with_columns(pl.col(col).shift(n).over("security_id").alias(out))
