"""The study strategies on synthetic bars: right targets, and no look-ahead."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from assimilation_trials import breakout, config, momentum, overnight, reversal, stats_by_period
from tactical_cube import check_lookahead

DAY = date(2024, 3, 1)
N = 78                                                      # 5-minute bars in a session


def bars(paths: dict[str, list[float]], gap: float = 0.0) -> pl.DataFrame:
    rows = []
    open_ = datetime(2024, 3, 1, 14, 30, tzinfo=UTC)
    for s, closes in paths.items():
        o = closes[0]
        hi = lo = o
        for b, c in enumerate(closes):
            mfo = 5 * (b + 1)
            hi, lo = max(hi, c), min(lo, c)
            done = mfo >= 30
            or_hi = max(closes[:6]) if done else None
            or_lo = min(closes[:6]) if done else None
            brk = None if not done else (1 if c > or_hi else -1 if c < or_lo else 0)
            rows.append({"security_id": s, "ticker": s, "bar": b, "bar_end": open_ + timedelta(minutes=mfo),
                         "minutes_from_open": mfo, "minutes_to_close": 390 - mfo, "close": c, "stale": False,
                         "ret_open": c / o - 1, "gap": gap, "or_break": brk, "dollar_volume": 1e6})
    return pl.DataFrame(rows).with_columns(pl.col("bar_end").cast(pl.Datetime("ns", "UTC")))


def test_config_loads():
    c = config()
    assert c["common"]["freq"] == "5min" and c["reversal"]["top"] == 500


def test_momentum_side_and_entry_time():
    up = bars({"SPY": [100 + 0.01 * b for b in range(N)]})
    t = momentum()(DAY, up)
    assert t.height == 1 and t["weight"][0] == 1.0
    assert up.filter(pl.col("bar_end") == t["bar_end"][0])["minutes_to_close"][0] == 30
    down = bars({"SPY": [100 + 0.01 * b for b in range(N)]}, gap=-0.02)        # overnight loss dominates
    assert momentum()(DAY, down)["weight"][0] == -1.0
    check_lookahead(momentum(), DAY, up)


def test_overnight_and_intraday_hold():
    b = bars({"SPY": [100.0] * N})
    o = overnight(hold="overnight")(DAY, b).sort("auction")
    assert o.select("auction", "weight").rows() == [("close", 1.0), ("open", 0.0)]
    i = overnight(hold="intraday")(DAY, b).sort("auction")
    assert i.select("auction", "weight").rows() == [("close", 0.0), ("open", 1.0)]
    with pytest.raises(ValueError):
        overnight(hold="weekend")


def test_breakout_restates_the_book_at_each_entry():
    spy = [100.0] * 6 + [101.0] * (N - 6)                   # breaks up on the first bar after the range
    qqq = [200.0] * 10 + [199.0] * (N - 10)                 # breaks down 4 bars later
    b = bars({"SPY": spy, "QQQ": qqq})
    t = breakout()(DAY, b).sort("bar_end", "security_id")
    times = t["bar_end"].unique().sort().to_list()
    assert len(times) == 2
    assert t.filter(pl.col("bar_end") == times[0]).select("security_id", "weight").rows() == [("SPY", 0.5)]
    assert t.filter(pl.col("bar_end") == times[1]).select("security_id", "weight").rows() == [("QQQ", -0.5), ("SPY", 0.5)]
    check_lookahead(breakout(), DAY, b)


def test_reversal_buys_losers_sells_winners():
    paths = {f"S{i:03d}": [100.0] * 5 + [100.0 + (i - 100) * 0.01] * (N - 5) for i in range(200)}
    b = bars(paths)
    t = reversal(fraction=0.1)(DAY, b)
    longs, shorts = t.filter(pl.col("weight") > 0), t.filter(pl.col("weight") < 0)
    assert longs.height == shorts.height == 20
    assert set(longs["security_id"]) == {f"S{i:03d}" for i in range(20)}
    assert longs["weight"].sum() == pytest.approx(0.5) and shorts["weight"].sum() == pytest.approx(-0.5)
    assert reversal(min_names=500)(DAY, b).is_empty()
    thin = b.with_columns(dollar_volume=pl.when(pl.col("security_id") == "S000").then(1e3).otherwise(1e6))
    assert "S000" not in reversal(fraction=0.1)(DAY, thin)["security_id"].to_list()   # too thin to trade
    check_lookahead(reversal(), DAY, b)


def test_stats_by_period():
    d = pl.DataFrame({"date": [date(2004, 1, 2) + timedelta(days=i) for i in range(4000)],
                      "gross": 0.001, "net": [0.0005 + (0.001 if i % 2 else -0.001) for i in range(4000)]})
    s = stats_by_period(d)
    assert s["period"][0] == "2000-2004" and s["gross_ann"][0] == pytest.approx(0.252)
