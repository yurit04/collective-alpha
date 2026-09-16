import datetime as dt
import math

import polars as pl

from collective_alpha.intraday.aggregate import aggregate_session
from collective_alpha.intraday.session import minute_of_day, session_bounds

D = dt.date
DEFAULT_DAY = dt.date(2026, 9, 11)


def test_session_bounds_regular_and_half_day():
    b = session_bounds(D(2026, 9, 11))
    assert (b.open_min, b.close_min, b.half_day, b.n_minutes) == (570, 960, False, 390)
    assert b.open_window_end == 600 and b.close_window_start == 930
    h = session_bounds(D(2025, 11, 28))  # day after Thanksgiving: 13:00 close
    assert (h.open_min, h.close_min, h.half_day, h.n_minutes) == (570, 780, True, 210)
    assert h.close_window_start == 750
    assert session_bounds(D(2026, 9, 12)) is None  # Saturday


def test_minute_of_day_does_not_overflow():
    """hour and minute are 8-bit: `hour * 60` silently wraps unless cast first."""
    df = pl.DataFrame({"ts_ny": [dt.datetime(2026, 9, 11, h, 30) for h in (4, 9, 15, 19)]})
    assert df.select(minute_of_day())["ts_ny"].to_list() == [270, 570, 930, 1170]


def _minute_bars(ticker: str, minutes: list[int], price, volume=100.0, date=None) -> pl.DataFrame:
    date = date or DEFAULT_DAY
    prices = [price(m) for m in minutes]
    return pl.DataFrame(
        {
            "ticker": [ticker] * len(minutes),
            "ts_ny": [dt.datetime(date.year, date.month, date.day, m // 60, m % 60) for m in minutes],
            "open": prices,
            "high": [p * 1.001 for p in prices],
            "low": [p * 0.999 for p in prices],
            "close": prices,
            "volume": [volume] * len(minutes),
            "transactions": [5] * len(minutes),
        }
    )


def test_aggregate_regular_session_fields():
    b = session_bounds(D(2026, 9, 11))
    # a dense name: every regular minute, price rising 0.01% per minute, plus pre and post bars
    reg = list(range(570, 960))
    bars = _minute_bars("DENSE", reg, lambda m: 100.0 * (1.0001 ** (m - 570)))
    extra = _minute_bars("DENSE", [500, 1000], lambda m: 100.0, volume=50.0)
    out = aggregate_session(pl.concat([bars, extra]), b)
    r = out.filter(pl.col("ticker") == "DENSE").row(0, named=True)
    assert r["n_bars"] == 390 and r["bar_coverage"] == 1.0 and not r["half_day"]
    assert r["first_bar_min"] == 570 and r["last_bar_min"] == 959
    assert abs(r["open_reg"] - 100.0) < 1e-9
    assert r["volume_reg"] == 390 * 100.0 and r["volume_pre"] == 50.0 and r["volume_post"] == 50.0
    assert abs(r["volume_open30"] - 30 * 100.0) < 1e-9 and abs(r["volume_close30"] - 30 * 100.0) < 1e-9
    assert abs(r["share_open30"] - 30 / 390) < 1e-9
    # rising price: close above vwap, close at the top of the opening range
    assert r["close_to_vwap"] > 0 and r["close_vs_or"] > 1
    # realised vol: 389 identical log returns of 1e-4
    step = math.log(1.0001)
    assert abs(r["rv_1m"] - (389**0.5) * step) < 1e-12
    assert abs(r["max_abs_1m_ret"] - step) < 1e-12
    assert abs(r["rv_5m"] - (77**0.5) * 5 * step) < 1e-12  # 78 buckets -> 77 returns of five minutes
    assert abs(r["hl_range_mean"] - math.log(1.001 / 0.999)) < 1e-12  # mean log high/low
    assert r["ret_open30"] > 0 and r["ret_mid"] > 0 and r["ret_close30"] > 0
    assert r["dollar_vol_reg"] > 0 and r["transactions_reg"] == 390 * 5


def test_sparse_names_get_null_estimators():
    b = session_bounds(D(2026, 9, 11))
    sparse = _minute_bars("SPARSE", [600, 700, 800], lambda m: 10.0)
    # one bar every four minutes: enough for shape and five-minute vol, too sparse for one-minute vol
    medium = _minute_bars("MED", list(range(600, 760, 4)), lambda m: 10.0)
    out = aggregate_session(pl.concat([sparse, medium]), b).sort("ticker")
    med = out.filter(pl.col("ticker") == "MED").row(0, named=True)
    sp = out.filter(pl.col("ticker") == "SPARSE").row(0, named=True)
    assert sp["n_bars"] == 3 and sp["rv_1m"] is None and sp["rv_5m"] is None and sp["share_open30"] is None
    assert med["n_bars"] == 40 and med["rv_1m"] is None and med["rv_5m"] is not None and med["share_open30"] is not None
    # volume totals survive for every name, however sparse
    assert sp["volume_reg"] == 300.0


def test_half_day_windows():
    b = session_bounds(D(2025, 11, 28))
    bars = _minute_bars("A", list(range(570, 780)), lambda m: 50.0, date=D(2025, 11, 28))
    r = aggregate_session(bars, b).row(0, named=True)
    assert r["half_day"] and r["n_bars"] == 210 and r["bar_coverage"] == 1.0
    assert r["last_bar_min"] == 779
    # the closing window is the last 30 minutes of the *early* close
    assert abs(r["volume_close30"] - 30 * 100.0) < 1e-9


def test_closing_auction_is_separated_from_post_market():
    """The bar stamped at the close carries the auction: it must not count as post-market, and the
    official close comes from it."""
    b = session_bounds(DEFAULT_DAY)
    bars = pl.concat(
        [
            _minute_bars("A", list(range(570, 960)), lambda m: 20.0),
            _minute_bars("A", [960], lambda m: 20.5, volume=5000.0),  # 16:00 auction
            _minute_bars("A", [961, 1100], lambda m: 21.0, volume=100.0),  # genuine post-market
        ]
    )
    r = aggregate_session(bars, b).row(0, named=True)
    assert r["close_reg"] == 20.0 and r["auction_close"] == 20.5 and r["close_final"] == 20.5
    assert r["auction_volume"] == 5000.0 and r["volume_post"] == 200.0
    assert abs(r["share_auction"] - 5000.0 / (390 * 100.0 + 5000.0)) < 1e-12
    # a name with no auction print falls back to the last continuous close
    only_reg = aggregate_session(_minute_bars("B", list(range(570, 960)), lambda m: 7.0), b).row(0, named=True)
    assert only_reg["auction_close"] is None and only_reg["close_final"] == 7.0 and only_reg["auction_volume"] == 0.0


def test_bars_outside_the_session_are_excluded_from_regular_stats():
    b = session_bounds(D(2026, 9, 11))
    bars = pl.concat(
        [
            _minute_bars("X", list(range(570, 960)), lambda m: 20.0),
            _minute_bars("X", [960, 1100], lambda m: 25.0, volume=999.0),  # after the close
        ]
    )
    r = aggregate_session(bars, b).row(0, named=True)
    assert r["close_reg"] == 20.0 and r["high_reg"] < 21
    assert r["auction_volume"] == 999.0 and r["volume_post"] == 999.0  # 16:00 is the auction, 18:20 is post
    assert r["share_post"] > 0 and abs(r["share_pre"]) < 1e-12
