import numpy as np
import polars as pl

from collective_alpha.portfolio.construct import PortfolioConfig, build_weights, heuristic_weights, mvo_weights
from collective_alpha.portfolio.risk import fit_risk_model
from collective_alpha.portfolio.sectors import ff12, sector_table


def test_ff12_mapping():
    assert ff12(7372) == "BusEq" and ff12(6021) == "Money" and ff12(2834) == "Hlth" and ff12(4911) == "Utils"
    assert ff12(None) == "Unknown" and ff12(9999) == "Other"
    det = pl.DataFrame({"ticker": ["A", "B"], "sic_code": ["7372", None]})
    master = pl.DataFrame({"security_id": ["S1", "S2"], "ticker": ["A", "B"], "valid_to": [1, 1]})
    st = sector_table(det, master).sort("security_id")
    assert st["sector"].to_list() == ["BusEq", "Unknown"]


def _synthetic_returns(T=1500, N=40, k=2, seed=0):
    rng = np.random.default_rng(seed)
    F = rng.normal(0, 0.01, (T, k))
    B = rng.normal(0, 1, (N, k))
    eps = rng.normal(0, 0.01, (T, N))
    return F @ B.T + eps


def test_risk_model_recovers_variance_and_beta():
    R = _synthetic_returns()
    ids = [f"S{i}" for i in range(R.shape[1])]
    rm = fit_risk_model(R, ids, k=2, shrink=0.0)
    sample = np.cov(R, rowvar=False)
    w = np.full(R.shape[1], 1 / R.shape[1])
    pv_model = rm.portfolio_var(w)
    pv_sample = float(w @ sample @ w)
    assert abs(pv_model / pv_sample - 1) < 0.15  # model var close to sample var for the EW portfolio
    assert abs(rm.beta(w) - 1.0) < 0.05  # EW portfolio has beta 1 to the EW market
    # a missing name gets average variance and zero loadings
    R2 = R.copy()
    R2[:, 0] = np.nan
    rm2 = fit_risk_model(R2, ids, k=2)
    assert rm2.loadings[0].sum() == 0 and rm2.idio_var[0] > 0 and rm2.market_beta[0] == 1.0


def test_heuristic_weights_constraints():
    rng = np.random.default_rng(1)
    N = 60
    alpha = rng.normal(size=N)
    alpha[:3] = np.nan
    sectors = np.array(["A", "B", "C"] * (N // 3))
    cfg = PortfolioConfig(gross=2.0, net=0.0, max_weight=0.06, sector_neutral=True)
    w = heuristic_weights(alpha, sectors, cfg)
    assert np.all(w[:3] == 0)
    assert abs(np.abs(w).sum() - 2.0) < 1e-4 and abs(w.sum()) < 1e-4
    assert np.abs(w).max() <= 0.06 + 1e-9
    for g in "ABC":
        assert abs(w[sectors == g].sum()) < 1e-3  # sector neutral (up to cap/scale rounding)
    # alpha ordering respected: highest alpha has the largest weight
    assert w[np.nanargmax(alpha)] > 0 and w[np.nanargmin(alpha)] < 0
    # vol targeting scales the book down
    R = _synthetic_returns(N=N)
    rm = fit_risk_model(R, [str(i) for i in range(N)], k=2)
    cfg_v = PortfolioConfig(gross=2.0, max_weight=0.06, vol_target=0.02)
    wv = heuristic_weights(alpha, sectors, cfg_v, rm)
    assert np.abs(wv).sum() < 2.0 and abs(rm.portfolio_vol(wv) - 0.02) < 1e-6


def test_mvo_weights_constraints():
    rng = np.random.default_rng(2)
    N = 30
    R = _synthetic_returns(N=N, seed=2)
    ids = [str(i) for i in range(N)]
    rm = fit_risk_model(R, ids, k=2)
    alpha = rng.normal(size=N)
    sectors = np.array(["A", "B"] * (N // 2))
    cfg = PortfolioConfig(
        method="mvo", gross=1.0, net=0.0, max_weight=0.1, sector_band=0.02, beta_band=0.05, risk_aversion=1.0
    )
    w = mvo_weights(alpha, sectors, cfg, rm)
    assert abs(w.sum()) < 1e-4 and np.abs(w).sum() <= 1.0 + 1e-4 and np.abs(w).max() <= 0.1 + 1e-4
    assert abs(w[sectors == "A"].sum()) <= 0.02 + 1e-4 and abs(rm.beta(w)) <= 0.05 + 1e-4
    assert float(alpha @ w) > 0  # tilts toward alpha
    # turnover penalty keeps the solution closer to the previous book
    prev = w.copy()
    alpha2 = alpha + rng.normal(size=N) * 0.5
    w_free = mvo_weights(alpha2, sectors, cfg, rm, prev)
    cfg_t = PortfolioConfig(
        method="mvo",
        gross=1.0,
        max_weight=0.1,
        sector_band=0.02,
        beta_band=0.05,
        risk_aversion=1.0,
        turnover_penalty=0.05,
    )
    w_pen = mvo_weights(alpha2, sectors, cfg_t, rm, prev)
    assert np.abs(w_pen - prev).sum() < np.abs(w_free - prev).sum()
    assert build_weights(alpha, sectors, PortfolioConfig(), rm).shape == (N,)


def test_n_names_concentration():
    rng = np.random.default_rng(3)
    alpha = rng.normal(size=200)
    cfg = PortfolioConfig(gross=2.0, max_weight=0.06, n_names=40, sector_neutral=False)
    w = heuristic_weights(alpha, None, cfg)
    assert (w != 0).sum() == 40 and (w > 0).sum() == 20 and (w < 0).sum() == 20
    assert abs(np.abs(w).sum() - 2.0) < 1e-4 and abs(w.sum()) < 1e-4
    assert set(np.argsort(alpha)[-20:]) == set(np.nonzero(w > 0)[0])
