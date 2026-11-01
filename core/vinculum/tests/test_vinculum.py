"""Offline tests: a synthetic market with every case the join must handle.

ALPH  ordinary stock, 2-for-1 split on 2021-07-01, two SEC counts reported in thousands
BETA  Massive's CIK is bogus (shared with unrelated GAMA): repaired by name
GAMA  same bogus CIK, no SEC company of that name: CIK rejected
DLTA  ADR, 5 ordinary shares per ADS (ratio 0.2 from Massive's ADS count)
OMGA/OMGB  two classes of one company; SEC count in Class A equivalents, 1 A = 1500 B
ALPHw when-issued line: not equity
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from vinculum import SIZE_BUCKETS, Vinculum, load_settings
from vinculum._names import looks_equity, names_match, tokens

D = date
START, END = D(2020, 10, 1), D(2022, 3, 31)
SPLIT = D(2021, 7, 1)

# security_id, ticker, name, type, massive cik, raw price (before split), daily volume
SECURITIES = [
    ("S1", "ALPH", "ALPHA CORP", "CS", "0000000001", 50.0, 1_000_000),
    ("S2", "BETA", "BETA HOLDINGS INC", "CS", "0000000099", 20.0, 500_000),
    ("S3", "GAMA", "GAMMA TECH INC", "CS", "0000000099", 5.0, 100_000),
    ("S4", "DLTA", "Delta PLC Sponsored ADR", "ADRC", "0000000004", 10.0, 1_000_000),
    ("S5", "OMGA", "OMEGA INC Class A", "CS", "0000000005", 3000.0, 1_000),
    ("S6", "OMGB", "OMEGA INC Class B", "CS", "0000000005", 2.0, 5_000_000),
    ("S7", "ALPHw", "ALPHA CORP WHEN ISSUED", "CS", "0000000001", 25.0, 10_000),
]


def weekdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def month_ends(days: list[date]) -> list[date]:
    out: dict[tuple[int, int], date] = {}
    for d in days:
        out[(d.year, d.month)] = d
    return sorted(out.values())


DAYS = weekdays(START, END)


class FakeStore:
    """The parts of massive_subspace_stream.Store that vinculum uses."""

    def __init__(self):
        rows = []
        for sid, t, _, _, _, px, vol in SECURITIES:
            for d in DAYS:
                p = px / 2 if (sid == "S1" and d >= SPLIT) else px
                v = vol * 2 if (sid == "S1" and d >= SPLIT) else vol
                rows.append((t, d, sid, p, float(v)))
        self._bars = pl.DataFrame(rows, schema={"ticker": pl.String, "date": pl.Date, "security_id": pl.String,
                                                "close": pl.Float64, "volume": pl.Float64}, orient="row")
        self._splits = pl.DataFrame({"security_id": ["S1"], "ticker": ["ALPH"], "execution_date": [SPLIT],
                                     "split_from": [1.0], "split_to": [2.0]})

    def securities(self):
        return pl.DataFrame([(sid, n, ty, c, [t], t, DAYS[0], DAYS[-1]) for sid, t, n, ty, c, _, _ in SECURITIES],
                            schema={"security_id": pl.String, "name": pl.String, "type": pl.String, "cik": pl.String,
                                    "tickers": pl.List(pl.String), "last_ticker": pl.String,
                                    "first_trade": pl.Date, "last_trade": pl.Date}, orient="row")

    def security_attributes(self):
        return pl.DataFrame([(d, sid, t, n, ty, c) for d in month_ends(DAYS) for sid, t, n, ty, c, _, _ in SECURITIES],
                            schema={"as_of": pl.Date, "security_id": pl.String, "ticker": pl.String,
                                    "name": pl.String, "type": pl.String, "cik": pl.String}, orient="row")

    def security_actions(self, kind="splits", security_ids=None):
        return self._splits

    def bars(self, tickers=None, start=None, end=None, adjust=None, lazy=False, **_):
        b = self._bars.filter(pl.col("date").is_between(start, end))
        factor = pl.when((pl.col("security_id") == "S1") & (pl.col("date") < SPLIT)).then(0.5).otherwise(1.0)
        b = b.with_columns(factor.alias("split_factor"))
        if adjust:
            b = b.with_columns(pl.col("close") * pl.col("split_factor"), pl.col("volume") / pl.col("split_factor"))
        return b.lazy() if lazy else b

    def ticker_details(self):
        return pl.DataFrame({
            "as_of": [END] * 5, "ticker": ["ALPH", "BETA", "DLTA", "OMGA", "OMGB"],
            "weighted_shares_outstanding": [200e6, 50e6, 100e6, 1e6, 1.5e9],
            "share_class_shares_outstanding": [200e6, 50e6, 100e6, 0.9e6, 1.4e9],
            "market_cap": [5e9, 1e9, 1e9, 3e9, 3e9]})

    def trading_days(self, start, end):
        return [d for d in DAYS if start <= d <= end]

    def resolve(self, tickers, on=None):
        ids = {t: sid for sid, t, *_ in SECURITIES}
        return pl.DataFrame({"ticker": list(tickers), "security_id": [ids.get(t) for t in tickers]})


def count(cik, end, shares, filed, tag="dei:EntityCommonStockSharesOutstanding"):
    return (cik, end, float(shares), filed, filed + timedelta(days=1), tag)


class FakeEdgar:
    """The parts of edgar_cortical_node.EdgarStore that vinculum uses."""

    def profiles(self):
        return pl.DataFrame({"cik": ["0000000001", "0000000002", "0000000004", "0000000005"],
                             "tickers": [["ALPH"], ["BETA"], ["DLTA"], ["OMGA", "OMGB"]],
                             "first_periodic": [D(2010, 1, 1)] * 4, "last_periodic": [D(2022, 3, 1)] * 4})

    def names(self):
        return pl.DataFrame({"cik": ["0000000001", "0000000002", "0000000004", "0000000005"],
                             "name": ["ALPHA CORP", "BETA HOLDINGS INC", "DELTA PLC", "OMEGA INC"],
                             "valid_from": [None] * 4, "valid_to": [None] * 4},
                            schema={"cik": pl.String, "name": pl.String, "valid_from": pl.Date, "valid_to": pl.Date})

    def shares(self, ciks):
        rows = [
            count("0000000001", D(2020, 12, 31), 100e6, D(2021, 2, 15)),
            count("0000000001", D(2021, 3, 31), 100e3, D(2021, 5, 10)),      # reported in thousands
            count("0000000001", D(2021, 6, 30), 100e3, D(2021, 8, 5)),      # ... twice
            count("0000000001", D(2021, 9, 30), 200e6, D(2021, 11, 5)),     # after the 2:1 split
            count("0000000001", D(2021, 12, 31), 200e6, D(2022, 2, 15)),
            count("0000000002", D(2020, 12, 31), 50e6, D(2021, 2, 15)),
            count("0000000002", D(2021, 9, 30), 50e6, D(2021, 11, 5)),
            count("0000000004", D(2020, 12, 31), 500e6, D(2021, 3, 1)),
            count("0000000004", D(2021, 12, 31), 500e6, D(2022, 3, 1)),
            count("0000000005", D(2020, 12, 31), 1e6, D(2021, 2, 15), "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"),
            count("0000000005", D(2021, 9, 30), 1e6, D(2021, 11, 5), "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"),
        ]
        df = pl.DataFrame(rows, schema={"cik": pl.String, "end": pl.Date, "shares": pl.Float64, "filed": pl.Date,
                                        "available_from": pl.Date, "tag": pl.String}, orient="row")
        return df.filter(pl.col("cik").is_in(list(ciks)))

    def series(self, items, ciks, policy="first"):
        # ALPHA: TTM revenue 400 (filed 2021-05-10), then 480 (filed 2021-11-05); equity 2000.
        rows = [
            ("0000000001", "revenue", "USD", D(2021, 3, 31), 100.0, D(2021, 5, 11), 400.0, D(2021, 5, 11)),
            ("0000000001", "revenue", "USD", D(2021, 9, 30), 130.0, D(2021, 11, 6), 480.0, D(2021, 11, 6)),
            ("0000000001", "equity", "USD", D(2021, 3, 31), 2000.0, D(2021, 5, 11), None, None),
            ("0000000004", "revenue", "GBP", D(2021, 12, 31), 900.0, D(2022, 3, 2), 900.0, D(2022, 3, 2)),
        ]
        df = pl.DataFrame(rows, schema={"cik": pl.String, "item": pl.String, "unit": pl.String, "end": pl.Date,
                                        "value": pl.Float64, "available_from": pl.Date, "ttm": pl.Float64,
                                        "ttm_available_from": pl.Date}, orient="row")
        return df.filter(pl.col("item").is_in(list(items)) & pl.col("cik").is_in(list(ciks)))


@pytest.fixture(scope="module")
def v(tmp_path_factory):
    root = tmp_path_factory.mktemp("vinculum")
    settings = load_settings(data_root=str(root), start_year=2020)
    vin = Vinculum(settings=settings, store=FakeStore(), edgar=FakeEdgar())
    vin.build()
    return vin


def cap(v, ticker, day):
    r = v.market_cap(ticker, start=day, end=day)
    return r.row(0, named=True)


def test_names():
    assert tokens("Taiwan Semiconductor Manufacturing Co Ltd Sponsored ADR") == ("TAIWAN", "SEMICONDUCTOR", "MANUFACTURING")
    assert names_match("QUALITY SYS INC", "QUALITY SYSTEMS INC")
    assert names_match("HILFIGER TOMMY CORP", "TOMMY HILFIGER CORP")
    assert not names_match("DOVER MOTORSPORTS INC", "DOVER DOWNS GAMING & ENTERTAINMENT INC")
    assert not looks_equity("B. Riley Financial, Inc. 5.00% Senior Notes due 2026")
    assert looks_equity("Apple Inc.")


def test_cik_map_verifies_repairs_and_rejects(v):
    m = v.cik_map().group_by("security_id").agg(pl.col("cik").last(), pl.col("status").last())
    st = dict(zip(m["security_id"], zip(m["cik"], m["status"], strict=False), strict=False))
    assert st["S1"] == ("0000000001", "verified")
    assert st["S2"] == ("0000000002", "repaired")   # bogus shared CIK, found by name
    assert st["S3"] == (None, "rejected")
    assert "S7" not in v.market_cap()["security_id"].to_list()   # when-issued line left out


def test_split_and_unit_slip(v):
    c = v.share_counts("S1").sort("end")
    assert c["shares_now"].to_list() == pytest.approx([200e6] * 5)   # 2:1 applied, thousands fixed
    assert c["unit_fixed"].sum() == 2
    assert cap(v, "ALPH", D(2021, 6, 1))["market_cap"] == pytest.approx(5e9)   # a fixed count in use
    # Before the split: 50 x 100M; after: 25 x 200M; same company value.
    assert cap(v, "ALPH", D(2021, 3, 1))["market_cap"] == pytest.approx(5e9)
    assert cap(v, "ALPH", D(2021, 12, 1))["market_cap"] == pytest.approx(5e9)
    before = cap(v, "ALPH", D(2021, 3, 1))
    assert before["shares"] == pytest.approx(100e6) and before["close"] == pytest.approx(50.0)


def test_point_in_time_and_stale(v):
    assert cap(v, "ALPH", D(2021, 2, 15))["market_cap"] is None       # first count usable the next day
    assert cap(v, "ALPH", D(2021, 2, 16))["market_cap"] == pytest.approx(5e9)
    assert cap(v, "GAMA", D(2021, 6, 1))["market_cap"] is None        # no CIK, no shares


def test_adr_ratio_and_classes(v):
    b = v.basis().select("security_id", "basis_ratio", "basis")
    ratios = dict(zip(b["security_id"], b["basis_ratio"], strict=False))
    assert ratios["S4"] == pytest.approx(0.2) and ratios["S6"] == pytest.approx(1500) and ratios["S5"] == 1.0
    assert cap(v, "DLTA", D(2021, 6, 1))["market_cap"] == pytest.approx(10 * 500e6 * 0.2)
    a, bb = cap(v, "OMGA", D(2021, 6, 1)), cap(v, "OMGB", D(2021, 6, 1))
    assert a["market_cap"] == pytest.approx(3e9) and bb["market_cap"] == pytest.approx(3e9)
    assert bb["is_primary"] and not a["is_primary"]                   # B trades more dollars
    assert a["issuer_market_cap"] == bb["issuer_market_cap"]


def test_ranks_and_universe(v):
    r = v.ranks(D(2021, 6, 1))
    assert r["ticker"].to_list()[:3] == ["ALPH", "OMGB", "BETA"] or r["ticker"][0] == "ALPH"
    assert "OMGA" not in r["ticker"].to_list()                       # one row per company
    assert set(r["size"]) == {"large"} and SIZE_BUCKETS["large"] == (1, 200)
    u = v.universe(D(2021, 6, 15), top=2)
    assert u["rank_date"][0] == D(2021, 5, 31) and u.height == 2
    with pytest.raises(ValueError):
        v.universe(D(2021, 6, 15), size="huge")


def test_fundamentals_point_in_time(v):
    f = v.fundamentals(["revenue", "equity"], start=D(2021, 5, 1), end=D(2021, 12, 31), freq="1d")
    alph = f.filter(pl.col("ticker") == "ALPH")
    day = lambda d: alph.filter(pl.col("date") == d).row(0, named=True)  # noqa: E731
    assert day(D(2021, 5, 10))["revenue"] is None                     # filed that day: not yet usable
    assert day(D(2021, 5, 11))["revenue"] == 400.0
    assert day(D(2021, 11, 8))["revenue"] == 480.0
    assert day(D(2021, 11, 8))["sales_to_price"] == pytest.approx(480 / 5e9)
    assert day(D(2021, 11, 8))["book_to_market"] == pytest.approx(2000 / 5e9)
    m = v.fundamentals(["revenue"], start=D(2021, 1, 1), end=D(2021, 12, 31))   # month ends
    assert m["date"].n_unique() == 12


def test_checks_and_status_run(v):
    info = v.status()
    assert info["rows"] > 0 and "cik_status" in info
    names = [n for n, _, _ in v.check()]
    assert any("Taser" in n for n in names)
