"""Calibration fits on synthetic stock-days with known answers."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from adaptation_matrix._calibrate import (
    Calibration,
    FittedSpread,
    clusters,
    compare,
    fit_spread,
    impact_table,
    tod_profile,
)


def _inputs(n: int = 60_000, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    days = [date(2020, 1, 6) + timedelta(days=7 * i) for i in range(60)] + [date(2021, 1, 4) + timedelta(days=7 * i)
                                                                          for i in range(60)]
    d = np.array(days)[rng.integers(0, len(days), n)]
    adv = 10 ** rng.uniform(5, 10.5, n)
    vol = 0.02 * np.exp(rng.normal(0, 0.4, n))
    truth = FittedSpread(adv_slope=-0.4, vol_slope=0.6, knots=[(1e6, -0.1), (1e7, 0.15)], levels={2020: 2.0, 2021: 3.0})
    mean = truth.predict(adv, vol, np.array([x.year for x in d]))
    eff = mean * rng.gamma(4.0, 0.25, n)                   # mean-one multiplicative noise
    return pl.DataFrame({"date": pl.Series(list(d), dtype=pl.Date), "security_id": [f"S{i}" for i in range(n)], "class": "stock", "adv": adv,
                         "vol": vol, "eff_bps": eff, "outside_share": 0.0, "n_matched": 100,
                         "dollar_volume": adv})


def test_spread_fit_recovers_slopes_and_year_levels():
    fit = fit_spread(_inputs())
    assert fit.adv_slope == pytest.approx(-0.4, abs=0.02)
    assert fit.vol_slope == pytest.approx(0.6, abs=0.03)
    assert [c for _, c in fit.knots] == pytest.approx([-0.1, 0.15], abs=0.04)
    assert fit.levels[2020] == pytest.approx(2.0, rel=0.05) and fit.levels[2021] == pytest.approx(3.0, rel=0.05)


def test_compare_reports_ratios_near_one_for_the_true_model():
    x = _inputs(seed=1)
    fit = fit_spread(x)
    t = compare(x, fit)
    mid = t.filter(pl.col("size").is_in(["1-10M", "10-100M", "100M-1B"]))
    assert mid["fit_dw_ratio"].to_list() == pytest.approx([1.0] * 3, abs=0.08)
    assert {"rule_bps", "rule_dw_ratio", "stock_days"} <= set(t.columns)


def test_rows_failing_the_quality_filters_are_left_out():
    x = _inputs(20_000)
    bad = x.head(500).with_columns(eff_bps=pl.lit(1e4), outside_share=pl.lit(0.5))
    few = x.slice(500, 500).with_columns(eff_bps=pl.lit(1e4), n_matched=pl.lit(3))
    etf = x.slice(1000, 500).with_columns(eff_bps=pl.lit(1e4), **{"class": pl.lit("etf")})
    clean = fit_spread(x.slice(1500))
    dirty = fit_spread(pl.concat([bad, few, etf, x.slice(1500)]))
    assert dirty.adv_slope == pytest.approx(clean.adv_slope) and dirty.levels == pytest.approx(clean.levels)


def test_impact_coefficient_is_recovered_from_flow():
    rng = np.random.default_rng(3)
    n = 40_000
    d = np.array([date(2025, 1, 2) + timedelta(days=(i // 2) % 50) for i in range(n)])   # both sizes each day
    f = rng.normal(0, 0.05, n)
    vol = np.full(n, 0.02)
    r = 0.8 * vol * np.sign(f) * np.sqrt(np.abs(f)) + rng.normal(0, 0.005, n)
    x = pl.DataFrame({"date": pl.Series(list(d), dtype=pl.Date), "class": "stock", "adv": np.where(np.arange(n) % 2 == 0, 5e8, 5e7), "vol": vol,
                      "n_matched": 100, "outside_share": 0.0, "mid_open1": 100.0, "mid_preclose": 100 * np.exp(r),
                      "buy_dv": np.where(f > 0, f, 0) * 5e7 + 1e6, "sell_dv": np.where(f < 0, -f, 0) * 5e7 + 1e6,
                      "pi_5m_bps": 1.0})
    # flow / ADV must be f for the 5e7 names; the 5e8 names see a tenth of it
    t = impact_table(x)
    assert t.filter(pl.col("size") == "10-100M")["impact_coef"].item() == pytest.approx(0.8, abs=0.1)


def test_tod_profile_averages_to_one_over_the_day_and_pairs_end_each_segment():
    seg = pl.DataFrame({"date": [date(2026, 1, 5)] * 3, "security_id": ["A"] * 3, "seg": [0, 1, 5],
                        "eff": [9.0, 3.0, 1.0], "qhs": [1.0] * 3, "mdv": [1.0, 1.0, 8.0]})
    inp = pl.DataFrame({"date": [date(2026, 1, 5)], "security_id": ["A"], "eff_bps": [(9 + 3 + 8) / 10],
                        "adv": [5e7], "class": ["stock"]})
    prof = tod_profile(seg, inp)
    assert prof["multiplier"].to_list() == pytest.approx([4.5, 1.5, 0.5])
    assert (prof["multiplier"] * pl.Series([1.0, 1.0, 8.0])).sum() / 10 == pytest.approx(1.0)
    cal = Calibration(spread=FittedSpread(0, 0, [], {}), validation=pl.DataFrame(), close=pl.DataFrame(),
                      impact=pl.DataFrame(), tod=prof)
    assert cal.tod_pairs()[:2] == ((1, 4.5), (5, 1.5))


def test_clusters_split_measured_days_into_close_runs():
    d = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 9), date(2026, 2, 10), date(2026, 3, 10)]
    assert clusters(d) == [d[:3], [d[3]], [d[4]]]
