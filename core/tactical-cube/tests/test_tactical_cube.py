"""Offline tests on hand-built sessions: six 5-minute bars a day, official auctions in `aux`."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from tactical_cube import (
    NO_INTRADAY_COSTS,
    Cube,
    IntradayCosts,
    LookaheadError,
    check_lookahead,
    market_inputs,
    simulate_day,
)
from unimatrix_zero import CostModel

D1, D2, D3 = date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5)
N = 6                                                     # bars a day, 5 minutes each
T0 = datetime(2024, 3, 1, 14, 35, tzinfo=UTC)


def opening(d):
    return datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC)


def bar_end(d, b):
    return opening(d) + timedelta(minutes=5 * (b + 1))


def make_bars(d, prices: dict[str, list], volume=1e6, stale: dict[str, set] | None = None) -> pl.DataFrame:
    """prices: security -> closes per bar; vwap = close - 0.1, open = close - 0.2."""
    rows = []
    for s, closes in prices.items():
        for b, c in enumerate(closes):
            st = b in (stale or {}).get(s, set())
            rows.append({"security_id": s, "ticker": s.lower(), "bar": b, "bar_end": bar_end(d, b),
                         "minutes_from_open": 5 * (b + 1), "minutes_to_close": 5 * (N - b - 1),
                         "open": c - 0.2, "close": c, "vwap": None if st else c - 0.1,
                         "volume": 0.0 if st else volume, "dollar_volume": 0.0 if st else volume * c, "stale": st})
    return pl.DataFrame(rows).with_columns(pl.col("bar").cast(pl.Int32), pl.col("bar_end").cast(pl.Datetime("ns", "UTC")),
                                           pl.col("minutes_from_open").cast(pl.Int32),
                                           pl.col("minutes_to_close").cast(pl.Int32), pl.col("vwap").cast(pl.Float64))


def make_aux(rows: dict[str, tuple]) -> pl.DataFrame:
    """security -> (official open, official close, prev_close)."""
    return pl.DataFrame([{"security_id": s, "open": o, "close": c, "prev_close": p, "adv": 1e9, "vol": 0.01}
                         for s, (o, c, p) in rows.items()])


def tgt(d, rows, auction=None) -> pl.DataFrame:
    """rows: (bar index of the decision, security, weight)."""
    return pl.DataFrame([{"bar_end": bar_end(d, b), "security_id": s, "weight": w, "auction": auction}
                         for b, s, w in rows], schema={"bar_end": pl.Datetime("ns", "UTC"), "security_id": pl.String,
                                                       "weight": pl.Float64, "auction": pl.String})


PRICES = {"A": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0], "B": [50.0] * N}
AUX = make_aux({"A": (100.0, 106.0, 99.0), "B": (50.0, 50.0, 50.0)})


# ---------------------------------------------------------------------------- fills and timing
def test_lagged_vwap_fill_then_closing_auction():
    r = simulate_day(D1, make_bars(D1, PRICES), tgt(D1, [(1, "A", 1.0)]), AUX)
    f = r.fills.sort("fill_bar")
    assert f["kind"].to_list() == ["bar", "close_auction"]
    buy = f.row(0, named=True)
    units = 1.0 / 101.0                                   # sized at the decision bar's close
    assert buy["fill_bar"] == 2 and buy["price"] == pytest.approx(101.9) and buy["units"] == pytest.approx(units)
    assert buy["wait_bars"] == 0 and buy["decision_price"] == 101.0
    assert f["units"][1] == pytest.approx(-units) and f["price"][1] == 106.0
    assert r.row["gross"] == pytest.approx(units * (106.0 - 101.9))
    assert r.carry.is_empty() and r.row["overnight_gross"] == 0


def test_lag_two_open_price_and_stale_bars_wait():
    bars = make_bars(D1, PRICES, stale={"A": {2}})     # bar 2 (the first allowed) has no prints
    r = simulate_day(D1, bars, tgt(D1, [(0, "A", 1.0)]), AUX, lag=2, fill="open")
    buy = r.fills.filter(pl.col("kind") == "bar").row(0, named=True)
    assert buy["fill_bar"] == 3 and buy["wait_bars"] == 1 and buy["price"] == pytest.approx(102.8)
    with pytest.raises(ValueError, match="lag"):
        simulate_day(D1, bars, tgt(D1, [(0, "A", 1.0)]), AUX, lag=0)


def test_participation_cap_fills_in_parts():
    # 1e6 capital, 50% weight in B at $50 = 10,000 shares; 3,000 a bar at 10% of 30,000
    r = simulate_day(D1, make_bars(D1, PRICES, volume=30_000), tgt(D1, [(0, "B", 0.5)]), AUX,
                     capital=1e6, participation=0.10)
    buys = r.fills.filter(pl.col("kind") == "bar").sort("fill_bar")
    assert buys["units"].to_list() == pytest.approx([3000, 3000, 3000, 1000])
    assert buys["fill_bar"].to_list() == [1, 2, 3, 4]
    r2 = simulate_day(D1, make_bars(D1, PRICES, volume=5_000), tgt(D1, [(0, "B", 0.5)]), AUX,
                      capital=1e6, participation=0.10)
    assert r2.unfilled["reason"].to_list() == ["not filled by the end of the session"]
    assert r2.carry.is_empty()                            # what was bought is sold in the auction


def test_rebalance_replaces_the_book_and_flat_by_time():
    t = tgt(D1, [(0, "A", 1.0), (2, "B", 1.0)])
    r = simulate_day(D1, make_bars(D1, PRICES), t, AUX, flat_by="09:50")      # bar 3 ends 09:50 New York
    f = r.fills.sort("fill_bar", "security_id")
    assert [(x["security_id"], x["fill_bar"], x["units"] > 0) for x in f.iter_rows(named=True)] == \
        [("A", 1, True), ("A", 3, False), ("B", 3, True), ("B", 4, False)]
    assert f.filter(pl.col("kind") == "close_auction").is_empty()
    late = simulate_day(D1, make_bars(D1, PRICES), tgt(D1, [(4, "A", 1.0)]), AUX, flat_by="09:50")
    assert late.fills.is_empty() and late.warnings
    # With lag 2 a decision at bar 2 would start trading in bar 4, after flat_by: ignored.
    slow = simulate_day(D1, make_bars(D1, PRICES), t, AUX, flat_by="09:50", lag=2)
    f = slow.fills.sort("fill_bar", "security_id")
    assert [(x["security_id"], x["fill_bar"], x["units"] > 0) for x in f.iter_rows(named=True)] == \
        [("A", 2, True), ("A", 5, False)]
    assert any("after flat_by" in w for w in slow.warnings)


def test_errors_and_rejections():
    bars = make_bars(D1, PRICES)
    with pytest.raises(ValueError, match="overnight"):
        simulate_day(D1, bars, tgt(D1, [(1, "A", 1.0)], auction="open"), AUX)
    with pytest.raises(ValueError, match="flat_by"):
        simulate_day(D1, bars, tgt(D1, [(1, "A", 1.0)], auction="close"), AUX)
    odd = tgt(D1, [(1, "A", 1.0)]).with_columns(pl.col("bar_end") + pl.duration(minutes=1))
    with pytest.raises(ValueError, match="not the end"):
        simulate_day(D1, bars, odd, AUX)
    r = simulate_day(D1, bars, tgt(D1, [(4, "A", 1.0)], auction="close"), AUX, flat_by=None, moc_cutoff=10)
    assert r.fills.is_empty() and "closing-auction" in r.unfilled["reason"][0]
    r = simulate_day(D1, bars, tgt(D1, [(5, "A", 1.0)]), AUX)
    assert r.unfilled["reason"].to_list() == ["after the last bar"]


# ---------------------------------------------------------------------------- overnight
def test_overnight_split_dividend_and_opening_auction():
    # Day 1: buy A in the closing auction. Day 2: A splits 2:1 (prev_close 53 = 106 / 2);
    # an opening-auction order decided on day 2 sells everything on day 3.
    r1 = simulate_day(D1, make_bars(D1, PRICES), tgt(D1, [(2, "A", 1.0)], auction="close"), AUX, flat_by=None)
    u = 1.0 / 102.0
    assert r1.carry["units"][0] == pytest.approx(u) and r1.row["gross"] == 0
    p2 = [53.0, 54.0, 55.0, 55.5, 56.0, 56.5]
    aux2 = make_aux({"A": (52.0, 57.0, 53.0)})
    r2 = simulate_day(D2, make_bars(D2, {"A": p2}), tgt(D2, [(3, "A", 0.0)], auction="open"), aux2,
                      carry=r1.carry, flat_by=None)
    assert r2.carry["units"][0] == pytest.approx(2 * u)                       # shares doubled
    assert r2.row["gross"] == pytest.approx(2 * u * (57.0 - 53.0))            # from prev_close, total return
    assert r2.pending["units"][0] == 0.0
    aux3 = make_aux({"A": (58.0, 59.0, 57.0)})
    r3 = simulate_day(D3, make_bars(D3, {"A": [58.0] * N}), tgt(D3, []), aux3, carry=r2.carry,
                      pending=r2.pending, flat_by=None)
    sell = r3.fills.row(0, named=True)
    assert sell["kind"] == "open_auction" and sell["price"] == 58.0 and sell["units"] == pytest.approx(-2 * u)
    assert r3.row["gross"] == pytest.approx(2 * u * (58.0 - 57.0)) and r3.carry.is_empty()


# ---------------------------------------------------------------------------- costs
def test_intraday_costs():
    fills = pl.DataFrame({"units": [1000.0, 10.0, -1000.0], "price": [50.0, 50.0, 50.0],
                          "kind": ["bar", "bar", "close_auction"], "start_minute": [0.0, 60.0, 385.0],
                          "adv": [1e8, 1e8, 1e8], "vol": [0.02, 0.02, 0.02],
                          "decision_time": [T0, T0 + timedelta(minutes=55), None]})
    c = IntradayCosts(base=CostModel(spread=2.0), aum=1e6).apply(fills, 1e6)
    assert c["spread_bps"].to_list() == pytest.approx([7.42, 2.04, 0.0])      # measured: x3.71 in minute 0, x1.02 at 60-120
    assert c["commission_bps"][0] == pytest.approx(3.5 / 50_000 * 1e4)        # $0.0035 x 1000 shares
    assert c["commission_bps"][1] == pytest.approx(0.35 / 500 * 1e4)          # the $0.35 minimum
    assert c["impact_bps"][0] == pytest.approx(0.02 * (50_000 / 1e8) ** 0.5 * 1e4)
    assert c["cost"][0] == pytest.approx(50_000 / 1e6 * (c["spread_bps"][0] + c["impact_bps"][0]
                                                         + c["commission_bps"][0]) / 1e4)
    no_aum = IntradayCosts().apply(fills.head(1), 1.0)
    assert no_aum["impact_bps"][0] == 0 and no_aum["commission_bps"][0] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        IntradayCosts(base=CostModel(spread="estimate"))


def test_impact_and_commission_minimum_are_per_order():
    """Four fills of one order pay the impact of the whole order and one commission minimum."""
    one = pl.DataFrame({"units": [40.0], "price": [50.0], "kind": ["bar"], "start_minute": [60.0],
                        "adv": [1e6], "vol": [0.02], "decision_time": [T0]})
    split = pl.concat([one.with_columns(units=pl.lit(10.0), start_minute=pl.lit(60.0 + 5 * i)) for i in range(4)])
    costs = IntradayCosts(base=CostModel(spread=0.0), aum=1e6)
    a, b = costs.apply(one, 1e6), costs.apply(split, 1e6)
    assert b["cost"].sum() == pytest.approx(a["cost"][0])
    assert b["impact_bps"].to_list() == pytest.approx([a["impact_bps"][0]] * 4)
    assert b["commission_bps"][0] == pytest.approx(0.35 / 2000 * 1e4)        # one $0.35 minimum on $2,000
    other_side = split.with_columns(units=pl.Series([10.0, 10.0, -10.0, -10.0]))   # a buy and a sell: two orders
    assert costs.apply(other_side, 1e6)["commission_bps"][0] == pytest.approx(0.35 / 1000 * 1e4)


def test_market_inputs_use_the_previous_day_only():
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(30)]
    s = pl.DataFrame({"date": days, "security_id": "A", "ticker": "a", "open": 10.0, "close": 10.0,
                      "prev_close": 10.0, "volume": [100.0 * (i + 1) for i in range(30)]})
    m = market_inputs(s)
    assert m["adv"][0] is None and m["adv"][1] == pytest.approx(1000.0)        # day 1's own volume excluded
    assert m["adv"][25] == pytest.approx(sum(10.0 * 100 * (i + 1) for i in range(4, 25)) / 21)


# ---------------------------------------------------------------------------- look-ahead
def test_check_lookahead():
    bars = make_bars(D1, PRICES)

    def honest(day, b):
        x = b.filter((pl.col("bar") == 1) & (pl.col("close") > 100))
        return x.select("bar_end", "security_id", weight=pl.lit(1.0))

    def cheat(day, b):                                    # uses the day's last close
        best = b.sort("bar").group_by("security_id").agg(pl.col("close").last()).sort("close")[-1, "security_id"]
        return b.filter((pl.col("bar") == 1) & (pl.col("security_id") == best)).select(
            "bar_end", "security_id", weight=pl.lit(1.0))

    check_lookahead(honest, D1, bars)
    rising = make_bars(D1, {"A": [100.0, 99.0, 98.0, 110.0, 111.0, 112.0], "B": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]})
    with pytest.raises(LookaheadError):
        check_lookahead(cheat, D1, rising)


# ---------------------------------------------------------------------------- Cube.run
class FakeProbe:
    def __init__(self, days: dict):
        self.d = days                                     # date -> (prices, aux rows)

    def days(self, start, end, tickers=None, **_):
        for d in sorted(self.d):
            if start <= d <= end:
                yield d, make_bars(d, self.d[d][0])

    def summaries(self, start=None, end=None, lazy=False, **_):
        rows = [{"date": d, "security_id": s, "ticker": s.lower(), "open": o, "close": c, "prev_close": p,
                 "volume": 1e6} for d, (_, aux) in self.d.items() for s, (o, c, p) in aux.items()]
        f = pl.DataFrame(rows)
        return f.lazy() if lazy else f


class FakeVinculum:
    def short_interest(self, ids, start, end, by="security"):
        return pl.DataFrame({"security_id": ["A"], "published_date": [D1], "si_pct_float": [0.5]})


def test_cube_run_strategy_and_targets_frame():
    days = {D1: (PRICES, {"A": (100.0, 106.0, 99.0), "B": (50.0, 50.0, 50.0)}),
            D2: (PRICES, {"A": (105.0, 106.0, 106.0), "B": (50.0, 50.0, 50.0)})}
    cube = Cube(probe=FakeProbe(days), vinculum=FakeVinculum())

    def strat(day, b):
        return b.filter((pl.col("bar") == 1) & (pl.col("security_id") == "A")).select(
            "bar_end", "security_id", weight=pl.lit(1.0))

    bt = cube.run(strat, D1, D2, tickers=["A"], costs=NO_INTRADAY_COSTS)
    assert bt.daily.height == 2 and bt.settings["lookahead_checked_days"] == 2
    assert bt.daily["gross"][0] == pytest.approx((106.0 - 101.9) / 101.0)
    assert bt.daily["net"].to_list() == pytest.approx(bt.daily["gross"].to_list())
    s = bt.summary()
    assert s.filter(pl.col("series") == "net")["turnover_per_year"][0] > 0
    assert bt.round_trips()["fills"].to_list() == [2, 2]
    assert set(bt.by_time_of_day()["minutes_from_open"]) == {5 * (b + 1) for b in range(N)}

    # the same as a targets frame, with costs: net below gross by the fills' costs
    t = pl.concat([tgt(d, [(1, "A", 1.0)]).with_columns(date=pl.lit(d)) for d in (D1, D2)])
    bt2 = cube.run(targets=t, costs=IntradayCosts(base=CostModel(spread=5.0)))
    assert bt2.daily["gross"].to_list() == pytest.approx(bt.daily["gross"].to_list())
    assert (bt2.daily["cost"] > 0).all()
    assert bt2.daily["net"].to_list() == pytest.approx((bt2.daily["gross"] - bt2.daily["cost"]).to_list())
    with pytest.raises(ValueError, match="one of them"):
        cube.run(strat, D1, D2, targets=t)


def test_cube_borrow_only_on_overnight_shorts():
    days = {D1: (PRICES, {"A": (100.0, 106.0, 99.0)}), D2: (PRICES, {"A": (106.0, 106.0, 106.0)})}
    cube = Cube(probe=FakeProbe(days), vinculum=FakeVinculum())
    t = tgt(D1, [(2, "A", -1.0)], auction="close").with_columns(date=pl.lit(D1))
    bt = cube.run(targets=t, start=D1, end=D2, flat_by=None,
                  costs=IntradayCosts(base=CostModel(spread=0.0, commission_bps=0.0, borrow_bps=25, htb_bps=300)))
    value = 106.0 / 102.0                                 # shares sized at 102, valued at the 106 close
    assert bt.daily["borrow"][0] == pytest.approx(value * 0.03 / 252)          # hard to borrow: SI 50% of float
    flat = cube.run(targets=tgt(D1, [(1, "A", -1.0)]).with_columns(date=pl.lit(D1)), start=D1, end=D2)
    assert (flat.daily["borrow"] == 0).all()
