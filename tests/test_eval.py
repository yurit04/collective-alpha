import datetime as dt
import random

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.eval import metrics as M
from collective_alpha.eval.forward_returns import forward_returns
from collective_alpha.eval.report import evaluate
from collective_alpha.panel.build import build_panel

D = dt.date
SESSIONS = trading_days(D(2023, 1, 3), D(2023, 12, 29))
EMPTY_S = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "split_ratio": pl.Float64})
EMPTY_D = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "dividend": pl.Float64})


def _bars(sid, closes, days=None, opens=None):
    days = days or SESSIONS[: len(closes)]
    opens = opens or closes
    return pl.DataFrame(
        {
            "security_id": [sid] * len(days),
            "date": days,
            "ticker": [sid] * len(days),
            "open": opens,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(days),
            "transactions": [10] * len(days),
        }
    )


def test_forward_returns_basic_and_delisting():
    a = _bars("A", [100.0, 110.0, 121.0, 133.1, 146.41], opens=[100.0, 105.0, 115.0, 130.0, 140.0])
    b = _bars("B", [10.0, 9.0], SESSIONS[:2])  # delists after session 2 (store runs to session 5)
    panel = build_panel(pl.concat([a, b]), EMPTY_S, EMPTY_D, SESSIONS)
    fr = forward_returns(panel, horizons=(1, 2, 3), delist_return=-0.5).sort(["security_id", "date"])
    fa = fr.filter(pl.col("security_id") == "A")
    assert abs(fa["fwd_ret_1d"][0] - 0.10) < 1e-12 and abs(fa["fwd_ret_2d"][0] - 0.21) < 1e-12
    assert fa["fwd_ret_1d"][4] is None  # store end: unknown, not truncated
    assert fa["fwd_ret_3d"][3] is None
    assert abs(fa["fwd_o2c_1d"][0] - (110 / 105 - 1)) < 1e-12 and abs(fa["fwd_c2o_1d"][0] - (105 / 100 - 1)) < 1e-12
    fb = fr.filter(pl.col("security_id") == "B")
    # B: from session 1, 1d ahead exists (-10%); 2d ahead does not -> last price then -50%
    assert abs(fb["fwd_ret_1d"][0] + 0.10) < 1e-12
    assert abs(fb["fwd_ret_2d"][0] - (0.9 * 0.5 - 1)) < 1e-12
    assert abs(fb["fwd_ret_1d"][1] - (-0.5)) < 1e-12  # on its last bar: delisting return only


def _synthetic(n_names=60, seed=0, strength=0.5):
    rng = random.Random(seed)
    rows = []
    for d in SESSIONS[:120]:
        for i in range(n_names):
            fwd = rng.gauss(0, 0.02)
            sig = strength * fwd + rng.gauss(0, 0.02)
            rows.append(
                {
                    "security_id": f"S{i}",
                    "date": d,
                    "signal": sig,
                    "fwd_ret_1d": fwd,
                    "fwd_ret_5d": fwd * 1.5 + rng.gauss(0, 0.02),
                }
            )
    return pl.DataFrame(rows)


def test_metrics_on_synthetic_signal():
    df = _synthetic()
    ic = M.rank_ic(df, "signal", "fwd_ret_1d")
    s = M.ic_summary(ic)
    assert s["n_dates"] == 120 and s["ic_mean"] > 0.25 and s["hit_rate"] > 0.9
    q = M.quantile_returns(df, "signal", "fwd_ret_1d", 5)
    qs = M.quantile_summary(q)
    assert qs["q"].to_list() == [1, 2, 3, 4, 5]
    assert qs["mean_ret_bp"][4] > qs["mean_ret_bp"][0]  # monotone-ish: top beats bottom
    ls = M.long_short(q, 5)
    perf = M.performance(ls["spread"])
    assert perf["sharpe"] > 3 and perf["hit_rate"] > 0.8
    decay = M.ic_decay(df, "signal", (1, 5))
    assert decay["horizon"].to_list() == [1, 5]
    # iid noise signal -> no persistence; quantile turnover near 1 - 1/5
    ac = M.signal_autocorr(df, "signal", (1,))
    assert abs(ac["rank_autocorr"][0]) < 0.15
    members = df.with_columns(
        q=(
            (pl.col("signal").rank(method="ordinal").over("date") - 1) * 5 // pl.col("signal").count().over("date") + 1
        ).cast(pl.Int32)
    )
    to = M.quantile_turnover(members, 5)
    assert 0.6 < to["top_turnover"] < 0.95


def test_no_signal_gives_flat_ic():
    df = _synthetic(strength=0.0, seed=1)
    s = M.ic_summary(M.rank_ic(df, "signal", "fwd_ret_1d"))
    assert abs(s["ic_mean"]) < 0.05 and abs(s["ic_tstat"]) < 3


def test_evaluate_report_roundtrip():
    df = _synthetic(n_names=40)
    sig = df.select("security_id", "date", "signal")
    fwd = df.select("security_id", "date", "fwd_ret_1d", "fwd_ret_5d")
    rep = evaluate(sig, fwd, name="syn", universe="test", horizon=5, n_quantiles=5)
    d = rep.to_dict()
    assert d["ic"]["ic_mean"] > 0.2 and len(d["quantiles"]) == 5 and d["by_year"][0]["year"] == 2023
    md = rep.to_markdown()
    assert "syn on test" in md and "thin cross-section" in md  # 40 names < 50


def test_walk_forward_windows():
    w = M.walk_forward_windows(SESSIONS, train_sessions=100, test_sessions=20)
    assert w[0][0] == SESSIONS[0] and w[0][1] == SESSIONS[99] and w[0][2] == SESSIONS[100] and w[0][3] == SESSIONS[119]
    assert w[1][2] == SESSIONS[120]
    assert all(b < c for _, b, c, _ in w)


def test_performance_overlapping_horizon():
    # a constant +1% 21-day return observed daily: compounding daily would overstate everything
    r = pl.Series([0.01] * 252)
    p1 = M.performance(r, horizon=21)
    assert p1["n_non_overlapping"] == 12 and abs(p1["total_return"] - (1.01**12 - 1)) < 1e-4
    assert abs(p1["ann_return"] - 0.12) < 1e-9 and p1["max_drawdown"] == 0.0
