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
    """The bar stamped at the close carries the auction: it must not count as post-market, and its
    first print (not its close, which can be an after-hours price) is the auction price."""
    b = session_bounds(DEFAULT_DAY)
    bars = pl.concat(
        [
            _minute_bars("A", list(range(570, 960)), lambda m: 20.0),
            _minute_bars("A", [960], lambda m: 20.5, volume=5000.0),  # 16:00 auction
            _minute_bars("A", [961, 1100], lambda m: 21.0, volume=100.0),  # genuine post-market
        ]
    )
    r = aggregate_session(bars, b).row(0, named=True)
    assert r["close_reg"] == 20.0 and r["auction_price"] == 20.5
    assert r["auction_volume"] == 5000.0 and r["volume_post"] == 200.0
    assert abs(r["share_auction"] - 5000.0 / (390 * 100.0 + 5000.0)) < 1e-12
    # a name with no auction print falls back to the last continuous close
    only_reg = aggregate_session(_minute_bars("B", list(range(570, 960)), lambda m: 7.0), b).row(0, named=True)
    assert only_reg["auction_price"] is None and only_reg["auction_volume"] == 0.0


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


def test_spread_estimators_recover_a_known_spread():
    """Bars simulated from a random walk seen through a bid-ask bounce: both estimators should come
    back near the spread that generated them, and Abdi-Ranaldo should be the more accurate."""
    import numpy as np

    from collective_alpha.intraday.spread import abdi_ranaldo, corwin_schultz, simulate_bars

    for true_bp in (10, 50):
        cs, ar = [], []
        for seed in range(15):
            h, low, c = simulate_bars(390, true_bp / 1e4, seed=seed)
            cs.append(corwin_schultz(h, low))
            ar.append(abdi_ranaldo(h, low, c))
        true = true_bp / 1e4
        assert abs(np.mean(ar) - true) < 0.25 * true
        assert abs(np.mean(cs) - true) < 0.40 * true
    # a market with no spread at all must not manufacture one
    h, low, c = simulate_bars(390, 0.0, seed=1)
    assert corwin_schultz(h, low) < 5e-4 and abdi_ranaldo(h, low, c) < 5e-4


def test_spread_expressions_match_the_reference_implementation():
    import numpy as np

    from collective_alpha.intraday.spread import (
        abdi_ranaldo,
        abdi_ranaldo_expr,
        corwin_schultz,
        corwin_schultz_expr,
        simulate_bars,
    )

    h, low, c = simulate_bars(200, 30 / 1e4, seed=3)
    df = pl.DataFrame({"g": ["x"] * len(h), "high": h, "low": low, "close": c})
    out = df.group_by("g").agg(cs=corwin_schultz_expr(), ar=abdi_ranaldo_expr()).row(0, named=True)
    assert abs(out["cs"] - corwin_schultz(h, low)) < 1e-12
    assert abs(out["ar"] - abdi_ranaldo(h, low, c)) < 1e-12
    assert np.isfinite(out["cs"]) and np.isfinite(out["ar"])


def test_spread_null_for_sparse_names():
    b = session_bounds(DEFAULT_DAY)
    sparse = _minute_bars("S", list(range(600, 640)), lambda m: 10.0)  # 40 bars < 60
    dense = _minute_bars("D", list(range(570, 960)), lambda m: 10.0 + (m % 3) * 0.01)
    out = aggregate_session(pl.concat([sparse, dense]), b).sort("ticker")
    assert out.filter(pl.col("ticker") == "S")["spread_ar"][0] is None
    assert out.filter(pl.col("ticker") == "D")["spread_ar"][0] is not None


def test_spread_estimate_is_floored_at_one_tick():
    from collective_alpha.intraday.spread import spread_estimate_expr

    df = pl.DataFrame(
        {
            "spread_ar": [0.0, 0.00039, 0.0030, 0.0025],  # KO-like zero, Ford-like, wide, cheap name
            "close_reg": [79.48, 13.83, 100.0, 0.50],
        }
    )
    est = df.with_columns(est=spread_estimate_expr())["est"].to_list()
    assert abs(est[0] - 0.01 / 79.48) < 1e-12  # floored at one tick
    assert abs(est[1] - 0.01 / 13.83) < 1e-12  # floored: below the tick
    assert abs(est[2] - 0.0030) < 1e-12  # estimator wins when it exceeds the tick
    assert abs(est[3] - 0.0025) < 1e-12  # sub-dollar name: tick is a hundredth of a cent
