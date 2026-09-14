import datetime as dt

import polars as pl

from collective_alpha.backtest.engine import BacktestConfig, CostModel, run_backtest
from collective_alpha.backtest.weights import long_short_quantiles, on_rebalance_dates, rebalance_dates, signal_weighted
from collective_alpha.calendar import trading_days

D = dt.date
SESSIONS = trading_days(D(2024, 1, 2), D(2024, 3, 28))


def _panel(rets: dict[str, list[float]]) -> pl.DataFrame:
    rows = []
    for sid, r in rets.items():
        days = SESSIONS[: len(r)]
        for k, (d, x) in enumerate(zip(days, r, strict=True)):
            rows.append({"security_id": sid, "date": d, "ret": None if k == 0 else x, "is_last_trade": k == len(r) - 1})
    return pl.DataFrame(rows)


def test_weight_schemes():
    sig = pl.DataFrame(
        {"security_id": [f"S{i}" for i in range(10)], "date": [SESSIONS[0]] * 10, "signal": list(range(10))}
    )
    w = long_short_quantiles(sig, n_quantiles=5, gross=2.0)
    assert w.height == 4 and abs(w["weight"].sum()) < 1e-12 and abs(w["weight"].abs().sum() - 2.0) < 1e-12
    assert set(w.filter(pl.col("weight") > 0)["security_id"]) == {"S8", "S9"}
    lo = long_short_quantiles(sig, n_quantiles=5, short_q=None, gross=1.0)
    assert lo.height == 2 and abs(lo["weight"].sum() - 1.0) < 1e-12
    sw = signal_weighted(sig, gross=2.0)
    assert abs(sw["weight"].sum()) < 1e-12 and abs(sw["weight"].abs().sum() - 2.0) < 1e-12
    assert sw.sort("security_id")["weight"][9] > 0 > sw.sort("security_id")["weight"][0]
    rd = rebalance_dates(SESSIONS, "monthly")
    assert rd == {D(2024, 1, 2), D(2024, 2, 1), D(2024, 3, 1)}
    assert len(on_rebalance_dates(w.with_columns(date=pl.lit(SESSIONS[3])), SESSIONS, 21)) == 0


def test_engine_single_long_with_delay_and_costs():
    # A: +1% every day. Target 100% long on day 0, executed at close of day 1 (delay 1), earns from day 2.
    panel = _panel({"A": [0.0] + [0.01] * 9})
    tgt = pl.DataFrame({"security_id": ["A"], "date": [SESSIONS[0]], "weight": [1.0]})
    cfg = BacktestConfig(delay=1, costs=CostModel(commission_bps=10, half_spread_bps=0, slippage_bps=0, borrow_rate=0))
    res = run_backtest(tgt, panel, cfg)
    d = res.daily.sort("date")  # starts on the execution day (delay 1 -> session 2)
    assert d["date"][0] == SESSIONS[1]
    assert d["ret_gross"].to_list()[:2] == [0.0, 0.01]
    assert abs(d["cost"][0] - 1.0 * 10 / 1e4) < 1e-12 and d["turnover"][0] == 1.0
    assert abs(d["ret_net"][0] + 0.001) < 1e-12
    assert d["gross"].to_list()[:2] == [1.0, 1.0] and d["n_long"][1] == 1
    s = res.summary()
    assert s["days"] == 9 and abs(s["total_return_net"] - ((1 - 0.001) * 1.01**8 - 1)) < 1e-4


def test_engine_long_short_drift_and_borrow():
    panel = _panel({"A": [0.0, 0.10, 0.0], "B": [0.0, -0.10, 0.0]})
    tgt = pl.DataFrame({"security_id": ["A", "B"], "date": [SESSIONS[0]] * 2, "weight": [1.0, -1.0]})
    cfg = BacktestConfig(delay=0, costs=CostModel(0, 0, 0, borrow_rate=0.252))
    res = run_backtest(tgt, panel, cfg)
    d = res.daily.sort("date")  # delay 0 -> starts on session 0
    # executed at close of day 0; day 1 earns +10% on long and +10% on short => +20% gross
    assert abs(d["ret_gross"][1] - 0.20) < 1e-12
    # borrow on 1.0 short notional at 0.252/yr = 0.1% per day
    assert abs(d["borrow"][0] - 0.001) < 1e-12
    # drift: after day 1, long grew to 1.1/1.2 and short to -0.9/1.2
    assert abs(d["gross"][1] - (1.1 + 0.9) / 1.2) < 1e-12


def test_engine_delisting():
    # B delists after day 2; long 50/50; delist_return -0.5 applied on day 3
    panel = _panel({"A": [0.0] * 6, "B": [0.0, 0.0, 0.0]})
    tgt = pl.DataFrame({"security_id": ["A", "B"], "date": [SESSIONS[0]] * 2, "weight": [0.5, 0.5]})
    res = run_backtest(tgt, panel, BacktestConfig(delay=0, delist_return=-0.5, costs=CostModel(0, 0, 0, 0)))
    d = res.daily.sort("date")
    assert abs(d["ret_gross"][3] + 0.25) < 1e-12  # half the book lost 50%
    assert d["n_long"][3] == 1 and abs(d["gross"][3] - 0.5) < 1e-12


def test_engine_impact_cost():
    panel = _panel({"A": [0.0] * 3})
    tgt = pl.DataFrame({"security_id": ["A"], "date": [SESSIONS[0]], "weight": [1.0]})
    adv = pl.DataFrame({"security_id": ["A"], "date": [SESSIONS[0]], "adv": [4_000_000.0]})
    cfg = BacktestConfig(delay=0, capital=1_000_000, costs=CostModel(0, 0, 0, 0, impact_coef=0.1))
    d = run_backtest(tgt, panel, cfg, adv).daily.sort("date")
    assert abs(d["cost"][0] - 0.1 * (0.25**0.5)) < 1e-12  # participation 25%
