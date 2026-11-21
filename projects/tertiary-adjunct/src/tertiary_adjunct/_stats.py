"""Statistics on daily returns: Sharpe, information ratio, drawdowns, and the probabilistic and
deflated Sharpe ratios (Bailey and Lopez de Prado 2012, 2014)."""

from __future__ import annotations

import math

import numpy as np
import polars as pl
from scipy.stats import kurtosis, norm, skew

DAYS = 252
EULER = 0.5772156649015329


def _arr(r) -> np.ndarray:
    a = np.asarray(r.to_numpy() if isinstance(r, pl.Series) else r, dtype=float)
    return a[np.isfinite(a)]


def sharpe(r, annualize: bool = True) -> float:
    a = _arr(r)
    if len(a) < 2 or a.std(ddof=1) == 0:
        return float("nan")
    s = a.mean() / a.std(ddof=1)
    return float(s * math.sqrt(DAYS)) if annualize else float(s)


def ann_return(r) -> float:
    a = _arr(r)
    return float(np.prod(1 + a) ** (DAYS / len(a)) - 1) if len(a) else float("nan")


def max_drawdown(r) -> float:
    """Worst fall of the compounded value from its running peak (negative)."""
    a = _arr(r)
    v = np.cumprod(1 + a)
    return float((v / np.maximum.accumulate(v) - 1).min()) if len(a) else float("nan")


def relative_drawdown(r, bench) -> float:
    """Worst fall of (1 + r) compounded over (1 + bench) compounded, from its peak."""
    a, b = _arr(r), _arr(bench)
    v = np.cumprod(1 + a) / np.cumprod(1 + b)
    return float((v / np.maximum.accumulate(v) - 1).min()) if len(a) else float("nan")


def psr(r, benchmark_sr: float = 0.0) -> float:
    """Probabilistic Sharpe ratio: P(true per-period Sharpe > benchmark_sr) given the sample's
    length, skewness and kurtosis (per-period, not annualized, Sharpe ratios)."""
    a = _arr(r)
    n = len(a)
    if n < 3:
        return float("nan")
    sr = sharpe(a, annualize=False)
    g3, g4 = float(skew(a)), float(kurtosis(a, fisher=False))
    den = 1 - g3 * sr + (g4 - 1) / 4 * sr ** 2
    return float(norm.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(max(den, 1e-12))))


def expected_max_sharpe(trial_srs: list[float]) -> float:
    """Expected maximum of N per-period Sharpe ratios under the null of no skill, from their
    variance: sqrt(V) x ((1 - g) Z(1 - 1/N) + g Z(1 - 1/(N e)))."""
    n = len(trial_srs)
    if n < 2:
        return 0.0
    v = float(np.var(trial_srs, ddof=1))
    return math.sqrt(v) * ((1 - EULER) * norm.ppf(1 - 1 / n) + EULER * norm.ppf(1 - 1 / (n * math.e)))


def deflated_sharpe(r, trial_srs: list[float]) -> float:
    """Deflated Sharpe ratio: the PSR of `r` against the Sharpe the best of the trials would show
    by luck. trial_srs: per-period Sharpe ratios of every trial, this one included."""
    return psr(r, expected_max_sharpe(trial_srs))


def figures(daily: pl.DataFrame, market: pl.DataFrame | None = None, col: str = "net") -> dict:
    """The protocol's figures on a daily frame (date, net, ...): return, volatility, Sharpe,
    max drawdown; against `market` (date, ret) also active return, tracking error, information
    ratio and relative drawdown; turnover and costs when the frame has them."""
    d = daily.sort("date")
    out = {"start": d["date"].min(), "end": d["date"].max(), "years": round(d.height / DAYS, 2),
           "ann_return": ann_return(d[col]), "ann_vol": float(_arr(d[col]).std(ddof=1) * math.sqrt(DAYS)),
           "sharpe": sharpe(d[col]), "max_drawdown": max_drawdown(d[col])}
    if "turnover" in d.columns:
        out["turnover_per_year"] = float(d["turnover"].sum()) / (d.height / DAYS)
    if "gross" in d.columns:
        out["cost_drag"] = ann_return(d["gross"]) - out["ann_return"]
    if market is not None:
        j = d.select("date", col).join(market.select("date", "ret"), on="date").drop_nulls().sort("date")
        act = j[col] - j["ret"]
        out |= {"market_return": ann_return(j["ret"]), "active_return": ann_return(j[col]) - ann_return(j["ret"]),
                "tracking_error": float(_arr(act).std(ddof=1) * math.sqrt(DAYS)), "ir": sharpe(act),
                "relative_drawdown": relative_drawdown(j[col], j["ret"])}
    return out
