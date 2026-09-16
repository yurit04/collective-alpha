"""Statistical factor risk model: PCA on a trailing window of daily returns plus a diagonal
idiosyncratic term, with a shrinkage floor. Cheap enough to refit at every rebalance."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RiskModel:
    ids: list[str]
    loadings: np.ndarray  # (N x k) factor loadings
    factor_cov: np.ndarray  # (k x k)
    idio_var: np.ndarray  # (N,)
    market_beta: np.ndarray  # (N,) beta to the equal-weight universe return
    ann: float = 252.0

    def cov(self) -> np.ndarray:
        return self.loadings @ self.factor_cov @ self.loadings.T + np.diag(self.idio_var)

    def portfolio_var(self, w: np.ndarray) -> float:
        f = self.loadings.T @ w
        return float(f @ self.factor_cov @ f + np.sum(self.idio_var * w * w))

    def portfolio_vol(self, w: np.ndarray, annualise: bool = True) -> float:
        v = self.portfolio_var(w)
        return float(np.sqrt(max(v, 0.0) * (self.ann if annualise else 1.0)))

    def factor_exposures(self, w: np.ndarray) -> np.ndarray:
        return self.loadings.T @ w

    def beta(self, w: np.ndarray) -> float:
        return float(self.market_beta @ w)


def fit_risk_model(
    returns: np.ndarray, ids: list[str], k: int = 10, min_obs: int = 60, shrink: float = 0.1
) -> RiskModel:
    """returns: (T x N) daily returns with NaN for missing; names with fewer than min_obs valid
    observations get the cross-sectional average variance and zero loadings."""
    T, N = returns.shape
    X = returns.copy()
    valid = ~np.isnan(X)
    n_obs = valid.sum(axis=0)
    ok = n_obs >= min_obs
    # demean and fill missing with 0 (mean) for the eigen-decomposition. Columns and rows can be
    # entirely missing (a name that never traded in the window), so divide only where there is data
    # rather than letting nanmean warn on an empty slice.
    filled = np.where(valid, X, 0.0)
    col_n = valid.sum(axis=0)
    mu = np.divide(filled.sum(axis=0), col_n, out=np.zeros(N), where=col_n > 0)
    Xc = np.where(valid, X - mu, 0.0)
    row_n = valid.sum(axis=1)
    mkt = np.divide(filled.sum(axis=1), row_n, out=np.zeros(T), where=row_n > 0)  # equal-weight market
    # sample covariance on the filled matrix, scaled by pairwise-ish effective count
    S = (Xc.T @ Xc) / max(T - 1, 1)
    avg_var = float(np.mean(np.diag(S)[ok])) if ok.any() else float(np.mean(np.diag(S)))
    k = int(min(k, max(1, ok.sum() - 1), T - 1))
    # PCA via eigh on the covariance of "ok" names; others get zero loadings
    S_ok = S[np.ix_(ok, ok)]
    vals, vecs = np.linalg.eigh(S_ok)
    order = np.argsort(vals)[::-1][:k]
    lam = np.clip(vals[order], 0.0, None)
    V = vecs[:, order]  # (n_ok x k) orthonormal
    loadings = np.zeros((N, k))
    loadings[ok] = V * np.sqrt(lam)  # so that loadings @ loadings.T reproduces the factor part
    factor_cov = np.eye(k)
    sys_var = np.sum(loadings**2, axis=1)
    idio = np.clip(np.diag(S) - sys_var, 0.0, None)
    idio = (1 - shrink) * idio + shrink * avg_var  # shrink towards the average
    idio[~ok] = avg_var
    # market beta by OLS on the equal-weight market
    mk = mkt - mkt.mean()
    denom = float(mk @ mk) or 1.0
    beta = (Xc.T @ mk) / denom
    beta[~ok] = 1.0
    return RiskModel(ids=list(ids), loadings=loadings, factor_cov=factor_cov, idio_var=idio, market_beta=beta)
