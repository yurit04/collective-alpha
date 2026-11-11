"""Offline tests on a synthetic market with known returns."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from unimatrix_zero import (
    NO_COSTS,
    CostModel,
    Engine,
    daily_returns,
    quantile_weights,
    rank_weights,
    stats,
    top_n_weights,
)

D = date


def weekdays(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


CAL = weekdays(D(2023, 10, 2), D(2024, 6, 28))


def path(start, daily, n):
    out, p = [], start
    for _ in range(n):
        out.append(p)
        p *= 1 + daily
    return out


def series():
    """A +1%/day, B flat at 100, C falls to $0.50 and stops trading on 2024-03-01, D is
    succeeded by E on 2024-03-05 at the same price, F flat at 50."""
    n = len(CAL)
    cut = CAL.index(D(2024, 3, 1))
    cut_e = CAL.index(D(2024, 3, 5))
    s = {"A": (CAL, path(10.0, 0.01, n)), "B": (CAL, [100.0] * n), "F": (CAL, [50.0] * n),
         "C": (CAL[:cut + 1], path(5.0, -0.0185, cut + 1)),
         "D": (CAL[:cut + 1], path(20.0, 0.001, cut + 1))}
    d_last = s["D"][1][-1]
    s["E"] = (CAL[cut_e:], path(d_last, 0.001, n - cut_e))
    return s


class FakeStore:
    def __init__(self):
        rows = []
        for sid, (days, px) in series().items():
            for d, p in zip(days, px, strict=True):
                rows.append((sid, d, p, p * 1.001, p * 0.999, 1e6, 1.0, 1.0))
        self.b = pl.DataFrame(rows, schema={"security_id": pl.String, "date": pl.Date, "close": pl.Float64,
                                            "high": pl.Float64, "low": pl.Float64, "volume": pl.Float64,
                                            "split_factor": pl.Float64, "div_factor": pl.Float64}, orient="row")

    def bars(self, tickers=None, start=None, end=None, adjust=None, lazy=False, **_):
        b = self.b.filter(pl.col("date").is_between(start or D(1900, 1, 1), end or D(2100, 1, 1)))
        if tickers:
            b = b.filter(pl.col("security_id").is_in(list(tickers)))
        return b.lazy() if lazy else b

    def trading_days(self, start, end):
        return [d for d in CAL if start <= d <= end]


class FakeVinculum:
    def successors(self):
        return pl.DataFrame({"security_id": ["E"], "cik": ["2"], "predecessor_id": ["D"], "predecessor_cik": ["1"],
                             "link_date": [D(2024, 3, 5)], "kind": ["security"], "ticker": ["DD"],
                             "price_ratio": [1.0], "evidence": ["name"]})

    def short_interest(self, start=None, end=None):
        return pl.DataFrame({"security_id": ["B"], "settlement_date": [D(2023, 12, 15)],
                             "published_date": [D(2023, 12, 27)], "short_interest": [1],
                             "si_pct_float": [0.5], "si_pct_market_cap": [0.4], "days_to_cover": [1.0]})


@pytest.fixture
def eng():
    return Engine(vinculum=FakeVinculum(), store=FakeStore())


# ---------------------------------------------------------------- returns
def test_returns_delistings_and_successors(eng):
    r = eng.returns(["A", "B", "C", "E"], D(2023, 11, 1))
    c = r.filter(pl.col("security_id") == "C").sort("date")
    assert c["date"][-1] == D(2024, 3, 4) and c["delisted"][-1] and c["ret"][-1] == -0.30   # penny stock: Shumway
    assert r.filter(pl.col("security_id") == "D").is_empty()                                 # D continues as E
    e = r.filter(pl.col("security_id") == "E").sort("date")
    assert e["date"][0] < D(2024, 3, 5) and not e["delisted"].any()
    link = e.filter(pl.col("date") == D(2024, 3, 5))["ret"][0]
    assert link == pytest.approx(0.0)                       # raw close to raw close: E opened at D's last price
    a = r.filter(pl.col("security_id") == "A")
    assert a["ret"].drop_nulls().to_list() == pytest.approx([0.01] * (a.height - 0))
    assert a["adv"][-1] == pytest.approx(sum(path(10.0, 0.01, len(CAL))[-21:]) / 21 * 1e6)
    last = eng.returns(["C"], D(2023, 11, 1), delisting="last")
    assert last.filter(pl.col("delisted"))["ret"].to_list() == [0.0]
    with pytest.raises(ValueError):
        daily_returns(FakeStore().bars(lazy=True), None, CAL, "nope")


# ---------------------------------------------------------------- weights
def panel():
    return pl.DataFrame({"date": [D(2024, 1, 31)] * 8, "security_id": list("ABCDEFGH"),
                         "sector": ["X", "X", "X", "X", "Y", "Y", "Y", "Y"],
                         "issuer_market_cap": [4.0, 3, 2, 1, 4, 3, 2, 1],
                         "score": [8.0, 7, 6, 5, 4, 3, 2, 1], "accruals": [0.4, 0.3, 0.2, 0.1, 0.0, -0.1, -0.2, -0.3]})


def test_weight_builders():
    w = quantile_weights(panel(), "score", q=0.25)
    assert w.filter(pl.col("weight") > 0)["security_id"].to_list() == ["A", "B"]
    assert w["weight"].sum() == pytest.approx(0) and w.filter(pl.col("weight") > 0)["weight"].sum() == pytest.approx(1)
    a = quantile_weights(panel(), "accruals", q=0.25)          # direction -1: lowest accruals long
    assert set(a.filter(pl.col("weight") > 0)["security_id"]) == {"G", "H"}
    sn = quantile_weights(panel(), "score", q=0.25, sector_neutral=True)
    assert sn.group_by("sector").agg(pl.col("weight").sum())["weight"].to_list() == pytest.approx([0, 0])
    assert set(sn.filter(pl.col("weight") > 0)["security_id"]) == {"A", "E"}
    cw = quantile_weights(panel(), "score", q=0.25, weighting="cap")
    assert cw.filter(pl.col("security_id") == "A")["weight"][0] == pytest.approx(4 / 7)
    rw = rank_weights(panel(), "score")
    assert rw.filter(pl.col("weight") > 0)["weight"].sum() == pytest.approx(1) and rw["weight"].sum() == pytest.approx(0)
    assert rw.filter(pl.col("security_id") == "A")["weight"][0] > rw.filter(pl.col("security_id") == "B")["weight"][0]
    t = top_n_weights(panel(), "score", n=4, max_weight=0.3, weighting="cap")
    assert t["weight"].sum() == pytest.approx(1) and t["weight"].max() <= 0.3 + 1e-9


def test_cap_too_tight_for_the_leg_keeps_equal_weights():
    """4 names cannot each stay under 10% and sum to 1: equal weights, never a shrunken leg."""
    t = top_n_weights(panel(), "score", n=4, max_weight=0.1, weighting="cap")
    assert t["weight"].sum() == pytest.approx(1) and t["weight"].to_list() == pytest.approx([0.25] * 4)
    ls = quantile_weights(panel(), "score", q=0.25, weighting="cap", max_weight=0.3)   # 2 names a leg
    assert ls.filter(pl.col("weight") > 0)["weight"].to_list() == pytest.approx([0.5, 0.5])
    assert ls.filter(pl.col("weight") < 0)["weight"].to_list() == pytest.approx([-0.5, -0.5])


# ---------------------------------------------------------------- engine
def weights(rows):
    return pl.DataFrame(rows, schema={"date": pl.Date, "security_id": pl.String, "weight": pl.Float64}, orient="row")


def test_engine_arithmetic_and_lag(eng):
    w = weights([(D(2024, 1, 31), "A", 1.0), (D(2024, 1, 31), "B", -1.0)])
    bt = eng.run(w, costs=NO_COSTS, lag=0, market=None)
    d = bt.daily
    assert d["date"][0] == D(2024, 1, 31)                    # trades at that close, no return that day
    assert d["net"][0] == 0.0 and d["turnover"][0] == pytest.approx(2.0)
    # Long A (+1%/day), short B (flat): NAV grows by w_A(t) * 1% where w_A drifts up with NAV.
    nav = (1 + d["net"]).cum_prod()
    a_val = [1.01 ** k for k in range(d.height)]
    assert nav.to_list() == pytest.approx([a_val[k] - 1 + 1 for k in range(d.height)])
    bt1 = eng.run(w, costs=NO_COSTS, lag=1, market=None)
    assert bt1.daily["date"][0] == D(2024, 2, 1)             # executes the next trading day
    assert bt1.weights["signal_date"][0] == D(2024, 1, 31)


def test_signal_on_a_non_trading_day_counts_from_its_last_close(eng):
    cal = weekdays(D(2024, 1, 1), D(2024, 1, 12))
    sat = D(2024, 1, 6)
    assert Engine.execution_dates([sat], cal, 0) == {sat: D(2024, 1, 5)}       # known at Friday's close
    assert Engine.execution_dates([sat], cal, 1) == {sat: D(2024, 1, 8)}       # trades Monday, not Tuesday
    assert Engine.execution_dates([D(2023, 12, 30)], cal, 1) == {}            # before the calendar
    assert Engine.execution_dates([D(2024, 1, 12)], cal, 1) == {}             # past its end
    # The engine itself: a Saturday signal with the default lag trades on the Monday.
    w = weights([(sat, "A", 1.0), (sat, "B", -1.0)])
    bt = eng.run(w, costs=NO_COSTS, lag=1, market=None)
    assert bt.daily["date"][0] == D(2024, 1, 8) and bt.weights["signal_date"][0] == sat


def test_rebalance_turnover_costs_and_borrow(eng):
    w = weights([(D(2024, 1, 31), "A", 1.0), (D(2024, 1, 31), "B", -1.0),
                 (D(2024, 2, 29), "A", 1.0), (D(2024, 2, 29), "B", -1.0)])
    bt = eng.run(w, costs=NO_COSTS, lag=0, market=None)
    n = len([d for d in CAL if D(2024, 1, 31) < d <= D(2024, 2, 29)])
    grow = 1.01 ** n
    drift_a = grow / (1 + grow - 1)                          # A's value over NAV before the trade
    reb = bt.daily.filter(pl.col("date") == D(2024, 2, 29)).row(0, named=True)
    assert reb["turnover"] == pytest.approx(abs(1 - drift_a) + abs(-1 - (-1 / grow)))
    fixed = CostModel(commission_bps=1, spread=4, borrow_bps=0, htb_bps=0)
    bc = eng.run(w, costs=fixed, lag=0, market=None)
    assert bc.daily["cost"][0] == pytest.approx(2.0 * 5e-4)  # build-up: 2 x (4 + 1) bp
    assert bc.daily.filter(pl.col("date") == D(2024, 2, 29))["cost"][0] == pytest.approx(reb["turnover"] * 5e-4)
    # Borrow: B is hard to borrow (short interest 50% of float) -> 300 bp a year on short value.
    bb = eng.run(w, costs=CostModel(commission_bps=0, spread=0, spread_floor_bps=0), lag=0, market=None)
    first = bb.daily.filter(pl.col("date") > D(2024, 1, 31))["borrow"][0]
    assert first == pytest.approx(0.03 / 252)


def test_delisted_position_becomes_cash(eng):
    w = weights([(D(2024, 2, 15), "C", 1.0), (D(2024, 3, 15), "A", 1.0)])
    bt = eng.run(w, costs=CostModel(commission_bps=10, spread=0, spread_floor_bps=0, borrow_bps=0), lag=0, market=None)
    d = bt.daily
    assert d.filter(pl.col("date") == D(2024, 3, 4))["net"][0] == pytest.approx(-0.30)      # delisting return
    assert d.filter(pl.col("date").is_between(D(2024, 3, 5), D(2024, 3, 14)))["net"].abs().max() == 0
    reb = d.filter(pl.col("date") == D(2024, 3, 15)).row(0, named=True)
    assert reb["cost"] == pytest.approx(1.0 * 1e-3) and reb["turnover"] == pytest.approx(1.0)  # buying A only


def test_successor_position_carries_on(eng):
    w = weights([(D(2024, 2, 15), "D", 1.0)])               # signal on the predecessor
    bt = eng.run(w, costs=NO_COSTS, lag=0, market=None)
    assert bt.weights["security_id"].to_list() == ["E"]
    after = bt.daily.filter(pl.col("date") > D(2024, 3, 5))["net"]
    assert after.to_list() == pytest.approx([0.001] * after.len())


def test_filters_and_screens(eng):
    w = weights([(D(2024, 1, 31), "A", 0.5), (D(2024, 1, 31), "C", 0.5), (D(2024, 1, 31), "B", -1.0)])
    bt = eng.run(w, costs=NO_COSTS, lag=0, min_price=2.0, market=None)
    got = dict(zip(bt.weights["security_id"], bt.weights["weight"], strict=True))
    assert got == pytest.approx({"A": 1.0, "B": -1.0})      # C under $2 dropped, long leg rescaled
    p = pl.DataFrame({"date": [D(2024, 1, 31)] * 3 + [D(2024, 2, 29)] * 3, "security_id": ["A", "B", "F"] * 2,
                      "score": [3.0, 2.0, 1.0] * 2})
    f = eng.forward_returns(p)
    a = f.filter((pl.col("security_id") == "A") & (pl.col("date") == D(2024, 1, 31)))["ret_fwd"][0]
    n = len([d for d in CAL if D(2024, 1, 31) < d <= D(2024, 2, 29)])
    assert a == pytest.approx(1.01 ** n - 1)
    ic = eng.ic(p, ["score"], min_names=3)
    assert ic.is_empty() or ic["mean_ic"][0] > 0
    q = eng.quantile_returns(p, "score", n=3)
    assert q.filter(pl.col("date") == D(2024, 1, 31)).sort("quantile")["ret"][-1] > 0


def test_stats():
    s = stats(pl.Series([0.01, -0.005] * 126))
    assert s["ann_vol"] > 0 and s["max_drawdown"] <= 0 and s["sharpe"] > 0


def test_data_breaks_are_not_returns():
    days = CAL[:30]
    px = [0.10] * 15 + [24.0] * 15                           # pennies to $24: a bankruptcy emergence
    rows = [("Z", d, p, p, p, 1e6, 1.0, 1.0) for d, p in zip(days, px, strict=True)]
    bars = pl.DataFrame(rows, schema={"security_id": pl.String, "date": pl.Date, "close": pl.Float64,
                                      "high": pl.Float64, "low": pl.Float64, "volume": pl.Float64,
                                      "split_factor": pl.Float64, "div_factor": pl.Float64}, orient="row")
    r = daily_returns(bars.lazy(), None, days, "shumway")
    assert r["break"].sum() == 1 and r["ret"].drop_nulls().abs().max() == 0.0
    assert r["vol"].drop_nulls().max() < 0.2


def test_targets_that_stopped_trading_are_dropped(eng):
    # C trades until 2024-03-01; a signal on that day executes on 2024-03-04 (lag 1): no bar.
    w = weights([(D(2024, 3, 1), "C", 0.5), (D(2024, 3, 1), "A", 0.5), (D(2024, 3, 1), "B", -1.0)])
    bt = eng.run(w, costs=NO_COSTS, lag=1, market=None)
    got = dict(zip(bt.weights["security_id"], bt.weights["weight"], strict=True))
    assert got == pytest.approx({"A": 1.0, "B": -1.0}) and bt.settings["untradable_dropped"] == 1


def test_cost_warnings_name_expensive_trades(eng):
    w = weights([(D(2024, 1, 31), "A", 1.0), (D(2024, 1, 31), "B", -1.0)])
    with pytest.warns(UserWarning, match="cost more than"):
        bt = eng.run(w, costs=CostModel(spread=150, warn_bps=100), lag=0, market=None)
    assert set(bt.cost_warnings["security_id"]) == {"A", "B"}
    quiet = eng.run(w, costs=CostModel(spread=150, warn_bps=None), lag=0, market=None)
    assert quiet.cost_warnings is None
    assert {"spread_bps", "impact_bps", "adv", "vol"} <= set(bt.trades.columns)


def test_data_warnings_list_held_days_that_look_like_a_missing_split():
    class SuspectVinculum(FakeVinculum):
        def suspect_splits(self, start=None, end=None):
            return pl.DataFrame({"security_id": ["A", "B"], "ticker": ["AA", "BB"], "cik": ["1", "2"],
                                 "date": [D(2024, 2, 14), D(2024, 2, 14)], "date_before": [D(2024, 2, 13)] * 2,
                                 "price_ratio": [2.0, 2.0], "share_ratio": [2.0, 2.0]})

    eng = Engine(vinculum=SuspectVinculum(), store=FakeStore())
    w = pl.DataFrame({"date": [D(2024, 1, 31)], "security_id": ["A"], "weight": [1.0]})
    with pytest.warns(UserWarning, match="look like a split the data lacks"):
        bt = eng.run(w, market=None)
    assert bt.data_warnings["security_id"].to_list() == ["A"]          # B is not held
    plain = Engine(vinculum=FakeVinculum(), store=FakeStore()).run(w, market=None)
    assert plain.data_warnings is None                                  # no check available: nothing listed
