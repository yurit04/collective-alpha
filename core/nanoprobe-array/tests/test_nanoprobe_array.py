"""Offline tests on a synthetic market: three sessions (one early close), a liquid name A with a
dividend, a sparse name B with a 2-for-1 split, and pre-market prints."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from nanoprobe_array import (
    FEATURES,
    Nanoprobe,
    clock,
    daily_prices,
    features,
    grid,
    parse_freq,
    resample,
    summarize,
)

D = date
UTC = UTC
DAYS = [D(2024, 11, 26), D(2024, 11, 27), D(2024, 11, 29)]          # 11-29 closes at 13:00 ET
OPEN = {d: datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC) for d in DAYS}
CLOSE = {d: datetime(d.year, d.month, d.day, 18 if d == D(2024, 11, 29) else 21, 0, tzinfo=UTC) for d in DAYS}
SESSIONS = pl.DataFrame({"date": DAYS, "market_open": [OPEN[d] for d in DAYS],
                         "market_close": [CLOSE[d] for d in DAYS]},
                        schema={"date": pl.Date, "market_open": pl.Datetime("ns", "UTC"),
                                "market_close": pl.Datetime("ns", "UTC")})


def make_minutes() -> pl.DataFrame:
    rows = []
    for k, d in enumerate(DAYS):
        n = int((CLOSE[d] - OPEN[d]).total_seconds() // 60)
        base = 100.0 + 5 * k
        rows.append(("A", "A", d, OPEN[d] - timedelta(hours=1), base, base, base, base, 50.0, 1))  # pre-market
        for i in range(n):
            p = base + 0.01 * i
            rows.append(("A", "A", d, OPEN[d] + timedelta(minutes=i), p, p + 0.02, p - 0.02, p + 0.01, 100.0 + i, 10))
        split = 0.5 if d >= D(2024, 11, 27) else 1.0                  # B splits 2-for-1 on 11-27
        for i in range(0, n, 7):                                      # B prints every 7th minute
            p = 40.0 * split + 0.05 * i * split
            rows.append(("BB", "B", d, OPEN[d] + timedelta(minutes=i), p, p, p, p, 20.0, 1))
    return pl.DataFrame(rows, orient="row", schema={
        "ticker": pl.String, "security_id": pl.String, "date": pl.Date, "timestamp": pl.Datetime("ns", "UTC"),
        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64,
        "transactions": pl.Int64})


MINUTES = make_minutes()


def make_daily() -> pl.DataFrame:
    """Adjusted daily bars as Store.bars(adjust='all') returns them (prices x factors, volume / split)."""
    reg = MINUTES.join(SESSIONS, on="date").filter(pl.col("timestamp") >= pl.col("market_open")).sort("timestamp")
    d = reg.group_by("ticker", "security_id", "date").agg(
        pl.col("open").first(), pl.col("high").max(), pl.col("low").min(),
        (pl.col("close").last() * 1.001).alias("close"), (pl.col("volume").sum() * 1.25).alias("volume"))
    split = pl.when((pl.col("security_id") == "B") & (pl.col("date") < D(2024, 11, 27))).then(0.5).otherwise(1.0)
    div = pl.when((pl.col("security_id") == "A") & (pl.col("date") < D(2024, 11, 29))).then(0.99).otherwise(1.0)
    d = d.with_columns(split_factor=split, div_factor=div)
    f = pl.col("split_factor") * pl.col("div_factor")
    return (d.with_columns([(pl.col(c) * f).alias(c) for c in ("open", "high", "low", "close")])
            .with_columns(volume=pl.col("volume") / pl.col("split_factor")).sort("security_id", "date"))


DAILY = make_daily()


class FakeStore:
    root = "/nonexistent"

    def bars(self, tickers=None, start=None, end=None, freq="1d", adjust=None, session="all", by="ticker",
             include_live=False, **_):
        src = MINUTES if freq == "1min" else DAILY
        out = src.filter(pl.col("date").is_between(start, end))
        if tickers:
            out = out.filter(pl.col("ticker").is_in(list(tickers)) | pl.col("security_id").is_in(list(tickers)))
        return out

    def sessions(self, start, end=None):
        return SESSIONS.filter(pl.col("date").is_between(start, end or start))

    def trading_days(self, start, end=None):
        return [d for d in DAYS if start <= d <= (end or start)]

    def coverage(self):
        return pl.DataFrame({"dataset": ["bars_minute"], "first": [DAYS[0].isoformat()], "last": [DAYS[-1].isoformat()]})


# ---------------------------------------------------------------------------- clock
def test_parse_freq():
    assert parse_freq("5min") == 5 and parse_freq("15m") == 15 and parse_freq("1h") == 60 and parse_freq(30) == 30
    for bad in ("0min", "391min", "5s", True, "abc"):
        with pytest.raises(ValueError):
            parse_freq(bad)


def test_clock_anchored_at_open_and_ends_at_close():
    c = clock(SESSIONS, "5min")
    per_day = dict(c.group_by("date").len().iter_rows())
    assert per_day == {DAYS[0]: 78, DAYS[1]: 78, DAYS[2]: 42}          # 390 and 210 minutes
    early = c.filter(pl.col("date") == DAYS[2])
    assert early["bar_end"].max() == CLOSE[DAYS[2]] and early.filter("last").height == 1
    assert early.filter("last")["minutes_to_close"][0] == 0
    hourly = clock(SESSIONS.head(1), "1h")                               # 6.5 hours -> 7 bars, last 30 min
    assert hourly.height == 7
    last = hourly.row(6, named=True)
    assert (last["bar_end"] - last["bar_start"]) == timedelta(minutes=30) and last["minutes_from_open"] == 390


# ---------------------------------------------------------------------------- bars
def test_resample_aggregates_regular_minutes_labelled_by_end():
    b = resample(MINUTES, SESSIONS, 5).filter((pl.col("security_id") == "A") & (pl.col("date") == DAYS[0]))
    assert b.height == 78
    first = b.row(0, named=True)
    assert first["bar_start"] == OPEN[DAYS[0]] and first["bar_end"] == OPEN[DAYS[0]] + timedelta(minutes=5)
    assert first["open"] == 100.0 and first["close"] == pytest.approx(100.05)       # minute 4 close
    assert first["high"] == pytest.approx(100.06) and first["low"] == pytest.approx(99.98)
    assert first["volume"] == sum(100.0 + i for i in range(5)) and first["minutes"] == 5
    assert first["transactions"] == 50
    assert first["last_trade"] == OPEN[DAYS[0]] + timedelta(minutes=4)
    tp = [((100 + 0.01 * i + 0.02) + (100 + 0.01 * i - 0.02) + (100 + 0.01 * i + 0.01)) / 3 for i in range(5)]
    assert first["vwap"] == pytest.approx(sum(t * (100 + i) for i, t in enumerate(tp)) / first["volume"])
    assert b["volume"].sum() == sum(100.0 + i for i in range(390))     # pre-market print dropped


def test_grid_marks_empty_bars_and_carries_within_the_day_only():
    b = resample(MINUTES, SESSIONS, 5)
    g = grid(b, SESSIONS, 5).filter(pl.col("security_id") == "B")
    assert g.height == 78 + 78 + 42
    day1 = g.filter(pl.col("date") == DAYS[0]).sort("bar")
    # B prints at minutes 0, 7, 14, 21, ...: bar 3 (minutes 15-19) has none
    assert not day1.filter(pl.col("bar") == 2)["stale"][0] and day1.filter(pl.col("bar") == 3)["stale"][0]
    stale = day1.filter("stale")
    assert (stale["volume"] == 0).all() and stale["vwap"].is_null().all()
    for r in stale.iter_rows(named=True):            # carried close = previous bar's close
        prev = day1.filter(pl.col("bar") == r["bar"] - 1).row(0, named=True)
        assert r["close"] == prev["close"] == r["open"] == r["high"] == r["low"]
    # explicit ids: a security with no prints that day has null prices, never yesterday's close
    g2 = grid(b.filter(pl.col("date") != DAYS[1]), SESSIONS, 5, security_ids=["B"])
    mid = g2.filter(pl.col("date") == DAYS[1])
    assert mid.height == 78 and mid["close"].is_null().all() and mid["stale"].all()


# ---------------------------------------------------------------------------- features
def test_features_never_look_ahead():
    """Changing everything after bar k leaves every feature at bars <= k unchanged."""
    k = 20
    cut = OPEN[DAYS[1]] + timedelta(minutes=5 * (k + 1))
    later = (pl.col("timestamp") >= cut) & (pl.col("date") == DAYS[1])
    noisy = MINUTES.with_columns([pl.when(later).then(pl.col(c) * 1.7).otherwise(pl.col(c)).alias(c)
                                  for c in ("open", "high", "low", "close", "volume")])
    names = [n for n in FEATURES if FEATURES[n].needs != "prev_close"]

    def run(m):
        g = grid(resample(m, SESSIONS, 5), SESSIONS, 5)
        return features(g, names, or_minutes=30, lookback=1).filter(
            (pl.col("date") == DAYS[1]) & (pl.col("bar") <= k)).sort("security_id", "bar")

    a, b = run(MINUTES), run(noisy)
    for n in names:
        x, y = a[n].to_list(), b[n].to_list()
        assert [v is None for v in x] == [v is None for v in y], n
        assert [v for v in x if v is not None] == pytest.approx([v for v in y if v is not None], rel=1e-12), n


def test_first_open_features_wait_for_the_first_print():
    """gap and ret_open use the day's first open only from the bar that printed it: a name with
    no prints in its first bars must not see a later open there."""
    n = None
    b = pl.DataFrame({"security_id": ["A"] * 4, "date": [DAYS[0]] * 4, "bar": [0, 1, 2, 3],
                      "minutes_from_open": [5, 10, 15, 20], "open": [n, n, 10.0, 10.5],
                      "high": [n, n, 10.2, 10.6], "low": [n, n, 9.9, 10.4], "close": [n, n, 10.1, 10.5],
                      "volume": [0.0, 0.0, 100.0, 100.0], "dollar_volume": [0.0, 0.0, 1010.0, 1050.0],
                      "prev_close": [9.0] * 4})
    f = features(b, ["gap", "ret_open"]).sort("bar")
    assert f["gap"][:2].is_null().all() and f["ret_open"][:2].is_null().all()
    assert f["gap"][2] == pytest.approx(10.0 / 9.0 - 1) and f["gap"][3] == pytest.approx(10.0 / 9.0 - 1)
    assert f["ret_open"][3] == pytest.approx(10.5 / 10.0 - 1)


def test_feature_values():
    g = grid(resample(MINUTES, SESSIONS, 5), SESSIONS, 5)
    f = features(g, ["ret_bar", "ret_open", "rvol", "or_high", "or_break", "range_pos"], or_minutes=30)
    a = f.filter((pl.col("security_id") == "A") & (pl.col("date") == DAYS[0])).sort("bar")
    assert a["ret_bar"][0] == pytest.approx(100.05 / 100.0 - 1)
    assert a["ret_bar"][1] == pytest.approx(100.10 / 100.05 - 1)
    assert a["ret_open"][3] == pytest.approx(a["close"][3] / 100.0 - 1)
    assert a["rvol"][1] == pytest.approx(math.sqrt(math.log(100.05 / 100) ** 2 + math.log(100.10 / 100.05) ** 2))
    assert a["or_high"][:5].is_null().all() and a["or_break"][:5].is_null().all()   # range ends at bar 5
    assert a["or_high"][5] == pytest.approx(100.0 + 0.01 * 29 + 0.02)
    assert a["or_break"][6] == 1                                   # A rises steadily
    assert a["range_pos"][10] == pytest.approx((a["close"][10] - 99.98) / (a["high"][10] - 99.98))


def test_vol_ratio_uses_earlier_days_only():
    g = grid(resample(MINUTES, SESSIONS, 5), SESSIONS, 5)
    f = features(g, ["vol_ratio"], lookback=20)                    # needs 5 earlier days: none here
    assert f["vol_ratio"].is_null().all()
    # history passed in: four copies of day 1 as earlier days + today -> ratio 1 when identical
    day = g.filter((pl.col("date") == DAYS[0]) & (pl.col("security_id") == "A"))
    hist = pl.concat([day.with_columns(date=pl.lit(D(2024, 11, 1 + i))) for i in range(5)])
    today = day.with_columns(date=pl.lit(D(2024, 11, 26)))
    r = features(today, ["vol_ratio"], lookback=20, history=hist)
    assert r.height == today.height and r["vol_ratio"].to_list() == pytest.approx([1.0] * r.height)


def test_prev_close_features_need_the_column():
    g = grid(resample(MINUTES, SESSIONS, 5), SESSIONS, 5)
    with pytest.raises(ValueError, match="prev_close"):
        features(g, ["gap"])
    assert "gap" not in features(g).columns                        # default skips them silently
    with pytest.raises(ValueError, match="unknown"):
        features(g, ["nope"])


# ---------------------------------------------------------------------------- daily
def test_daily_prices_put_prev_close_on_todays_basis():
    d = daily_prices(DAILY)
    b = d.filter(pl.col("security_id") == "B").sort("date")
    raw = DAILY.filter(pl.col("security_id") == "B").sort("date")
    raw_close0 = raw["close"][0] / 0.5                              # undo the stored adjustment
    assert b["close"][0] == pytest.approx(raw_close0)
    assert b["prev_close"][1] == pytest.approx(raw_close0 / 2)       # split day: half yesterday's close
    a = d.filter(pl.col("security_id") == "A").sort("date")
    a_raw_close1 = DAILY.filter(pl.col("security_id") == "A").sort("date")["close"][1] / 0.99
    assert a["prev_close"][2] == pytest.approx(a_raw_close1 * 0.99)  # dividend taken out
    assert a["prev_close"][0] is None
    raw_volume = MINUTES.filter(pl.col("security_id") == "B").group_by("date").agg(pl.col("volume").sum() * 1.25).sort("date")
    assert b["volume"].to_list() == pytest.approx(raw_volume["volume"].to_list())   # raw shares, before the split too


def test_summarize():
    s = summarize(MINUTES, SESSIONS, daily_prices(DAILY)).filter(
        (pl.col("security_id") == "A") & (pl.col("date") == DAYS[0])).row(0, named=True)
    last_minute = 100.0 + 0.01 * 389 + 0.01
    assert s["open"] == 100.0 and s["close"] == pytest.approx(last_minute * 1.001)
    assert s["minute_volume"] == sum(100.0 + i for i in range(390)) and s["minutes_traded"] == 390
    assert s["auction_share"] == pytest.approx(1 - 1 / 1.25)
    assert s["ret_close_auction"] == pytest.approx(0.001)
    assert s["ret_first30"] == pytest.approx((100.0 + 0.01 * 29 + 0.01) / 100.0 - 1)
    assert s["ret_last30"] == pytest.approx(s["close"] / (100.0 + 0.01 * 359 + 0.01) - 1)
    assert s["bars_5min"] == 78 and s["rv_5min"] > 0 and s["ret_overnight"] is None
    b = summarize(MINUTES, SESSIONS, daily_prices(DAILY)).filter(pl.col("security_id") == "B")
    assert b["auction_share"].to_list() == pytest.approx([1 - 1 / 1.25] * b.height)   # split days included


# ---------------------------------------------------------------------------- Nanoprobe
def test_nanoprobe_bars_days_auctions(tmp_path):
    p = Nanoprobe(root=tmp_path, store=FakeStore())
    b = p.bars("A", DAYS[0], DAYS[2], freq="15min", fill="grid", prev_close=True, features=["gap", "ret_prev"])
    assert set(b["date"].unique()) == set(DAYS) and b.filter(pl.col("date") == DAYS[2]).height == 14
    day2 = b.filter(pl.col("date") == DAYS[1]).sort("bar")
    assert day2["gap"][0] == pytest.approx(105.0 / day2["prev_close"][0] - 1)
    with pytest.raises(ValueError, match="days"):
        p.bars(None, DAYS[0])
    with pytest.raises(ValueError, match="grid"):
        p.bars("A", DAYS[0], features=["vol_ratio"])
    got = list(p.days(DAYS[1], DAYS[2], tickers=["A", "BB"], features=list(FEATURES), prev_close=True))
    assert [d for d, _ in got] == DAYS[1:]
    assert set(got[0][1]["security_id"].unique()) == {"A", "B"}
    a = p.auctions("A", DAYS[0], DAYS[2])
    assert {"open_auction", "close_auction", "prev_close"} <= set(a.columns) and a.height == 3


def test_build_skips_a_finished_year_ending_on_a_weekend(tmp_path):
    """2023 ended on a Sunday: a year stored through its last session (12-29) is complete and
    must not be rebuilt every night."""
    last_2023 = D(2023, 12, 29)

    class TwoYears(FakeStore):
        def trading_days(self, start, end=None):
            return [d for d in [last_2023, *DAYS] if start <= d <= (end or start)]

        def coverage(self):
            return pl.DataFrame({"dataset": ["bars_minute"], "first": [last_2023.isoformat()],
                                 "last": [DAYS[-1].isoformat()]})

    (tmp_path / "summaries").mkdir()
    pl.DataFrame({"date": [last_2023]}).write_parquet(tmp_path / "summaries" / "year=2023.parquet")
    (tmp_path / "build.json").write_text(json.dumps({"years": {"2023": {
        "rows": 1, "days": 1, "first_date": "2023-12-29", "last_date": "2023-12-29", "built_at": "x"}}}))
    p = Nanoprobe(root=tmp_path, store=TwoYears())
    assert p.build(workers=1)["years"] == [2024]
    assert p.build(workers=1)["years"] == []


def test_build_and_summaries(tmp_path):
    p = Nanoprobe(root=tmp_path, store=FakeStore())
    with pytest.raises(FileNotFoundError):
        p.summaries()
    info = p.build(workers=1)
    assert info["years"] == [2024] and info["days"] == 3 and info["rows"] == 6
    s = p.summaries("A")
    assert s.height == 3 and s["ret_overnight"].null_count() == 1
    st = p.status()
    assert st["last_date"] == DAYS[-1].isoformat() and st["behind"] is False
    assert p.build(workers=1)["years"] == []                        # nothing new: nothing rebuilt
