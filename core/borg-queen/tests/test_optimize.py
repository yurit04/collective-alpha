"""Optimizer and rebalance loop on a synthetic risk model (offline)."""

from datetime import date

import numpy as np
import polars as pl
import pytest
from borg_queen import (
    FACTORS,
    Constraints,
    Optimizer,
    Portfolio,
    RiskModel,
    RiskSettings,
    alpha_from_score,
)
from borg_queen._estimate import design
from borg_queen._styles import SECTORS, STYLES
from unimatrix_zero import NO_COSTS, CostModel

from .test_borg_queen import FakeEngine, FakeProcessor, N, build_exposures, make_panel, make_returns

M = date(2020, 8, 31)


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    panel = make_panel(seed=11)
    ex = build_exposures(panel)
    rets, _ = make_returns(ex, seed=12)
    rng = np.random.default_rng(13)
    ids = sorted(rets["security_id"].unique().to_list())
    adv = dict(zip(ids, np.exp(rng.normal(np.log(5e7), 1.0, len(ids)))))
    rets = rets.with_columns(pl.col("security_id").replace_strict(adv, return_dtype=pl.Float64).alias("adv"),
                             pl.lit(0.02).alias("vol"))
    rm = RiskModel(tmp_path_factory.mktemp("opt"), processor=FakeProcessor(panel), engine=FakeEngine(rets))
    rm.build("2020-01-01", settings=RiskSettings(top=N, min_obs=10, hl_vra=None))
    base = dict(zip(ids, rng.normal(0, 1, len(ids))))                  # persistent, like real signals
    score = panel.select("date", "security_id", "sector", "issuer_market_cap",
                         (pl.col("security_id").replace_strict(base, return_dtype=pl.Float64)
                          + pl.Series(rng.normal(0, 0.3, panel.height))).alias("score"))
    alpha = alpha_from_score(score, "score", rm)
    market = rets.filter(pl.col("date") <= M).group_by("security_id").agg(pl.all().sort_by("date").last())
    return rm, alpha, market, rets


def _at(alpha, d=M):
    return alpha.filter(pl.col("date") == d)


def test_alpha_is_ic_times_z_times_specific_vol(setup):
    rm, alpha, _, _ = setup
    a = _at(alpha).join(rm.specific_var(M).select("security_id", "spec_var"), on="security_id")
    ratio = a["alpha"] / (0.03 * (a["spec_var"] * 21).sqrt())
    assert ratio.std() == pytest.approx(1.0, rel=0.1) and abs(ratio.mean()) < 0.05


def test_constraints_hold(setup):
    rm, alpha, market, _ = setup
    c = Constraints(max_weight=0.03, sector_net=0.01, beta=0.05, styles=(("size", 0.1), ("value", 0.2)))
    sol = Optimizer(rm, CostModel(), c).solve(_at(alpha), M, None, market)
    w = sol.weights
    assert sol.info["status"] == "optimal" and sol.info["relaxed"] == "none"
    assert abs(w["weight"].sum()) < 1e-6 and w["weight"].abs().sum() <= 2 + 1e-6
    assert w["weight"].abs().max() <= 0.03 + 1e-6
    ex = rm.exposures(M).join(w, on="security_id")
    sec = ex.group_by("sector").agg(pl.col("weight").sum())
    assert sec["weight"].abs().max() <= 0.01 + 1e-6
    assert abs((ex["weight"] * ex["size"]).sum()) <= 0.1 + 1e-6
    assert abs((ex["weight"] * ex["value"]).sum()) <= 0.2 + 1e-6
    assert abs(sol.info["beta"]) <= 0.05 + 1e-6
    assert rm.predict(w, on=M)["total_vol"].item() <= 0.08 + 1e-4
    assert sol.info["vol"] == pytest.approx(rm.predict(w, on=M)["total_vol"].item(), rel=1e-4)


def test_no_costs_matches_closed_form(setup):
    """Mean-variance with only the budget (sum 0): w = (Sigma^-1 (a - mu 1)) / (2 lambda h)."""
    rm, alpha, _, _ = setup
    lam, hz = 50.0, 21
    c = Constraints(gross=100, target_vol=None, risk_aversion=lam, sector_net=None, beta=None, styles=(), max_weight=10)
    sol = Optimizer(rm, NO_COSTS, c).solve(_at(alpha), M)
    ex = rm.exposures(M).sort("security_id").join(rm.specific_var(M).select("security_id", "spec_var"), on="security_id")
    X = design(ex["sector"].to_numpy(), ex.select(list(STYLES)).to_numpy())
    F = rm.factor_cov(M).select(FACTORS).to_numpy()
    sigma = (X @ F @ X.T + np.diag(ex["spec_var"].to_numpy())) * hz
    a = ex.join(_at(alpha), on="security_id", how="left")["alpha"].fill_null(0).to_numpy()
    si = np.linalg.solve(sigma, np.c_[a, np.ones(len(a))])
    mu = si[:, 0].sum() / si[:, 1].sum()
    expect = (si[:, 0] - mu * si[:, 1]) / (2 * lam)
    got = ex.select("security_id").join(sol.weights, on="security_id", how="left")["weight"].fill_null(0).to_numpy()
    assert np.allclose(got, expect, atol=2e-4 * np.abs(expect).max() + 1e-6)


def test_higher_aum_trades_less(setup):
    rm, alpha, market, _ = setup
    turn = []
    for aum in (None, 1e8, 1e10):
        sol = Optimizer(rm, CostModel(aum=aum)).solve(_at(alpha), M, None, market)
        turn.append(sol.info["turnover"])
    assert turn[0] > turn[1] > turn[2]


def test_participation_limit(setup):
    rm, alpha, market, _ = setup
    aum = 1e10
    sol = Optimizer(rm, CostModel(aum=aum), Constraints(max_participation=0.01)).solve(_at(alpha), M, None, market)
    j = sol.weights.join(market.select("security_id", "adv"), on="security_id")
    assert (j["weight"].abs() * aum <= 0.01 * j["adv"] * (1 + 1e-4) + 1).all()


def test_costs_keep_holdings(setup):
    """With linear costs and a binding risk cap, a solution is also optimal from itself."""
    rm, alpha, market, _ = setup
    opt = Optimizer(rm, CostModel(), Constraints(target_vol=0.02))
    first = opt.solve(_at(alpha), M, None, market)
    assert first.info["vol"] == pytest.approx(0.02, rel=1e-3)
    again = opt.solve(_at(alpha), M, first.weights, market)
    assert again.info["turnover"] < 0.02 * first.info["turnover"]


def test_names_outside_universe_are_sold(setup):
    rm, alpha, market, _ = setup
    w0 = pl.DataFrame({"security_id": ["GONE"], "weight": [0.05]})
    sol = Optimizer(rm, CostModel()).solve(_at(alpha), M, w0, market)
    assert "GONE" not in sol.weights["security_id"].to_list() and sol.info["sold_outside"] == pytest.approx(0.05)


def test_infeasible_holds_current(setup):
    rm, alpha, market, _ = setup
    c = Constraints.long_only(max_weight=0.001)                 # 150 names x 0.1% cannot reach 100%
    w0 = pl.DataFrame({"security_id": ["S001"], "weight": [1.0]})
    sol = Optimizer(rm, CostModel(), c).solve(_at(alpha), M, w0, market)
    assert sol.info["status"] == "infeasible" and sol.weights["weight"].to_list() == [1.0]


def test_long_only(setup):
    rm, alpha, market, _ = setup
    sol = Optimizer(rm, CostModel(), Constraints.long_only(max_weight=0.05)).solve(_at(alpha), M, None, market)
    assert sol.weights["weight"].min() >= 0 and sol.weights["weight"].sum() == pytest.approx(1.0, abs=1e-6)


def test_market_needed_for_volume_spreads(setup):
    rm, alpha, _, _ = setup
    with pytest.raises(ValueError):
        Optimizer(rm, CostModel()).solve(_at(alpha), M)


def test_drift_matches_hand_calculation():
    """As in the engine: a delisting row makes the name cash; a halt (no bar on the execution
    date, trading again later) keeps it held."""
    d1, d2 = date(2020, 1, 2), date(2020, 1, 3)
    rets = pl.DataFrame({"security_id": ["A", "A", "B", "B", "C", "D"],
                         "date": [d1, d2, d1, d2, d1, d1],
                         "ret": [0.10, 0.0, -0.10, 0.0, 0.5, 0.2],
                         "delisted": [False, False, False, False, True, False]})
    held = pl.DataFrame({"security_id": ["A", "B", "C", "D"], "weight": [0.5, -0.5, 0.1, 0.1]})
    out, growth = Portfolio._drift(held, rets, date(2020, 1, 1), d2)
    pnl = 0.5 * 0.1 + (-0.5) * (-0.1) + 0.1 * 0.5 + 0.1 * 0.2
    assert growth == pytest.approx(1 + pnl)
    got = dict(out.iter_rows())
    assert got["A"] == pytest.approx(0.55 / growth) and got["B"] == pytest.approx(-0.45 / growth)
    assert "C" not in got                                       # delisted: cash
    assert got["D"] == pytest.approx(0.12 / growth)             # halted on d2: still held


def test_portfolio_build(setup):
    rm, alpha, _, rets = setup
    opt = Optimizer(rm, CostModel(), Constraints(target_vol=0.02))
    w, diag = Portfolio(opt, engine=FakeEngine(rets)).build(alpha, start="2020-03-31")
    assert w.columns == ["date", "security_id", "weight", "sector", "issuer_market_cap"]
    assert diag.height == w["date"].n_unique() >= 6 and (diag["status"] == "optimal").all()
    assert diag["turnover"][1:].mean() < diag["turnover"][0]     # later rebalances start from holdings
    assert (diag["nav"] > 0).all() and diag["nav"][0] == 1.0
    assert set(SECTORS) >= set(w["sector"].drop_nulls())


def test_row_order_of_joins_and_tables_does_not_matter(setup, monkeypatch):
    """Names are found by position (searchsorted) after joins, which keep no row order: the
    model and optimizer must sort after joining. Joins here return their rows reversed, as a
    polars release may, and the stored tables are shuffled; the answers must not change."""
    rm, alpha, market, _ = setup
    expected = rm.predict(rm.exposures(M).sort("security_id").head(20)
                          .select("security_id", pl.lit(0.05).alias("weight")), on=M).row(0)
    join = pl.DataFrame.join
    monkeypatch.setattr(pl.DataFrame, "join", lambda self, *a, **k: join(self, *a, **k).reverse())
    shuffled = RiskModel(rm.root, processor=rm.processor, engine=rm.engine)
    for name in ("exposures", "specific"):
        shuffled._tables[name] = rm._table(name).sample(fraction=1.0, shuffle=True, seed=5)
    w = rm.exposures(M).sort("security_id").head(20).select("security_id", pl.lit(0.05).alias("weight"))
    assert shuffled.predict(w, on=M).row(0) == pytest.approx(expected)
    ids = Optimizer(shuffled, CostModel())._inputs(M, _at(alpha), market, 1.0)[1]["security_id"]
    assert ids.is_sorted()
    c = Constraints.long_only(max_weight=0.001)                 # infeasible: holds w0 as given
    w0 = pl.DataFrame({"security_id": ["S077", "S003"], "weight": [0.6, 0.4]})
    sol = Optimizer(shuffled, CostModel(), c).solve(_at(alpha), M, w0, market)
    assert dict(sol.weights.iter_rows()) == pytest.approx({"S077": 0.6, "S003": 0.4})
