"""Offline tests: synthetic panels and returns generated from a known factor model."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from borg_queen import FACTORS, STYLES, RiskModel, RiskSettings, bias_stat, build_exposures
from borg_queen._estimate import (
    cross_section,
    design,
    factor_cov,
    regime_scale,
    regressions,
    specific_var,
)
from borg_queen._styles import signals
from neural_processor import SECTORS

N = 150
MONTHS = [date(2020, m, 1) - timedelta(days=1) for m in range(2, 13)]   # Jan-Nov 2020 month ends


def weekdays(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5]


def make_panel(seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    ids = [f"S{i:03d}" for i in range(N)]
    sector = rng.choice(SECTORS[:6], N)
    cap = np.exp(rng.normal(23, 1, N))
    rows = []
    for m in MONTHS:
        d = {"date": [m] * N, "security_id": ids, "sector": sector, "issuer_market_cap": cap * np.exp(rng.normal(0, .05, N))}
        for s in signals():
            x = rng.normal(0, 1, N)
            d[s] = np.exp(x) if s in ("volatility_3m", "turnover") else x
        d["size"] = np.log(d["issuer_market_cap"])
        d["roe"] = np.where(rng.random(N) < 0.1, np.nan, d["roe"])        # some missing (NaN, as from a ratio)
        rows.append(pl.DataFrame(d))
    return pl.concat(rows)


class FakeProcessor:
    def __init__(self, panel):
        self.p = panel

    def panel(self, names, start=None, end=None, top=None, **_):
        return self.p


class FakeEngine:
    def __init__(self, returns):
        self.r = returns

    def returns(self, ids, start=None, end=None, delisting="shumway"):
        return self.r.filter(pl.col("security_id").is_in(ids))

    def market(self, start, end=None, ticker="SPY"):
        return self.r.group_by("date").agg(pl.col("ret").mean()).sort("date")


def make_returns(ex: pl.DataFrame, seed: int = 1, spec_sd: float = 0.01):
    """Daily returns from the exposures of the previous month end, known factor returns."""
    rng = np.random.default_rng(seed)
    fsd = np.r_[0.01, np.full(len(SECTORS), 0.004), np.full(len(STYLES), 0.003)]
    rows, true = [], []
    months = sorted(ex["date"].unique().to_list())
    for i, m in enumerate(months[:-1]):
        e = ex.filter(pl.col("date") == m).sort("security_id")
        X = design(e["sector"].to_numpy(), e.select(list(STYLES)).to_numpy())
        cap = e["issuer_market_cap"].to_numpy()
        for d in weekdays(m + timedelta(days=1), months[i + 1]):
            f = rng.normal(0, fsd)
            # impose the sector constraint on the truth so it is identifiable
            sec = X[:, 1:1 + len(SECTORS)]
            c = (sec * cap[:, None]).sum(0)
            act = np.flatnonzero(c > 0)
            f[1 + np.setdiff1d(np.arange(len(SECTORS)), act)] = 0
            f[1 + act[-1]] = -(c[act[:-1]] * f[1 + act[:-1]]).sum() / c[act[-1]]
            r = X @ f + rng.normal(0, spec_sd, len(e))
            true.append((d, *f))
            rows.append(pl.DataFrame({"security_id": e["security_id"], "date": [d] * len(e), "ret": r,
                                      "break": [False] * len(e), "delisted": [False] * len(e)}))
    return pl.concat(rows), pl.DataFrame(true, schema=["date", *FACTORS], orient="row")


@pytest.fixture(scope="module")
def synth():
    panel = make_panel()
    ex = build_exposures(panel)
    rets, true = make_returns(ex)
    return panel, ex, rets, true


# ------------------------------------------------------------------ exposures
def test_exposures_cap_weighted_neutral_and_clipped(synth):
    _, ex, _, _ = synth
    assert ex.columns == ["date", "security_id", "sector", "issuer_market_cap", *STYLES]
    g = ex.group_by("date").agg([((pl.col(s) * pl.col("issuer_market_cap")).sum() / pl.col("issuer_market_cap").sum())
                                 .alias(s) for s in STYLES])
    assert g.select(list(STYLES)).to_numpy().__abs__().max() < 1e-9
    assert ex.select(list(STYLES)).to_numpy().__abs__().max() <= 3.0 + 3.1        # clipped, then re-centred
    assert ex.select(list(STYLES)).null_count().sum_horizontal().item() == 0       # missing -> 0
    assert set(ex["sector"]) <= set(SECTORS)


def test_exposures_need_signals():
    with pytest.raises(ValueError):
        build_exposures(pl.DataFrame({"date": [MONTHS[0]], "security_id": ["A"], "sector": ["Energy"],
                                      "issuer_market_cap": [1.0]}))


def test_missing_sector_is_other():
    p = make_panel().with_columns(pl.lit(None, pl.String).alias("sector"))
    assert set(build_exposures(p)["sector"]) == {"Other"}


# ------------------------------------------------------------------ regressions
def test_cross_section_recovers_factor_returns_and_constraint():
    rng = np.random.default_rng(3)
    n = 400
    sector = rng.choice(SECTORS[:5], n)
    X = design(sector, rng.normal(0, 1, (n, len(STYLES))))
    cap = np.exp(rng.normal(22, 1, n))
    c = (X[:, 1:13] * cap[:, None]).sum(0)
    f = rng.normal(0, 0.01, len(FACTORS))
    f[1 + np.arange(5, 12)] = 0
    f[5] = -(c[:4] * f[1:5]).sum() / c[4]
    est, r2 = cross_section(X, X @ f, cap)
    assert np.allclose(est, f, atol=1e-10) and r2 == pytest.approx(1.0)
    assert abs((c * est[1:13]).sum()) < 1e-12                       # cap-weighted sectors sum to 0
    assert np.all(est[1 + np.arange(5, 12)] == 0)                   # empty sectors get 0


def test_regressions_use_previous_month_exposures(synth):
    _, ex, rets, true = synth
    fr, resid = regressions(ex, rets)
    j = fr.join(true, on="date", suffix="_t")
    for f in ["market", *STYLES]:                     # 150 names, specific noise 3x a style's
        assert np.corrcoef(j[f], j[f"{f}_t"])[0, 1] > 0.9
    assert fr["date"].min() > MONTHS[0]
    assert resid.height == rets.height and resid["resid"].std() == pytest.approx(0.01, rel=0.15)


def test_breaks_and_delistings_left_out(synth):
    _, ex, rets, _ = synth
    first = rets["date"].min()
    bad = rets.with_columns(pl.when((pl.col("security_id") == "S000") & (pl.col("date") == first))
                            .then(pl.lit(True)).otherwise(pl.col("break")).alias("break"))
    _, resid = regressions(ex, bad)
    assert resid.filter((pl.col("security_id") == "S000") & (pl.col("date") == first)).is_empty()


# ------------------------------------------------------------------ covariance and specific risk
def test_factor_cov_long_half_life_is_sample_second_moment():
    rng = np.random.default_rng(5)
    f = rng.normal(0, 0.01, (2000, 3)) @ np.array([[1, .5, 0], [0, 1, .3], [0, 0, 1]])
    c = factor_cov(f, 1e9, 1e9)
    assert np.allclose(c, f.T @ f / len(f), rtol=1e-6)


def test_regime_scale_rises_after_a_volatility_jump():
    rng = np.random.default_rng(6)
    calm = rng.normal(0, 0.01, (500, 4))
    assert regime_scale(calm, 84, 42) == pytest.approx(1.0, abs=0.15)
    stormy = np.vstack([calm, rng.normal(0, 0.03, (40, 4))])
    assert regime_scale(stormy, 84, 42) > 2
    assert np.allclose(factor_cov(stormy, 84, 252, 42), factor_cov(stormy, 84, 252) * regime_scale(stormy, 84, 42))


def test_specific_var_shrinks_and_falls_back():
    days = weekdays(date(2020, 1, 1), date(2020, 6, 30))
    ids = [f"S{i}" for i in range(20)]
    rng = np.random.default_rng(7)
    sd = {s: 0.01 * (1 + i % 3) for i, s in enumerate(ids)}
    resid = pl.DataFrame([(d, s, rng.normal(0, sd[s])) for d in days for s in ids if not (s == "S0" and d < days[-5])],
                         schema=["date", "security_id", "resid"], orient="row")
    ex = pl.DataFrame({"date": [date(2020, 6, 30)] * 20, "security_id": ids,
                       "issuer_market_cap": np.linspace(1, 20, 20)})
    sp = specific_var(resid, ex, 1e9, 42, 0.3)
    s1 = sp.filter(pl.col("security_id") == "S1").row(0, named=True)
    assert s1["spec_var_raw"] == pytest.approx(sd["S1"] ** 2, rel=0.25)
    assert s1["spec_var"] != s1["spec_var_raw"]
    s0 = sp.filter(pl.col("security_id") == "S0").row(0, named=True)
    assert s0["spec_var_raw"] is None and s0["spec_var"] > 0       # too few residuals: decile value


# ------------------------------------------------------------------ RiskModel end to end
@pytest.fixture(scope="module")
def model(synth, tmp_path_factory):
    panel, _, rets, _ = synth
    rm = RiskModel(tmp_path_factory.mktemp("bq"), processor=FakeProcessor(panel), engine=FakeEngine(rets))
    info = rm.build("2020-01-01", settings=RiskSettings(top=N, min_obs=10, hl_vra=None))
    assert info["days"] > 150 and (rm.root / "build.json").exists()
    return rm


def test_reads(model):
    assert model.status()["settings"]["top"] == N
    assert model.exposures("2020-03-15")["date"].unique().to_list() == [date(2020, 2, 29)]
    cov = model.factor_cov("2020-06-30")
    assert cov.columns == ["factor", *FACTORS] and cov.height == len(FACTORS)
    assert model.specific_var("2020-06-30")["spec_var"].min() > 0
    with pytest.raises(ValueError):
        model.exposures("2019-12-31")


def test_predict_matches_manual_formula(model):
    m = date(2020, 8, 31)
    ex = model.exposures(m).sort("security_id")
    w = ex.head(20).select("security_id", pl.lit(0.05).alias("weight"))
    p = model.predict(w, on=m).row(0, named=True)
    X = design(ex["sector"].to_numpy(), ex.select(list(STYLES)).to_numpy())[:20]
    F = model.factor_cov(m).select(FACTORS).to_numpy()
    sv = model.specific_var(m).sort("security_id")["spec_var"].to_numpy()[:20]
    x = X.T @ np.full(20, 0.05)
    assert p["total_vol"] == pytest.approx(np.sqrt((x @ F @ x + (0.05 ** 2 * sv).sum()) * 252))
    assert p["covered"] == pytest.approx(1.0) and p["uncovered"] == 0
    c = model.contributions(w, m)
    assert c["contribution"].sum() == pytest.approx(1.0)
    outside = pl.concat([w, pl.DataFrame({"security_id": ["NOPE"], "weight": [0.5]})])
    assert model.predict(outside, on=m)["uncovered"].item() == pytest.approx(0.5)


def test_attribution_adds_up(model):
    ex = model.exposures().filter(pl.col("date") >= date(2020, 3, 31))
    w = ex.select("date", "security_id", (1 / pl.len().over("date")).alias("weight"))
    a = model.attribute(w)
    assert a.height > 100
    parts = a.select(pl.sum_horizontal(*FACTORS, "specific")).to_series()
    assert np.allclose(parts, a["total"], atol=1e-12)


def test_bias_near_one_on_model_data(model):
    ex = model.exposures().filter(pl.col("date") >= date(2020, 4, 30))
    rng = np.random.default_rng(9)
    rows = []
    for (d,), g in ex.group_by(["date"], maintain_order=True):
        pick = rng.choice(g["security_id"].to_numpy(), 40, replace=False)
        rows.append(pl.DataFrame({"date": [d] * 40, "security_id": pick, "weight": [0.05] * 20 + [-0.05] * 20}))
    b = model.bias(pl.concat(rows))
    assert b.columns == ["date", "forecast_vol", "realized_vol", "days", "z_sq_mean"]
    assert 0.75 < bias_stat(b) < 1.3
    assert bias_stat(b, 3).height == b.height - 2


def test_missing_model_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RiskModel(tmp_path).factor_returns()
