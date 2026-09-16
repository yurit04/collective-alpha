"""Effective-spread estimators from open, high, low and close. No quote data is required.

Corwin and Schultz (2012) infer the spread from the high-low range of two consecutive periods: the
range of a single period reflects both volatility and the spread, while the range of the two pooled
periods reflects volatility over twice the time but the spread only once.

Abdi and Ranaldo (2017) compare the close with the mid-range of the current and next period: under a
random walk the covariance of those two deviations isolates the spread.

Both are applied here to consecutive *minute* bars within one session, which yields one estimate per
security per day. Both can return negative values on noisy samples; negatives are floored at zero
before averaging, as the original papers recommend. Estimates are of the *round-trip* spread as a
fraction of price, so a half-spread cost is half of this.
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl

K = 3 - 2 * math.sqrt(2)


def corwin_schultz_expr(high: str = "high", low: str = "low") -> pl.Expr:
    """Mean Corwin-Schultz spread over consecutive bar pairs, as a fraction of price."""
    lh, ll = pl.col(high).log(), pl.col(low).log()
    hl2 = (lh - ll).pow(2)
    beta = hl2 + hl2.shift(-1)
    h2 = pl.max_horizontal(lh, lh.shift(-1))
    l2 = pl.min_horizontal(ll, ll.shift(-1))
    gamma = (h2 - l2).pow(2)
    alpha = ((2 * beta).sqrt() - beta.sqrt()) / K - (gamma / K).sqrt()
    s = 2 * (alpha.exp() - 1) / (1 + alpha.exp())
    return s.clip(lower_bound=0.0).mean()


def abdi_ranaldo_expr(high: str = "high", low: str = "low", close: str = "close") -> pl.Expr:
    """Abdi-Ranaldo spread, as a fraction of price."""
    eta = (pl.col(high).log() + pl.col(low).log()) / 2
    c = pl.col(close).log()
    s2 = 4 * (c - eta) * (c - eta.shift(-1))
    return s2.mean().clip(lower_bound=0.0).sqrt()


def tick_size_expr(price: str = "close_reg") -> pl.Expr:
    """Minimum price increment: one cent at or above $1.00, one hundredth of a cent below."""
    return pl.when(pl.col(price) >= 1.0).then(0.01).otherwise(0.0001)


def spread_estimate_expr(ar: str = "spread_ar", price: str = "close_reg") -> pl.Expr:
    """Canonical round-trip spread estimate: Abdi-Ranaldo floored at one tick.

    The raw estimator is unbiased in the middle of the range but underestimates at both ends. For a
    very liquid, low-volatility name the estimated covariance often goes negative and floors at zero
    (Coca-Cola: 0.0 bp). For a cheap name it lands below the minimum price increment (Ford: 3.9 bp
    against a one-cent tick of 7.2 bp). A round trip cannot cost less than one tick, so that is the
    floor. With it, Ford comes out at the tick and Apple stays at the estimator, which is the right
    behaviour in both cases."""
    return pl.max_horizontal(pl.col(ar), tick_size_expr(price) / pl.col(price))


# ---------------------------------------------------------------- reference implementations
# Plain numpy versions used to check the polars expressions and to document the formulas.


def corwin_schultz(high: np.ndarray, low: np.ndarray) -> float:
    lh, ll = np.log(high), np.log(low)
    hl2 = (lh - ll) ** 2
    beta = hl2[:-1] + hl2[1:]
    h2 = np.maximum(lh[:-1], lh[1:])
    l2 = np.minimum(ll[:-1], ll[1:])
    gamma = (h2 - l2) ** 2
    alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / K - np.sqrt(gamma / K)
    s = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    return float(np.mean(np.clip(s, 0.0, None))) if s.size else float("nan")


def abdi_ranaldo(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> float:
    eta = (np.log(high) + np.log(low)) / 2
    c = np.log(close)
    s2 = 4 * (c[:-1] - eta[:-1]) * (c[:-1] - eta[1:])
    return float(np.sqrt(max(np.mean(s2), 0.0))) if s2.size else float("nan")


def simulate_bars(
    n_bars: int,
    spread: float,
    sigma: float = 0.001,
    trades_per_bar: int = 20,
    p0: float = 100.0,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bars from an efficient price random walk observed through a bid-ask bounce, for testing.
    `spread` is the round-trip spread as a fraction of price."""
    rng = np.random.default_rng(seed)
    n = n_bars * trades_per_bar
    eff = p0 * np.exp(np.cumsum(rng.normal(0, sigma / math.sqrt(trades_per_bar), n)))
    side = rng.choice([-1.0, 1.0], size=n)
    obs = eff * (1 + side * spread / 2)
    obs = obs.reshape(n_bars, trades_per_bar)
    return obs.max(axis=1), obs.min(axis=1), obs[:, -1]


# ---------------------------------------------------------------- validation


def spread_report(
    settings=None,
    universe: str = "all_common",
    start=None,
    end=None,
    tickers: tuple[str, ...] = ("SPY", "AAPL", "MSFT", "KO", "F", "GME", "PLUG"),
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Cross-sectional check with no quote data to validate against: estimated spread must fall as
    dollar volume and price rise. Returns (by dollar-volume decile, named examples)."""
    from collective_alpha.config import get_settings
    from collective_alpha.features.base import load_features
    from collective_alpha.intraday.build import load_intraday

    s = settings or get_settings()
    i = load_intraday(
        s, start, end, columns=["spread_cs", "spread_ar", "spread_est", "hl_range_mean", "n_bars"], universe=universe
    )
    f = load_features(["price"], s, start, end, universe=universe, columns=["adv_21d", "log_price"])
    j = i.join(f, on=["security_id", "date"], how="inner").filter(pl.col("spread_ar").is_not_null())
    if j.height == 0:
        return pl.DataFrame(), pl.DataFrame()
    j = j.with_columns(
        decile=((pl.col("adv_21d").rank().over("date") - 1) * 10 // pl.col("adv_21d").count().over("date") + 1).cast(
            pl.Int32
        ),
        tick_bp=(0.01 / pl.col("log_price").exp() * 1e4),
    )
    by_decile = (
        j.group_by("decile")
        .agg(
            est_bp=(pl.col("spread_est").median() * 1e4).round(1),
            ar_bp=(pl.col("spread_ar").median() * 1e4).round(1),
            cs_bp=(pl.col("spread_cs").median() * 1e4).round(1),
            tick_bp=pl.col("tick_bp").median().round(1),
            price=pl.col("log_price").median().exp().round(2),
            adv_musd=(pl.col("adv_21d").median() / 1e6).round(2),
            median_bars=pl.col("n_bars").median(),
            n=pl.len(),
        )
        .sort("decile")
    )
    from collective_alpha.universe.builder import load_master

    m = load_master(s).select("security_id", "ticker").unique()
    named = (
        j.join(m, on="security_id", how="left")
        .filter(pl.col("ticker").is_in(list(tickers)))
        .group_by("ticker")
        .agg(
            est_bp=(pl.col("spread_est").median() * 1e4).round(2),
            ar_bp=(pl.col("spread_ar").median() * 1e4).round(2),
            cs_bp=(pl.col("spread_cs").median() * 1e4).round(2),
            tick_bp=pl.col("tick_bp").median().round(2),
            price=pl.col("log_price").median().exp().round(2),
        )
        .sort("est_bp")
    )
    return by_decile, named
