import datetime as dt

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.panel.build import build_panel, to_wide
from collective_alpha.panel.corporate_actions import clean_dividends, clean_splits, map_events_to_security

D = dt.date
SESSIONS = trading_days(D(2024, 1, 2), D(2024, 3, 28))


def _bars(sid: str, closes: dict[D, float], ticker: str | None = None) -> pl.DataFrame:
    days = sorted(closes)
    return pl.DataFrame(
        {
            "security_id": [sid] * len(days),
            "date": days,
            "ticker": [ticker or sid] * len(days),
            "open": [closes[d] for d in days],
            "high": [closes[d] * 1.01 for d in days],
            "low": [closes[d] * 0.99 for d in days],
            "close": [closes[d] for d in days],
            "volume": [1000.0] * len(days),
            "transactions": [10] * len(days),
        }
    )


def test_clean_splits_and_dividends():
    splits = pl.DataFrame(
        {
            "ticker": ["A", "A", "B"],
            "execution_date": ["2024-02-01", "2024-02-01", "2024-02-05"],
            "split_from": [1.0, 1.0, 10.0],
            "split_to": [2.0, 1.0, 1.0],
        }
    )
    cs = clean_splits(splits)
    assert cs.filter(pl.col("ticker") == "A")["split_ratio"][0] == 2.0  # 1:1 no-op dropped, 2-for-1 kept
    assert cs.filter(pl.col("ticker") == "B")["split_ratio"][0] == 0.1
    divs = pl.DataFrame(
        {
            "ticker": ["A", "A", "A", "C"],
            "ex_dividend_date": ["2024-02-10", "2024-02-10", "2024-02-10", "2024-02-10"],
            "cash_amount": [0.5, 0.5, 1.0, 3.0],
            "dividend_type": ["CD", "CD", "SC", "LT"],
            "currency": ["USD", "USD", "USD", "USD"],
        }
    )
    cd = clean_dividends(divs)
    assert cd.height == 1 and cd["dividend"][0] == 1.5  # exact duplicate collapsed, special added, LT excluded


def test_map_events_grace():
    master = pl.DataFrame(
        {"ticker": ["A"], "security_id": ["S"], "valid_from": [D(2024, 1, 2)], "valid_to": [D(2024, 2, 1)]}
    )
    ev = pl.DataFrame(
        {"ticker": ["A", "A", "A"], "date": [D(2024, 1, 15), D(2024, 2, 5), D(2024, 3, 1)], "x": [1, 2, 3]}
    )
    out = map_events_to_security(ev, master)
    assert out["x"].to_list() == [1, 2]  # 4 days after last trade still maps; a month later does not


def test_returns_across_split_and_dividend():
    d0, d1, d2, d3 = SESSIONS[:4]
    # 2-for-1 split on d2 (price halves), $1 dividend on d3
    bars = _bars("S", {d0: 100.0, d1: 102.0, d2: 52.0, d3: 51.0})
    splits = pl.DataFrame({"security_id": ["S"], "date": [d2], "split_ratio": [2.0]})
    divs = pl.DataFrame({"security_id": ["S"], "date": [d3], "dividend": [1.0]})
    p = build_panel(bars, splits, divs, SESSIONS).sort("date")
    ret = p["ret"].to_list()
    assert ret[0] is None
    assert abs(ret[1] - 0.02) < 1e-12
    assert abs(ret[2] - (52 * 2 / 102 - 1)) < 1e-12  # split-neutral
    assert abs(ret[3] - ((51 + 1) / 52 - 1)) < 1e-12  # dividend counted in total return
    assert abs(p["ret_px"][3] - (51 / 52 - 1)) < 1e-12
    # adjusted close: pre-split closes halved, post-split unchanged
    assert p["adj_close"].to_list() == [50.0, 51.0, 52.0, 51.0]
    # total-return index compounds ret
    assert abs(p["tr_index"][3] - (1.02 * (104 / 102) * (52 / 52))) < 1e-12
    assert p["is_last_trade"].to_list() == [False, False, False, True]
    assert p["gap_sessions"].to_list() == [None, 1, 1, 1]


def test_gap_and_implausible_dividend():
    d0, d1, d5 = SESSIONS[0], SESSIONS[1], SESSIONS[5]
    bars = _bars("S", {d0: 10.0, d1: 11.0, d5: 12.0})
    divs = pl.DataFrame({"security_id": ["S"], "date": [d5], "dividend": [9.0]})  # 82% of prev close -> dropped
    p = build_panel(
        bars, pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "split_ratio": pl.Float64}), divs, SESSIONS
    ).sort("date")
    assert p["gap_sessions"].to_list() == [None, 1, 4]
    assert p["dividend"][2] == 0.0 and abs(p["ret"][2] - (12 / 11 - 1)) < 1e-12


def test_to_wide():
    d0, d1 = SESSIONS[:2]
    bars = pl.concat([_bars("A", {d0: 1.0, d1: 2.0}), _bars("B", {d0: 5.0, d1: 4.0})])
    empty = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "split_ratio": pl.Float64})
    p = build_panel(
        bars, empty, pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "dividend": pl.Float64}), SESSIONS
    )
    w = to_wide(p, "ret")
    assert w.columns == ["date", "A", "B"] and abs(w["A"][1] - 1.0) < 1e-12 and abs(w["B"][1] + 0.2) < 1e-12


def test_suspect_split_flag():
    d0, d1, d2, d3 = SESSIONS[:4]
    bars = _bars("S", {d0: 0.10, d1: 0.09, d2: 9.5, d3: 20.0})
    bars = bars.with_columns(open=pl.Series([0.10, 0.09, 9.2, 10.5]))  # d2: level shift; d3: genuine intraday doubling
    empty_s = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "split_ratio": pl.Float64})
    empty_d = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "dividend": pl.Float64})
    p = build_panel(bars, empty_s, empty_d, SESSIONS).sort("date")
    assert p["suspect_split"].to_list() == [False, False, True, False]
    # a recorded split is not suspect
    sp = pl.DataFrame({"security_id": ["S"], "date": [d2], "split_ratio": [0.01]})
    p2 = build_panel(bars, sp, empty_d, SESSIONS).sort("date")
    assert not p2["suspect_split"][2] and abs(p2["ret"][2] - (9.5 * 0.01 / 0.09 - 1)) < 1e-12
