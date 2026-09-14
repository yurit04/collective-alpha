"""Portfolio construction from alpha scores.

`heuristic`: rank/z-score weights, sector-neutralised, capped, scaled to gross, then volatility-targeted
with the risk model. No solver; fast; the default.

`mvo`: maximise alpha'w - lambda * w'Sigma w - tau * |w - w_prev|_1 subject to net, gross, per-name cap,
sector bands, beta band and liquidity caps (cvxpy).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from collective_alpha.portfolio.risk import RiskModel


@dataclass(frozen=True)
class PortfolioConfig:
    method: str = "heuristic"  # heuristic | mvo
    gross: float = 2.0
    net: float = 0.0
    max_weight: float = 0.03
    n_names: int | None = None  # keep only the n/2 strongest longs and n/2 strongest shorts (after sector demeaning)
    sector_neutral: bool = True
    sector_band: float = 0.05  # mvo: |sector net exposure| <= band; heuristic: exact demeaning
    beta_band: float | None = 0.1  # mvo: |beta| <= band; heuristic: ignored
    vol_target: float | None = None  # annualised; scales gross down (never above `gross`)
    risk_aversion: float = 5.0  # mvo lambda
    turnover_penalty: float = 0.0  # mvo tau (per unit of |dw|), e.g. 0.001 ~ 10 bps
    max_adv_participation: float | None = None  # |w_i| <= k * adv_i / capital
    capital: float = 1_000_000.0


def _apply_cap_and_scale(w: np.ndarray, cap: float, gross: float, net: float) -> np.ndarray:
    """Iteratively cap and rescale so that gross and net are met with |w_i| <= cap."""
    for _ in range(50):
        long, short = w[w > 0].sum(), -w[w < 0].sum()
        tl, ts = (gross + net) / 2, (gross - net) / 2
        if long > 0:
            w[w > 0] *= tl / long
        if short > 0:
            w[w < 0] *= ts / short
        clipped = np.clip(w, -cap, cap)
        if np.allclose(clipped, w, rtol=0, atol=1e-10):
            return clipped
        w = clipped
    return w


def _keep_extremes(w: np.ndarray, n: int) -> np.ndarray:
    """Zero everything except the n//2 largest and n//2 smallest values."""
    half = max(n // 2, 1)
    order = np.argsort(w)
    keep = np.zeros_like(w, dtype=bool)
    keep[order[:half]] = True
    keep[order[-half:]] = True
    out = np.where(keep, w, 0.0)
    return out


def heuristic_weights(
    alpha: np.ndarray,
    sectors: np.ndarray | None,
    cfg: PortfolioConfig,
    risk: RiskModel | None = None,
    adv: np.ndarray | None = None,
) -> np.ndarray:
    """alpha: (N,) scores, NaN allowed (excluded). sectors: (N,) labels or None."""
    a = np.where(np.isnan(alpha), np.nan, alpha)
    ok = ~np.isnan(a)
    w = np.zeros_like(a)
    if ok.sum() < 2:
        return w
    # cross-sectional rank in [-0.5, 0.5]
    r = np.empty(ok.sum())
    r[np.argsort(a[ok])] = np.arange(ok.sum())
    x = r / (ok.sum() - 1) - 0.5
    x -= x.mean()
    if cfg.sector_neutral and sectors is not None:
        s = sectors[ok]
        for g in np.unique(s):
            m = s == g
            if m.sum() > 1:
                x[m] -= x[m].mean()
            else:
                x[m] = 0.0
    w[ok] = x
    if cfg.n_names and cfg.n_names < ok.sum():
        w = _keep_extremes(w, cfg.n_names)
        ok = w != 0
    if cfg.net:
        w[ok] += cfg.net / ok.sum() * (cfg.gross / 2)  # small tilt; rescaled below
    w = _apply_cap_and_scale(w, cfg.max_weight, cfg.gross, cfg.net)
    if cfg.sector_neutral and sectors is not None:
        # capping and per-leg scaling perturb sector sums; alternate demeaning and scaling
        for _ in range(5):
            for g in np.unique(sectors[ok]):
                m = ok & (sectors == g)
                if m.sum() > 1:
                    w[m] -= w[m].mean()
            w = _apply_cap_and_scale(w, cfg.max_weight, cfg.gross, cfg.net)
    if adv is not None and cfg.max_adv_participation:
        lim = cfg.max_adv_participation * np.nan_to_num(adv, nan=0.0) / cfg.capital
        w = np.sign(w) * np.minimum(np.abs(w), lim)
    if cfg.vol_target and risk is not None:
        vol = risk.portfolio_vol(w)
        if vol > 0:
            w = w * min(1.0, cfg.vol_target / vol)
    return w


def mvo_weights(
    alpha: np.ndarray,
    sectors: np.ndarray | None,
    cfg: PortfolioConfig,
    risk: RiskModel,
    prev: np.ndarray | None = None,
    adv: np.ndarray | None = None,
) -> np.ndarray:
    import cvxpy as cp

    N = alpha.shape[0]
    if cfg.n_names and np.sum(~np.isnan(alpha)) > cfg.n_names:
        a0 = np.where(np.isnan(alpha), 0.0, alpha - np.nanmean(alpha))
        kept = _keep_extremes(a0, cfg.n_names) != 0
        alpha = np.where(kept, alpha, np.nan)
    ok = ~np.isnan(alpha)
    a = np.nan_to_num(alpha, nan=0.0)
    # standardise alpha so risk_aversion has a stable meaning
    if ok.sum() > 1 and a[ok].std() > 0:
        a = (a - a[ok].mean()) / a[ok].std()
        a[~ok] = 0.0
    w = cp.Variable(N)
    f = risk.loadings.T @ w
    var = cp.sum_squares(np.linalg.cholesky(risk.factor_cov + 1e-12 * np.eye(risk.factor_cov.shape[0])).T @ f) + cp.sum(
        cp.multiply(risk.idio_var, cp.square(w))
    )
    obj = a @ w - cfg.risk_aversion * var
    if prev is not None and cfg.turnover_penalty > 0:
        obj = obj - cfg.turnover_penalty * cp.norm1(w - prev)
    cons = [cp.sum(w) == cfg.net, cp.norm1(w) <= cfg.gross, cp.abs(w) <= cfg.max_weight]
    if (~ok).any():
        cons.append(w[~ok] == 0)
    if cfg.sector_neutral and sectors is not None:
        for g in np.unique(sectors):
            m = (sectors == g).astype(float)
            cons += [m @ w <= cfg.sector_band, m @ w >= -cfg.sector_band]
    if cfg.beta_band is not None:
        cons += [risk.market_beta @ w <= cfg.beta_band, risk.market_beta @ w >= -cfg.beta_band]
    if adv is not None and cfg.max_adv_participation:
        lim = cfg.max_adv_participation * np.nan_to_num(adv, nan=0.0) / cfg.capital
        cons.append(cp.abs(w) <= np.maximum(lim, 0.0))
    prob = cp.Problem(cp.Maximize(obj), cons)
    try:
        prob.solve(solver=cp.CLARABEL)
    except Exception:  # noqa: BLE001
        prob.solve(solver=cp.SCS)
    if w.value is None:
        return heuristic_weights(alpha, sectors, cfg, risk, adv)
    out = np.asarray(w.value).ravel()
    out[np.abs(out) < 1e-6] = 0.0
    if cfg.vol_target:
        vol = risk.portfolio_vol(out)
        if vol > 0:
            out = out * min(1.0, cfg.vol_target / vol)
    return out


def build_weights(alpha, sectors, cfg, risk=None, prev=None, adv=None) -> np.ndarray:
    if cfg.method == "mvo":
        if risk is None:
            raise ValueError("mvo needs a risk model")
        return mvo_weights(alpha, sectors, cfg, risk, prev, adv)
    return heuristic_weights(alpha, sectors, cfg, risk, adv)
