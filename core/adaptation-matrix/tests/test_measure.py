"""Measures on hand-built ticks whose answers can be worked out on paper."""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from adaptation_matrix import measure_block
from adaptation_matrix._clean import classify_trades, interpolate_seconds, usable_quote

DAY = date(2024, 1, 10)
OPEN = int(datetime(2024, 1, 10, 14, 30, tzinfo=UTC).timestamp() * 1e9)
CLOSE = int(datetime(2024, 1, 10, 21, 0, tzinfo=UTC).timestamp() * 1e9)
S = 1_000_000_000
M = 60 * S


def ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)


def quotes(rows: list[tuple]) -> pl.LazyFrame:
    """rows: (ticker, ns, bid, ask)"""
    return pl.DataFrame(
        {"ticker": [r[0] for r in rows], "sip_timestamp": [r[1] for r in rows],
         "participant_timestamp": [r[1] for r in rows], "trf_timestamp": [None] * len(rows),
         "bid_price": [float(r[2]) for r in rows], "bid_size": [100.0] * len(rows), "bid_exchange": [1] * len(rows),
         "ask_price": [float(r[3]) for r in rows], "ask_size": [200.0] * len(rows), "ask_exchange": [2] * len(rows),
         "conditions": [[1]] * len(rows), "indicators": [None] * len(rows), "sequence_number": list(range(len(rows))),
         "tape": [1] * len(rows)},
        schema_overrides={"sip_timestamp": pl.Int64, "participant_timestamp": pl.Int64, "trf_timestamp": pl.Int64,
                          "bid_exchange": pl.Int16, "ask_exchange": pl.Int16, "conditions": pl.List(pl.Int16),
                          "indicators": pl.List(pl.Int16), "tape": pl.Int8},
    ).with_columns([pl.from_epoch(c, time_unit="ns").dt.replace_time_zone("UTC")
                    for c in ("sip_timestamp", "participant_timestamp", "trf_timestamp")]).lazy()


def trades(rows: list[tuple]) -> pl.LazyFrame:
    """rows: (ticker, sip_ns, price, size, exchange, conditions, participant_ns or None)"""
    return pl.DataFrame(
        {"ticker": [r[0] for r in rows], "sip_timestamp": [r[1] for r in rows],
         "participant_timestamp": [r[6] if len(r) > 6 else r[1] for r in rows], "trf_timestamp": [None] * len(rows),
         "price": [float(r[2]) for r in rows], "size": [float(r[3]) for r in rows], "exchange": [r[4] for r in rows],
         "conditions": [r[5] for r in rows], "correction": [0] * len(rows), "id": [str(i) for i in range(len(rows))],
         "sequence_number": list(range(len(rows))), "tape": [1] * len(rows), "trf_id": [None] * len(rows)},
        schema_overrides={"sip_timestamp": pl.Int64, "participant_timestamp": pl.Int64, "trf_timestamp": pl.Int64,
                          "exchange": pl.Int16, "conditions": pl.List(pl.Int16), "correction": pl.Int8,
                          "tape": pl.Int8, "trf_id": pl.Int32},
    ).with_columns([pl.from_epoch(c, time_unit="ns").dt.replace_time_zone("UTC")
                    for c in ("sip_timestamp", "participant_timestamp", "trf_timestamp")]).lazy()


@pytest.fixture
def block():
    q = quotes([
        ("AAA", OPEN - 60 * S, 9.00, 11.00),       # pre-open, wide; replaced before the open
        ("AAA", OPEN - S, 10.00, 10.02),           # in force at the open
        ("AAA", OPEN + 2 * M, 10.01, 10.03),       # from minute 2 on
        ("AAA", OPEN + 2 * M + 30 * S, 10.05, 10.00),  # crossed: unusable, previous quote stays
        ("AAA", CLOSE - 60 * M, 10.10, 10.12),     # last hour
    ])
    t = trades([
        ("AAA", OPEN, 10.00, 500, 1, [17]),                       # opening auction print
        ("AAA", OPEN + 30 * S, 10.02, 100, 11, None),             # buy at the ask: eff 1 bp... of mid 10.01
        ("AAA", OPEN + 40 * S, 10.01, 100, 11, [37]),             # odd lot at the mid: tick test -> -1
        ("AAA", OPEN + 50 * S, 10.00, 300, 11, [2]),              # average price: excluded
        ("AAA", OPEN + 3 * M + S, 10.0, 100, 4, None, OPEN + 1 * M),  # TRF, executed in minute 1
        ("AAA", CLOSE + 2 * S, 10.15, 1000, 11, [19]),            # closing auction print
    ])
    official = pl.DataFrame({"ticker": ["AAA"], "official_open": [10.00], "official_close": [10.15]})
    return measure_block(t, q, OPEN, CLOSE, interp=False, keep={"AAA"}, official=official)


def test_quoted_spread_is_time_weighted_over_the_session(block):
    d = block.daily.row(0, named=True)
    mins = (CLOSE - OPEN) / M
    # 2 minutes of 10.00/10.02, then 10.01/10.03 until the last hour, then 10.10/10.12.
    hs = lambda b, a: (a - b) / (a + b) * 1e4  # noqa: E731
    expect = (2 * hs(10.00, 10.02) + (mins - 62) * hs(10.01, 10.03) + 60 * hs(10.10, 10.12)) / mins
    assert d["qhs_bps"] == pytest.approx(expect, rel=1e-9)
    assert d["q_ns"] == CLOSE - OPEN
    assert block.minute.filter(pl.col("minute") == 0)["q_ns"].item() == M
    assert block.minute.height == mins


def test_effective_spread_sign_and_exclusions(block):
    d = block.daily.row(0, named=True)
    # Continuous trades: 10.02 x100 (buy, eff 0.01/10.01), 10.01 x100 at the mid (sign by tick: down),
    # TRF 10.00 x100 matched at its execution time (minute 1: mid 10.01 -> sell, eff 0.01/10.01).
    e = 0.01 / 10.01
    dv = [1002.0, 1001.0, 1000.0]
    assert d["n_trades"] == 3
    assert d["eff_bps"] == pytest.approx((dv[0] * e + dv[2] * e) / sum(dv) * 1e4)
    assert d["eff_trf_bps"] == pytest.approx(e * 1e4)
    signs = block.trades.filter(pl.col("kind") == 0).sort("t")["sign"].to_list()
    assert signs == [1, -1, -1]   # buy at the ask, tick-down at the mid, TRF below the mid (at 1 minute)
    assert d["odd_lot_share"] == pytest.approx(1001.0 / sum(dv))


def test_realized_plus_impact_is_effective(block):
    d = block.daily.row(0, named=True)
    for h in ("1m", "5m"):
        assert d[f"rs_{h}_bps"] + d[f"pi_{h}_bps"] == pytest.approx(d["eff_bps"])
    # The buy at 10.02 (mid 10.01): mid 5 minutes later is 10.02 -> impact +1 cent, realized 0.
    buy = block.trades.filter(pl.col("price") == 10.02).row(0, named=True)
    assert buy["mid_5m"] == pytest.approx(10.02)


def test_auctions_and_close_against_the_preclose_mid(block):
    d = block.daily.row(0, named=True)
    assert d["open_auction_volume"] == 500 and d["close_auction_volume"] == 1000
    assert d["mid_preclose"] == pytest.approx(10.11)
    assert d["close_vs_mid_bps"] == pytest.approx((10.15 / 10.11 - 1) * 1e4)
    assert sorted(block.trades["kind"].to_list()) == [0, 0, 0, 1, 2]


def test_classify_and_quote_rules():
    t = trades([("A", OPEN, 1.0, 1, 1, None), ("A", OPEN, 1.0, 1, 1, [14, 37, 41]), ("A", OPEN, 1.0, 1, 1, [12]),
                ("A", OPEN, 1.0, 1, 1, [8]), ("A", OPEN, 1.0, 1, 1, [25]), ("A", OPEN, 1.0, 1, 1, [28]),
                ("A", OPEN, 1.0, 1, 1, [53]), ("A", OPEN, 0.0, 1, 1, None)])
    assert classify_trades(t).collect()["kind"].to_list() == [0, 0, -1, 2, 1, 3, -1, -1]
    q = pl.DataFrame({"bid": [1.0, 0.0, 1.1, 1.0, 1.0], "ask": [1.01, 1.01, 1.0, 1.0, 2.0]})
    assert q.select(usable_quote())["bid"].to_list() == [True, False, False, True, False]


def test_interpolated_time_spreads_events_within_a_second():
    lf = pl.DataFrame({"ticker": ["A"] * 4 + ["B"], "ts": [5 * S, 5 * S, 5 * S, 6 * S + 123, 5 * S]}).lazy()
    out = interpolate_seconds(lf, "ts", only_whole=True).collect()["ts"].to_list()
    assert out[:3] == [5 * S + (2 * i - 1) * S // 6 for i in (1, 2, 3)]
    assert out[3] == 6 * S + 123   # sub-second stamp kept
    assert out[4] == 5 * S + S // 2


def test_keep_none_returns_no_trades():
    q = quotes([("AAA", OPEN - S, 10.0, 10.02)])
    t = trades([("AAA", OPEN + S, 10.02, 100, 11, None)])
    b = measure_block(t, q, OPEN, CLOSE, interp=False)
    assert b.trades.height == 0 and b.daily.height == 1
