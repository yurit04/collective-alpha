"""Offline tests: dates, walk-forward weights (no peeking), composites, benchmark, whole shares,
commission and statistics."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from tertiary_adjunct import (
    benchmark,
    constraints,
    deflated_sharpe,
    equal_groups,
    figures,
    ib_commission,
    load,
    max_drawdown,
    psr,
    relative_drawdown,
    round_to_shares,
    sharpe,
    signal_dates,
    signals,
    walk_forward_weights,
    weighted_signals,
    with_commission,
)

P = load()


def weekdays(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5]


# ------------------------------------------------------------------ protocol and dates
def test_protocol_is_complete():
    assert set(P["composites"]) == {"C1", "C2", "C3"} and P["capital"] == 100_000
    assert len(signals(P)) == len(set(signals(P))) == 17
    assert P["periods"]["dev_end"] < P["periods"]["holdout_start"]
    assert P["selection"]["trials_per_form"] == len(P["composites"])


def test_signal_dates_are_month_ends_and_mid_months():
    cal = [d for d in weekdays(date(2024, 1, 1), date(2024, 4, 10)) if d != date(2024, 3, 15)]  # a holiday on the 15th
    got = signal_dates(cal, date(2024, 1, 1), date(2024, 3, 31))
    assert got == [date(2024, 1, 15), date(2024, 1, 31), date(2024, 2, 15), date(2024, 2, 29),
                   date(2024, 3, 14), date(2024, 3, 29)]
    assert signal_dates(cal, kinds=("month_end",))[:2] == [date(2024, 1, 31), date(2024, 2, 29)]


# ------------------------------------------------------------------ walk-forward weights
def _ics(months: list[date], ic: dict[str, float]) -> pl.DataFrame:
    rows = []
    for i, m in enumerate(months[:-1]):
        for s, v in ic.items():
            rows.append({"date": m, "next_date": months[i + 1], "signal": s, "ic": v + 0.01 * ((i % 3) - 1)})
    return pl.DataFrame(rows)


def test_walk_forward_weights_use_only_known_ics():
    months = [date(2010 + (i // 12), i % 12 + 1, 28) for i in range(40)]
    ics = _ics(months, {"a": 0.05, "b": 0.02, "c": -0.03})
    d = months[30]
    w = walk_forward_weights(ics, [d], ["a", "b", "c"], window_months=36, min_months=24, shrink=0.5)
    known = w["known"][0]
    # The IC dated months[29] needs months[30] to have passed: not known on months[30] itself.
    assert known == 29
    wt = dict(zip(w["signal"], w["weight"]))
    assert wt["a"] > wt["b"] > wt["c"] == pytest.approx(0.5 / 3)    # a negative IC keeps only the shrink part
    assert sum(wt.values()) == pytest.approx(1.0)
    # Changing an IC that is not yet known changes nothing.
    later = ics.with_columns(pl.when(pl.col("date") >= months[29]).then(9.0).otherwise(pl.col("ic")).alias("ic"))
    w2 = walk_forward_weights(later, [d], ["a", "b", "c"], 36, 24, 0.5)
    assert w2["weight"].to_list() == w["weight"].to_list()


def test_walk_forward_weights_are_equal_before_min_months():
    months = [date(2010, m, 28) for m in range(1, 13)]
    w = walk_forward_weights(_ics(months, {"a": 0.05, "b": 0.0}), [months[-1]], ["a", "b"], 36, 24, 0.5)
    assert w["weight"].to_list() == [0.5, 0.5]


# ------------------------------------------------------------------ composites
def _panel(n=200, seed=1) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    d = date(2015, 1, 30)
    cols = {s: rng.normal(size=n) for s in signals(P)}
    return pl.DataFrame({"date": [d] * n, "security_id": [f"S{i:03d}" for i in range(n)],
                         "issuer_market_cap": np.exp(rng.normal(22, 1, n)), "sector": ["Tech"] * n, **cols})


def test_equal_groups_follows_directions():
    p = _panel()
    out = equal_groups(p, P, ["value", "quality", "momentum"], "C1")
    # accruals has direction -1: the score falls as accruals rise, other things equal.
    corr = out.select(pl.corr("C1", "accruals")).item()
    assert corr < 0 and out.select(pl.corr("C1", "earnings_yield")).item() > 0
    # A row with only one group has no score.
    thin = p.with_columns([pl.lit(None, pl.Float64).alias(s) for s in signals(P, ["value", "quality"])])
    assert equal_groups(thin, P, ["value", "quality", "momentum"], "C1")["C1"].null_count() == thin.height


def test_weighted_signals_need_two_groups_and_use_weights():
    p = _panel()
    d = p["date"][0]
    w = pl.DataFrame({"date": [d, d, d], "signal": ["earnings_yield", "roe", "momentum_12_1"],
                      "weight": [1.0, 0.0, 0.0]})
    out = weighted_signals(p, P, w, "C3")
    assert out["C3"].null_count() == out.height                     # one positively weighted group only
    w = w.with_columns(pl.Series("weight", [0.5, 0.5, 0.0]))
    out = weighted_signals(p, P, w, "C3")
    assert out["C3"].null_count() == 0
    assert out.select(pl.corr("C3", "momentum_12_1")).item() == pytest.approx(0, abs=0.2)


# ------------------------------------------------------------------ books
def test_constraints_per_form():
    ls, lo = constraints(P, "long_short"), constraints(P, "long_only")
    assert ls.long_short and ls.gross == 2.0 and ls.max_weight == 0.02 and ls.sector_net == 0.01
    assert not lo.long_short and lo.target_vol == 0.05 and lo.sector_net == 0.03 and lo.max_weight == 0.05
    with pytest.raises(ValueError):
        constraints(P, "levered")


def test_benchmark_is_the_largest_cap_weighted():
    p = _panel()
    b = benchmark(p, 50)
    assert b.height == 50 and b["weight"].sum() == pytest.approx(1.0)
    top = p.sort("issuer_market_cap", descending=True).head(50)["security_id"].to_list()
    assert set(b["security_id"]) == set(top)


def test_round_to_shares():
    d = date(2020, 1, 31)
    w = pl.DataFrame({"date": [d] * 5, "security_id": list("ABCDE"), "weight": [0.02, -0.02, 0.0015, 0.02, 0.01]})
    px = pl.DataFrame({"security_id": list("ABCDE"), "date": [d - timedelta(days=1)] * 5,
                       "raw_close": [33.0, 50.0, 10.0, 1500.0, 700.0]})
    out = round_to_shares(w, px, 100_000, max_weight=0.02, min_position=250, min_shares_at_max=2)
    got = {r["security_id"]: r for r in out.iter_rows(named=True)}
    assert got["A"]["shares"] == 61 and got["A"]["weight"] == pytest.approx(61 * 33 / 100_000)   # $2,000 / $33
    assert got["B"]["shares"] == -40
    assert "C" not in got                                       # $150: under the minimum position
    assert "D" not in got                                       # $1,500 > $2,000 / 2
    assert got["E"]["shares"] == 1                              # $1,000 / $700 rounds to 1
    lo = pl.DataFrame({"date": [d] * 2, "security_id": ["A", "B"], "weight": [0.6, 0.6]})
    out = round_to_shares(lo, px, 100_000, max_weight=1.0, min_position=250, min_shares_at_max=1, long_only=True)
    assert out["weight"].sum() == pytest.approx(1.0)


def test_ib_commission():
    d = date(2020, 2, 3)
    trades = pl.DataFrame({"date": [d] * 4, "security_id": list("ABCD"), "before": [0.0, 0.0, 0.0, 0.01],
                           "after": [0.02, 0.0005, 0.0001, 0.01]})
    daily = pl.DataFrame({"date": [d], "nav": [1.0], "net": [0.001]})
    px = pl.DataFrame({"security_id": list("ABCD"), "date": [d] * 4, "raw_close": [20.0, 50.0, 2.0, 10.0]})
    c = ib_commission(trades, daily, px, 100_000, 0.0035, 0.35, 0.01)
    # A: 100 shares -> $0.35 (minimum); B: 1 share of $50 -> $0.35; C: 5 shares of $2 = $10 -> 1% = $0.10; D: none.
    assert c["orders"][0] == 3 and c["commission"][0] == pytest.approx(0.35 + 0.35 + 0.10)
    out = with_commission(daily, c)
    assert out["net"][0] == pytest.approx(0.001 - 0.80 / 100_000)


# ------------------------------------------------------------------ statistics
def test_statistics():
    rng = np.random.default_rng(5)
    r = rng.normal(0.0004, 0.01, 2520)
    assert sharpe(r) == pytest.approx(r.mean() / r.std(ddof=1) * np.sqrt(252))
    assert max_drawdown([0.1, -0.5, 0.2]) == pytest.approx(-0.5)
    assert relative_drawdown([0.0, 0.0], [0.1, 0.1]) == pytest.approx(1 / 1.1 - 1)
    assert psr(r) > 0.9 and psr(r, benchmark_sr=sharpe(r, annualize=False)) == pytest.approx(0.5, abs=0.01)
    trials = [sharpe(r, False), sharpe(r, False) * 0.5, 0.0]
    assert deflated_sharpe(r, trials) < psr(r)
    d = pl.DataFrame({"date": [date(2020, 1, 1) + timedelta(days=i) for i in range(len(r))], "net": r,
                      "gross": r + 0.0001, "turnover": [0.01] * len(r)})
    mkt = d.select("date", (pl.col("net") * 0.5).alias("ret"))
    f = figures(d, mkt)
    assert f["sharpe"] == pytest.approx(sharpe(r)) and f["cost_drag"] > 0 and f["ir"] > 0
    assert f["turnover_per_year"] == pytest.approx(2.52, rel=0.01)
