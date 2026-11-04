"""Estimation: daily cross-sectional factor regressions, EWMA factor covariance, specific risk."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
from neural_processor import SECTORS

from ._styles import FACTORS, MARKET, STYLES

ROBUST = 5.0     # returns clipped at median +- ROBUST x robust std (1.4826 x MAD) for the fit
MIN_NAMES = 50   # a day with fewer usable names gets no factor returns


def design(sector: np.ndarray, styles: np.ndarray) -> np.ndarray:
    """n x k matrix in FACTORS order: market 1, sector dummies, styles."""
    n = len(sector)
    dummies = (sector[:, None] == np.array(SECTORS)[None, :]).astype(float)
    return np.hstack([np.ones((n, 1)), dummies, styles])


def cross_section(X: np.ndarray, r: np.ndarray, cap: np.ndarray) -> tuple[np.ndarray, float]:
    """One day's weighted least squares of returns on exposures X (FACTORS order), weights
    sqrt(cap), with the cap-weighted sector returns constrained to sum to 0 (so the market
    factor is the cap-weighted market, and sectors are relative to it). Sectors with no names
    get 0. Returns factor returns (k) and the weighted R^2."""
    k_sec = len(SECTORS)
    sec = X[:, 1:1 + k_sec]
    c = (sec * cap[:, None]).sum(0)
    active = np.flatnonzero(c > 0)
    base = active[-1]                       # its return is minus the others' cap-weighted sum
    rest = active[:-1]
    cols = [X[:, 0]]
    for s in rest:
        cols.append(sec[:, s] - c[s] / c[base] * sec[:, base])
    A = np.column_stack(cols + [X[:, 1 + k_sec:]])
    w = np.sqrt(cap)
    sw = np.sqrt(w)
    g, *_ = np.linalg.lstsq(A * sw[:, None], r * sw, rcond=None)
    f = np.zeros(X.shape[1])
    f[0] = g[0]
    f[1 + rest] = g[1:1 + len(rest)]
    f[1 + base] = -(c[rest] * g[1:1 + len(rest)]).sum() / c[base]
    f[1 + k_sec:] = g[1 + len(rest):]
    u = r - A @ g
    r2 = 1 - (w * u ** 2).sum() / (w * r ** 2).sum() if (w * r ** 2).sum() > 0 else np.nan
    return f, float(r2)


def robust_clip(r: np.ndarray) -> np.ndarray:
    med = np.median(r)
    s = 1.4826 * np.median(np.abs(r - med))
    return np.clip(r, med - ROBUST * s, med + ROBUST * s) if s > 0 else r


def regressions(exposures: pl.DataFrame, returns: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """exposures: build_exposures output (month ends). returns: security_id, date, ret, break,
    delisted. Each day's returns are regressed on the exposures of the latest month end before
    that day. Returns (factor_returns: date, one column per factor, r2, n) and (residuals: date,
    security_id, ret, resid). Delisting rows and data breaks are left out."""
    r = (returns.filter(~pl.col("break").fill_null(False) & ~pl.col("delisted").fill_null(False)
                        & pl.col("ret").is_not_null() & pl.col("ret").is_finite())
         .select("security_id", "date", "ret"))
    months = sorted(exposures["date"].unique().to_list())
    styles = list(STYLES)
    fr_rows, res = [], []
    for i, m in enumerate(months):
        nxt = months[i + 1] if i + 1 < len(months) else date.max
        ex = exposures.filter(pl.col("date") == m).sort("security_id")
        ids = ex["security_id"].to_numpy()
        X = design(ex["sector"].to_numpy(), ex.select(styles).to_numpy())
        cap = ex["issuer_market_cap"].to_numpy().astype(float)
        block = r.filter((pl.col("date") > m) & (pl.col("date") <= nxt) & pl.col("security_id").is_in(ids))
        if block.is_empty():
            continue
        wide = block.pivot(on="security_id", index="date", values="ret").sort("date")
        days = wide["date"].to_list()
        cols = [c for c in ids if c in wide.columns]
        pos = np.searchsorted(ids, cols)
        R = wide.select(cols).to_numpy()
        Xc, capc = X[pos], cap[pos]
        for d, row in zip(days, R):
            ok = ~np.isnan(row)
            if ok.sum() < MIN_NAMES:
                continue
            Xd = Xc[ok]
            f, r2 = cross_section(Xd, robust_clip(row[ok]), capc[ok])
            fr_rows.append((d, *f, r2, int(ok.sum())))
            res.append(pl.DataFrame({"date": [d] * int(ok.sum()), "security_id": np.array(cols)[ok],
                                     "ret": row[ok], "resid": row[ok] - Xd @ f}))
    schema = {"date": pl.Date, **{f: pl.Float64 for f in FACTORS}, "r2": pl.Float64, "n": pl.Int64}
    fr = pl.DataFrame(fr_rows, schema=schema, orient="row")
    resid = pl.concat(res) if res else pl.DataFrame(schema={"date": pl.Date, "security_id": pl.String,
                                                              "ret": pl.Float64, "resid": pl.Float64})
    return fr, resid


def ewma_weights(n: int, half_life: float) -> np.ndarray:
    """Weights of the last n observations, newest last, summing to 1."""
    lam = 0.5 ** (1 / half_life)
    w = lam ** np.arange(n - 1, -1, -1, dtype=float)
    return w / w.sum()


VRA_WARMUP = 63   # days of factor returns before the regime adjustment starts counting


def regime_scale(f: np.ndarray, hl_vol: float, hl_vra: float) -> float:
    """Volatility regime adjustment (as in Barra USE4): each day, every factor's return divided
    by its EWMA volatility up to the day before; the mean of those squared z-scores across
    factors, averaged with half-life hl_vra days. Above 1 when factors have recently moved more
    than forecast. Returns that multiplier for the covariance (1 with too little history)."""
    if len(f) <= VRA_WARMUP + 1:
        return 1.0
    lam = 0.5 ** (1 / hl_vol)
    var = np.mean(f[:VRA_WARMUP] ** 2, axis=0)
    b2 = []
    for row in f[VRA_WARMUP:]:
        ok = var > 0
        if ok.any():
            b2.append(np.mean(row[ok] ** 2 / var[ok]))
        var = lam * var + (1 - lam) * row ** 2
    if not b2:
        return 1.0
    w = ewma_weights(len(b2), hl_vra)
    return float(np.dot(w, b2))


def factor_cov(factor_returns: np.ndarray, hl_vol: float, hl_corr: float, hl_vra: float | None = None) -> np.ndarray:
    """Daily covariance from rows of factor returns (oldest first; NaN counts as 0): EWMA
    volatilities (half-life hl_vol days) times an EWMA correlation (hl_corr), scaled by the
    volatility regime adjustment (half-life hl_vra; None: off). Means are taken as 0, as is
    usual for daily returns."""
    f = np.nan_to_num(factor_returns)
    if len(f) == 0:
        return np.full((f.shape[1], f.shape[1]), np.nan)
    scale = regime_scale(f, hl_vol, hl_vra) if hl_vra else 1.0
    wc = ewma_weights(len(f), hl_corr)
    c = (f * wc[:, None]).T @ f
    sd = np.sqrt(np.diag(c))
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = c / np.outer(sd, sd)
    corr = np.nan_to_num(corr)
    np.fill_diagonal(corr, 1.0)
    wv = ewma_weights(len(f), hl_vol)
    vol = np.sqrt((f ** 2 * wv[:, None]).sum(0))
    return corr * np.outer(vol, vol) * scale


def specific_var(resid: pl.DataFrame, exposures: pl.DataFrame, half_life: float, min_obs: int,
                 shrink: float) -> pl.DataFrame:
    """Specific variance (daily) per security at each month end of `exposures`: an EWMA of its
    squared residuals up to that day, shrunk toward the cap-weighted mean of its size decile:
    (1 - shrink) x own + shrink x decile. Fewer than `min_obs` residuals: the decile's value.
    Returns date, security_id, spec_var, spec_var_raw, n_obs."""
    alpha = 1 - 0.5 ** (1 / half_life)
    e = (resid.sort("security_id", "date")
         .with_columns((pl.col("resid") ** 2).ewm_mean(alpha=alpha, adjust=True).over("security_id").alias("v"),
                       pl.int_range(1, pl.len() + 1).over("security_id").alias("n_obs")))
    months = exposures.select("date", "security_id", "issuer_market_cap").sort("date")
    own = months.join_asof(e.select("security_id", pl.col("date").alias("d"), "v", "n_obs").sort("d"),
                           left_on="date", right_on="d", by="security_id", strategy="backward",
                           check_sortedness=False)
    own = own.with_columns(pl.when(pl.col("n_obs") >= min_obs).then(pl.col("v")).otherwise(None).alias("raw"))
    own = own.with_columns((pl.col("issuer_market_cap").rank("ordinal").over("date") * 10
                            / pl.len().over("date")).ceil().clip(1, 10).alias("decile"))
    ok = pl.col("raw").is_not_null()
    w = pl.when(ok).then(pl.col("issuer_market_cap")).otherwise(None)
    own = own.with_columns(((pl.col("raw") * w).sum().over("date", "decile") / w.sum().over("date", "decile"))
                           .alias("bucket"))
    own = own.with_columns(pl.coalesce(pl.col("bucket"), (pl.col("raw") * w).sum().over("date") / w.sum().over("date"))
                           .alias("bucket"))
    spec = pl.when(ok).then((1 - shrink) * pl.col("raw") + shrink * pl.col("bucket")).otherwise(pl.col("bucket"))
    return own.select("date", "security_id", spec.alias("spec_var"), pl.col("raw").alias("spec_var_raw"),
                      pl.col("n_obs").fill_null(0).cast(pl.Int64)).sort("date", "security_id")


__all__ = ["MARKET", "cross_section", "design", "factor_cov", "regressions", "specific_var"]
