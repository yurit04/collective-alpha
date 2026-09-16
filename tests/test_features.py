import datetime as dt
import math

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.features.fundamentals import fundamental_features, quarterly_flows, ttm
from collective_alpha.features.news import explode_news, news_features
from collective_alpha.features.price import price_features
from collective_alpha.features.short import short_interest_features
from collective_alpha.features.signals import combine, cs_rank, cs_zscore, lag, neutralize
from collective_alpha.panel.build import build_panel

D = dt.date
SESSIONS = trading_days(D(2023, 1, 3), D(2024, 6, 28))
EMPTY_S = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "split_ratio": pl.Float64})
EMPTY_D = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "dividend": pl.Float64})


def _panel(sid: str, closes: list[float]) -> pl.DataFrame:
    days = SESSIONS[: len(closes)]
    bars = pl.DataFrame(
        {
            "security_id": [sid] * len(days),
            "date": days,
            "ticker": [sid] * len(days),
            "open": closes,
            "high": [c * 1.02 for c in closes],
            "low": [c * 0.98 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(days),
            "transactions": [10] * len(days),
        }
    )
    return build_panel(bars, EMPTY_S, EMPTY_D, SESSIONS)


def test_price_features_horizons_and_windows():
    closes = [100.0 * (1.01**i) for i in range(300)]  # +1% per session
    f = price_features(_panel("A", closes)).sort("date")
    last = f.row(-1, named=True)
    assert abs(last["ret_1d"] - 0.01) < 1e-9
    assert abs(last["ret_21d"] - (1.01**21 - 1)) < 1e-9
    assert abs(last["mom_12_1"] - (1.01**231 - 1)) < 1e-9  # sessions [t-252, t-21]
    assert abs(last["vol_21d"]) < 1e-9  # constant returns -> zero vol
    assert abs(last["parkinson_21d"] - math.sqrt(math.log(1.02 / 0.98) ** 2 / (4 * math.log(2)))) < 1e-9
    assert abs(last["dist_52w_high"]) < 1e-12  # at the high
    assert abs(last["gap_overnight"] - 0.01) < 1e-9 and abs(last["ret_intraday"]) < 1e-12
    assert last["adv_21d"] > 0 and abs(last["volume_ratio_21d"] - last["dollar_vol"] / last["adv_21d"]) < 1e-9
    # early rows have no long-horizon values
    assert f.row(5, named=True)["ret_21d"] is None and f.row(5, named=True)["vol_21d"] is None


def _fact(cik, concept, start, end, filed, val):
    return {
        "cik": cik,
        "concept": concept,
        "unit": "USD",
        "start": start,
        "end": end,
        "val": float(val),
        "filed": filed,
        "form": "10-Q",
    }


def test_quarterly_flows_and_ttm():
    # FY2023: Q1..Q3 reported in 10-Qs, FY total in the 10-K (Q4 derived), then Q1 2024
    rows = [
        _fact("1", "net_income", D(2023, 1, 1), D(2023, 3, 31), D(2023, 5, 1), 10),
        _fact("1", "net_income", D(2023, 4, 1), D(2023, 6, 30), D(2023, 8, 1), 20),
        _fact("1", "net_income", D(2023, 7, 1), D(2023, 9, 30), D(2023, 11, 1), 30),
        _fact("1", "net_income", D(2023, 1, 1), D(2023, 12, 31), D(2024, 2, 15), 100),  # FY -> Q4 = 40
        _fact("1", "net_income", D(2024, 1, 1), D(2024, 3, 31), D(2024, 5, 1), 50),
        _fact("1", "net_income", D(2023, 1, 1), D(2023, 3, 31), D(2024, 5, 1), 11),  # restatement, ignored
    ]
    facts = pl.DataFrame(rows).with_columns([pl.col(c).cast(pl.Date) for c in ("start", "end", "filed")])
    q = quarterly_flows(facts, "net_income")
    assert q["val"].to_list() == [10.0, 20.0, 30.0, 40.0, 50.0]
    assert q.filter(pl.col("end") == D(2023, 12, 31))["filed"][0] == D(2024, 2, 15)
    t = ttm(q)
    assert t["ttm"].to_list() == [100.0, 140.0]
    assert t["filed"].to_list() == [D(2024, 2, 15), D(2024, 5, 1)]  # available when the 4th quarter is filed
    keys = pl.DataFrame(
        {
            "security_id": ["S", "S", "S"],
            "date": [D(2024, 2, 14), D(2024, 2, 15), D(2024, 5, 2)],
            "cik": ["1", "1", "1"],
        }
    )
    ff = fundamental_features(facts, keys)
    assert ff["net_income_ttm"].to_list() == [None, 100.0, 140.0]


def test_short_interest_lag_and_ratio():
    si = pl.DataFrame(
        {
            "settlement_date": ["2024-01-31", "2024-02-15"],
            "ticker": ["A", "A"],
            "short_interest": [100, 150],
            "avg_daily_volume": [10, 10],
            "days_to_cover": [10.0, 15.0],
        }
    )
    master = pl.DataFrame(
        {"ticker": ["A"], "security_id": ["S"], "valid_from": [D(2023, 1, 3)], "valid_to": [D(2024, 12, 31)]}
    )
    keys = pl.DataFrame(
        {"security_id": ["S"] * 3, "date": [D(2024, 2, 9), D(2024, 2, 12), D(2024, 2, 26)], "shares": [1000.0] * 3}
    )
    out = short_interest_features(si, master, keys).sort("date")
    # 2024-01-31 settlement becomes known on 2024-02-10 -> not on Feb 9, yes on Feb 12
    assert out["si_shares"].to_list() == [None, 100.0, 150.0]
    assert out["si_ratio"].to_list()[1:] == [0.1, 0.15]
    assert out["si_change"].to_list()[2] == 0.5


def test_news_attribution_and_sentiment():
    news = pl.DataFrame(
        {
            "id": ["a", "b", "c"],
            "published_utc": [
                "2024-03-04T15:00:00Z",
                "2024-03-04T21:30:00Z",
                "2024-03-09T12:00:00Z",
            ],  # 10:00 NY Mon, 16:30 NY Mon, Sat
            "tickers": ['["A","B"]', '["A"]', '["A"]'],
            "insights": [
                '[{"ticker":"A","sentiment":"positive"},{"ticker":"B","sentiment":"negative"}]',
                None,
                '[{"ticker":"A","sentiment":"negative"}]',
            ],
        }
    )
    ex = explode_news(news, SESSIONS)
    a = ex.filter(pl.col("ticker") == "A").sort("id")
    assert a["date"].to_list() == [
        D(2024, 3, 4),
        D(2024, 3, 5),
        D(2024, 3, 11),
    ]  # after-close -> next session; weekend -> Monday
    assert a["sentiment"].to_list() == [1.0, None, -1.0]
    master = pl.DataFrame(
        {
            "ticker": ["A", "B"],
            "security_id": ["SA", "SB"],
            "valid_from": [D(2023, 1, 3)] * 2,
            "valid_to": [D(2024, 12, 31)] * 2,
        }
    )
    keys = pl.DataFrame(
        {
            "security_id": ["SA"] * 6,
            "date": [D(2024, 3, 4), D(2024, 3, 5), D(2024, 3, 6), D(2024, 3, 7), D(2024, 3, 8), D(2024, 3, 11)],
        }
    )
    f = news_features(news, master, keys, SESSIONS).sort("date")
    assert f["news_count"].to_list() == [1, 1, 0, 0, 0, 1]
    assert f["news_count_5d"].to_list() == [1, 2, 2, 2, 2, 2]
    assert f["news_sent_5d"].to_list()[0] == 1.0 and f["news_sent_5d"].to_list()[5] == -1.0


def test_signal_transforms():
    df = pl.DataFrame(
        {
            "security_id": ["A", "B", "C", "D"] * 2,
            "date": [D(2024, 1, 2)] * 4 + [D(2024, 1, 3)] * 4,
            "x": [1.0, 2.0, 3.0, None, 4.0, 3.0, 2.0, 1.0],
            "y": [10.0, 10.0, 0.0, 0.0, 1.0, 2.0, 3.0, 4.0],
            "sector": ["s1", "s1", "s2", "s2"] * 2,
        }
    )
    r = cs_rank(df, "x").sort(["date", "security_id"])
    assert r["x_rank"].to_list()[:4] == [1 / 3, 2 / 3, 1.0, None]
    z = cs_zscore(df, "x", winsor=None).sort(["date", "security_id"])
    assert abs(z["x_z"].to_list()[1]) < 1e-12  # middle value of 1,2,3
    n = neutralize(df, "y", "sector").sort(["date", "security_id"])
    assert n["y_n"].to_list()[:4] == [0.0, 0.0, 0.0, 0.0]  # y is constant within sector on day 1
    c = combine(df, {"x": 1.0, "y": -1.0}).sort(["date", "security_id"])
    assert c.filter(pl.col("security_id") == "D")["signal"].to_list()[0] is not None  # x null -> y alone
    lg = lag(df, "x", 1).sort(["security_id", "date"])
    assert lg.filter(pl.col("security_id") == "A")["x_lag1"].to_list() == [None, 1.0]


def test_quarterly_flows_from_ytd_periods():
    # cash-flow style: 3M, 6M, 9M year-to-date in 10-Qs, 12M in the 10-K
    rows = [
        _fact("1", "cfo", D(2023, 1, 1), D(2023, 3, 31), D(2023, 5, 1), 10),
        _fact("1", "cfo", D(2023, 1, 1), D(2023, 6, 30), D(2023, 8, 1), 30),
        _fact("1", "cfo", D(2023, 1, 1), D(2023, 9, 30), D(2023, 11, 1), 60),
        _fact("1", "cfo", D(2023, 1, 1), D(2023, 12, 31), D(2024, 2, 15), 100),
        _fact("1", "cfo", D(2024, 1, 1), D(2024, 3, 31), D(2024, 5, 1), 15),
    ]
    facts = pl.DataFrame(rows).with_columns([pl.col(c).cast(pl.Date) for c in ("start", "end", "filed")])
    q = quarterly_flows(facts, "cfo")
    assert q["val"].to_list() == [10.0, 20.0, 30.0, 40.0, 15.0]
    assert q["filed"].to_list() == [D(2023, 5, 1), D(2023, 8, 1), D(2023, 11, 1), D(2024, 2, 15), D(2024, 5, 1)]
    assert ttm(q)["ttm"].to_list() == [100.0, 105.0]


def _intraday_frame(sid: str, n: int, **cols) -> pl.DataFrame:
    days = SESSIONS[:n]
    base = {
        "security_id": [sid] * n,
        "date": days,
        "rv_5m": [0.01] * n,
        "spread_est": [0.0005] * n,
        "bar_coverage": [1.0] * n,
        "close_to_vwap": [0.0] * n,
        "share_open30": [0.2] * n,
        "share_close30": [0.2] * n,
        "share_auction": [0.01] * n,
        "share_pre": [0.02] * n,
        "share_post": [0.02] * n,
        "or_range_pct": [0.01] * n,
        "close_vs_or": [0.5] * n,
        "ret_open30": [0.001] * n,
        "ret_close30": [0.001] * n,
    }
    base.update({k: (v if isinstance(v, list) else [v] * n) for k, v in cols.items()})
    return pl.DataFrame(base)


def test_intraday_features_rolling_and_ratio():
    from collective_alpha.features.intraday import intraday_features

    n = 40
    rv = [0.01] * (n - 1) + [0.05]  # a volatility spike on the last day
    intra = _intraday_frame("S", n, rv_5m=rv)
    panel = _panel("S", [100.0 * (1.001**i) for i in range(n)]).select(
        "security_id", "date", "open", "close", "prev_close", "split_ratio"
    )
    f = intraday_features(intra, panel).sort("date")
    last = f.row(-1, named=True)
    assert abs(last["rv_21d"] - (20 * 0.01 + 0.05) / 21) < 1e-12
    assert last["rv_ratio"] > 4  # the spike shows up against its own trailing level
    assert abs(last["spread_21d"] - 0.0005) < 1e-12
    early = f.row(2, named=True)
    assert early["rv_21d"] is None  # not enough history yet


def test_overnight_intraday_split_is_split_adjusted():
    from collective_alpha.features.intraday import intraday_features

    n = 30
    closes = [100.0] * n
    panel = _panel("S", closes).select("security_id", "date", "open", "close", "prev_close", "split_ratio")
    # a 2-for-1 on day 10: the panel's raw open halves, and the split ratio must undo it
    idx = 10
    panel = panel.with_columns(
        open=pl.when(pl.int_range(pl.len()) >= idx).then(50.0).otherwise(100.0),
        close=pl.when(pl.int_range(pl.len()) >= idx).then(50.0).otherwise(100.0),
        prev_close=pl.when(pl.int_range(pl.len()) > idx).then(50.0).otherwise(100.0),
        split_ratio=pl.when(pl.int_range(pl.len()) == idx).then(2.0).otherwise(1.0),
    )
    f = intraday_features(_intraday_frame("S", n), panel).sort("date")
    on = f["overnight"].to_list()
    assert abs(on[idx]) < 1e-12  # split day: no spurious -50% overnight gap
    assert all(abs(x) < 1e-12 for x in on[1:] if x is not None)
