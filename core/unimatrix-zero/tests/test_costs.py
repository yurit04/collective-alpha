"""CostModel's fitted spread (decision 0017) and the closing-auction option."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest
from unimatrix_zero import SPREAD_FIT, CostModel, SpreadFit

FIT = SpreadFit(adv_slope=-0.4, vol_slope=0.5, knots=((1e6, -0.1), (1e7, 0.2)),
                levels=((2010, 2.0), (2011, 3.0), (2012, 4.0)))


def _bps(model: CostModel, rows: dict, at=None) -> list[float]:
    return pl.DataFrame(rows).with_columns((model.spread_cost(at) * 1e4).alias("x"))["x"].to_list()


def test_level_is_the_half_spread_at_100m_and_2pct_vol():
    m = CostModel(commission_bps=0.0, fit=FIT, spread_floor_bps=0.0)
    assert _bps(m, {"adv": [1e8], "vol": [0.02], "half_spread": [0.0]}, 2011) == pytest.approx([3.0])
    # above the last knot the slope is -0.4 + -0.1 + 0.2 = -0.3; below 1e6 it is -0.4
    above = _bps(m, {"adv": [1e9], "vol": [0.02], "half_spread": [0.0]}, 2011)
    assert above == pytest.approx([3.0 * 10 ** -0.3])
    lo, hi = _bps(m, {"adv": [1e4, 1e5], "vol": [0.02, 0.02], "half_spread": [0.0, 0.0]}, 2011)
    assert hi / lo == pytest.approx(10 ** -0.4)
    # volatility: (vol / 2%) ^ 0.5; a null volatility counts as 3%
    v = _bps(m, {"adv": [1e8, 1e8], "vol": [0.08, None], "half_spread": [0.0, 0.0]}, 2011)
    assert v == pytest.approx([6.0, 3.0 * 1.5 ** 0.5])


def test_years_outside_the_fit_take_the_nearest_level_and_none_the_latest():
    m = CostModel(commission_bps=0.0, fit=FIT, spread_floor_bps=0.0)
    row = {"adv": [1e8], "vol": [0.02], "half_spread": [0.0]}
    assert _bps(m, row, date(2005, 6, 1)) == pytest.approx([2.0])
    assert _bps(m, row, 2030) == pytest.approx([4.0])
    assert _bps(m, row) == pytest.approx([4.0])
    by_row = pl.DataFrame({"date": [date(2009, 1, 2), date(2011, 3, 1), date(2031, 1, 2)], "adv": [1e8] * 3,
                           "vol": [0.02] * 3, "half_spread": [0.0] * 3})
    got = by_row.with_columns((m.spread_cost(pl.col("date")) * 1e4).alias("x"))["x"].to_list()
    assert got == pytest.approx([2.0, 3.0, 4.0])


def test_fitted_spread_is_clipped_and_missing_volume_pays_the_cap():
    m = CostModel(commission_bps=1.0, fit=FIT, spread_floor_bps=0.5, spread_cap_bps=200.0)
    got = _bps(m, {"adv": [1e13, None, 0.0], "vol": [0.02, 0.02, 0.02], "half_spread": [0.0] * 3}, 2012)
    assert got == pytest.approx([1.5, 201.0, 201.0])


def test_auction_fills_pay_no_spread_but_commission_and_impact():
    m = CostModel(commission_bps=1.0, auction=True, aum=1e6)
    rows = pl.DataFrame({"adv": [1e6], "vol": [0.02], "half_spread": [0.01]})
    got = rows.with_columns((m.spread_cost() * 1e4).alias("s"), (m.impact_cost(pl.lit(0.01), 1.0) * 1e4).alias("i"))
    assert got["s"].to_list() == pytest.approx([1.0])
    assert got["i"].to_list() == pytest.approx([0.02 * (1e4 / 1e6) ** 0.5 * 1e4])


def test_default_is_the_shipped_fit_and_volume_rule_stays_available():
    assert CostModel().spread == "fitted" and CostModel().fit is SPREAD_FIT
    rows = {"adv": [1e8], "vol": [0.02], "half_spread": [0.0]}
    assert _bps(CostModel(commission_bps=0.0, spread="volume"), rows) == pytest.approx([3.0])
    latest = dict(SPREAD_FIT.levels)[max(dict(SPREAD_FIT.levels))]
    assert _bps(CostModel(commission_bps=0.0), rows) == pytest.approx([latest])
