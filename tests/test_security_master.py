import datetime as dt

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.universe.security_master import (
    assign_identities,
    check_master,
    map_to_security,
    securities_from_master,
    trading_episodes,
)

D = dt.date
SESSIONS = trading_days(D(2022, 1, 3), D(2022, 12, 30))


def _bars(ticker: str, start: D, end: D) -> pl.DataFrame:
    days = [d for d in SESSIONS if start <= d <= end]
    return pl.DataFrame({"ticker": [ticker] * len(days), "date": days})


META_OLD = {"name": "Metaverse ETF", "type": "ETF", "composite_figi": "BBG_ETF", "cik": None}
META_NEW = {"name": "Meta Platforms", "type": "CS", "composite_figi": "BBG_META", "cik": "0001"}
FB_META = {"name": "Meta Platforms", "type": "CS", "composite_figi": "BBG_META", "cik": "0001"}
NOFIGI = {"name": "Tiny Co", "type": "CS", "composite_figi": None, "cik": "0009"}

# ground truth used by the fake lookup: META = ETF until 2022-06-08, Meta from 2022-06-09 (no gap)
SWITCH = D(2022, 6, 9)


def fake_lookup(ticker: str, d: D):
    if ticker == "META":
        return META_OLD if d < SWITCH else META_NEW
    if ticker == "FB":
        return FB_META if d < SWITCH else None
    if ticker == "TINY":
        return NOFIGI
    return None  # ZVZZT etc.


def _snapshots() -> pl.DataFrame:
    rows = []
    firsts = {}
    for d in SESSIONS:
        firsts.setdefault((d.year, d.month), d)
    for asof in firsts.values():
        for t in ("META", "FB", "TINY", "ZVZZT"):
            rec = fake_lookup(t, asof)
            if rec:
                rows.append({"ticker": t, "asof": asof, **rec})
    return pl.DataFrame(rows)


def test_trading_episodes_split_on_gap():
    bars = pl.concat([_bars("FB", D(2022, 1, 3), D(2022, 6, 8)), _bars("FB", D(2022, 9, 1), D(2022, 12, 30))])
    eps = trading_episodes(bars.lazy())
    assert eps.height == 2
    assert eps["start"].to_list() == [D(2022, 1, 3), D(2022, 9, 1)]
    assert eps["end"].to_list() == [D(2022, 6, 8), D(2022, 12, 30)]


def test_assign_identities_resolves_reuse_and_rename():
    bars = pl.concat(
        [
            _bars("META", D(2022, 1, 3), D(2022, 12, 30)),  # reuse without gap
            _bars("FB", D(2022, 1, 3), D(2022, 6, 8)),  # renamed to META
            _bars("TINY", D(2022, 3, 2), D(2022, 3, 10)),  # no snapshot inside; no figi -> CIK id
            _bars("ZVZZT", D(2022, 1, 3), D(2022, 12, 30)),  # test symbol
        ]
    )
    eps = trading_episodes(bars.lazy())
    bar_dates = {t: g["date"].to_list() for (t,), g in bars.sort("date").group_by("ticker")}
    calls = []

    def lookup(t, d):
        calls.append((t, d))
        return fake_lookup(t, d)

    m = assign_identities(eps, bar_dates, _snapshots(), lookup, reuse_candidates={"FB", "META"})

    meta = m.filter(pl.col("ticker") == "META").sort("valid_from")
    assert meta["security_id"].to_list() == ["BBG_ETF", "BBG_META"]
    assert meta["valid_to"][0] == D(2022, 6, 8)
    assert meta["valid_from"][1] == SWITCH  # boundary pinned exactly by bisection
    assert meta["valid_to"][1] == D(2022, 12, 30)

    fb = m.filter(pl.col("ticker") == "FB")
    assert fb.height == 1 and fb["security_id"][0] == "BBG_META"

    tiny = m.filter(pl.col("ticker") == "TINY")
    assert tiny["security_id"][0] == "CIK:0009:TINY" and tiny["method"][0] == "lookup"

    z = m.filter(pl.col("ticker") == "ZVZZT")
    assert z["is_test"][0] and z["id_source"][0] == "none"

    # bisection is cheap: a handful of lookups, not one per day
    assert len([c for c in calls if c[0] == "META"]) < 12

    rep = check_master(m, eps)
    assert rep["overlapping_segments"] == 0
    assert rep["tickers_with_multiple_securities"] == 1  # META
    assert rep["securities_with_multiple_tickers"] == 1  # BBG_META via FB and META

    sec = securities_from_master(m)
    row = sec.filter(pl.col("security_id") == "BBG_META").row(0, named=True)
    assert row["tickers"] == ["FB", "META"]
    assert row["first_trade"] == D(2022, 1, 3) and row["last_trade"] == D(2022, 12, 30)


def test_map_to_security():
    master = pl.DataFrame(
        {
            "ticker": ["META", "META", "FB"],
            "security_id": ["BBG_ETF", "BBG_META", "BBG_META"],
            "valid_from": [D(2022, 1, 3), SWITCH, D(2022, 1, 3)],
            "valid_to": [D(2022, 6, 8), D(2022, 12, 30), D(2022, 6, 8)],
        }
    )
    df = pl.DataFrame(
        {
            "ticker": ["META", "META", "FB", "FB"],
            "date": [D(2022, 3, 1), D(2022, 7, 1), D(2022, 3, 1), D(2022, 9, 1)],
        }
    )
    out = map_to_security(df, master).sort(["ticker", "date"])
    assert out["security_id"].to_list() == ["BBG_META", None, "BBG_ETF", "BBG_META"]


def test_test_symbol_pattern():
    from collective_alpha.universe.security_master import TEST_TICKER_RE

    for t in ("ZVZZT", "ZTEST", "ZVV", "ZZK", "MTEST", "CTEST.A", "NTEST.I", "ZBZX"):
        assert TEST_TICKER_RE.match(t), t
    for t in ("ZM", "ZS", "META", "TEST", "X"):  # ZBRA matches but is_test also needs an unknown identity
        assert not TEST_TICKER_RE.match(t), t
