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
    ("S8", "ETWI", "Eta Transfer LP Common Units", "CS", "0000000008", 10.0, 200_000),   # MLP, ticker ends in WI
    ("S9", "NULT", "NULL TYPE CO", None, "0000000009", 8.0, 300_000),                   # delisted before types
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

    SECS = SECURITIES
    WINDOWS: dict[str, tuple[date, date]] = {}   # security_id -> (first, last trading day)

    def dataset(self, name, *args, **kwargs):
        raise FileNotFoundError(name)

    def days(self, sid):
        a, b = self.WINDOWS.get(sid, (DAYS[0], DAYS[-1]))
        return [d for d in DAYS if a <= d <= b]

    def __init__(self):
        rows = []
        for sid, t, _, _, _, px, vol in self.SECS:
            for d in self.days(sid):
                p = px / 2 if (sid == "S1" and d >= SPLIT) else px
                v = vol * 2 if (sid == "S1" and d >= SPLIT) else vol
                rows.append((t, d, sid, p, float(v)))
        self._bars = pl.DataFrame(rows, schema={"ticker": pl.String, "date": pl.Date, "security_id": pl.String,
                                                "close": pl.Float64, "volume": pl.Float64}, orient="row")
        self._splits = pl.DataFrame({"security_id": ["S1"], "ticker": ["ALPH"], "execution_date": [SPLIT],
                                     "split_from": [1.0], "split_to": [2.0]})

    def securities(self):
        return pl.DataFrame([(sid, n, ty, c, [t], t, self.days(sid)[0], self.days(sid)[-1])
                             for sid, t, n, ty, c, _, _ in self.SECS],
                            schema={"security_id": pl.String, "name": pl.String, "type": pl.String, "cik": pl.String,
                                    "tickers": pl.List(pl.String), "last_ticker": pl.String,
                                    "first_trade": pl.Date, "last_trade": pl.Date}, orient="row")

    def security_attributes(self):
        return pl.DataFrame([(d, sid, t, n, ty, c) for sid, t, n, ty, c, _, _ in self.SECS
                             for d in month_ends(self.days(sid))],
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
        ids = {t: sid for sid, t, *_ in SECURITIES}   # OLDA (ALPHA's former ticker) no longer trades
        return pl.DataFrame({"ticker": list(tickers), "security_id": [ids.get(t) for t in tickers]})

    def security_master(self, tickers=None):
        rows = [(t, DAYS[0], None, sid) for sid, t, *_ in SECURITIES] + [("OLDA", D(2015, 1, 1), D(2019, 12, 31), "S1")]
        df = pl.DataFrame(rows, schema={"ticker": pl.String, "valid_from": pl.Date, "valid_to": pl.Date,
                                        "security_id": pl.String}, orient="row")
        return df.filter(pl.col("ticker").is_in(list(tickers))) if tickers else df


def count(cik, end, shares, filed, tag="dei:EntityCommonStockSharesOutstanding"):
    return (cik, end, float(shares), filed, filed + timedelta(days=1), tag)


class FakeEdgar:
    """The parts of edgar_cortical_node.EdgarStore that vinculum uses."""

    def profiles(self):
        return pl.DataFrame({"cik": ["0000000001", "0000000002", "0000000004", "0000000005", "0000000008", "0000000009"],
                             "tickers": [["ALPH"], ["BETA"], ["DLTA"], ["OMGA", "OMGB"], ["ETWI"], []],
                             "first_periodic": [D(2010, 1, 1)] * 6, "last_periodic": [D(2022, 3, 1)] * 6})

    def names(self):
        return pl.DataFrame({"cik": ["0000000001", "0000000002", "0000000004", "0000000005", "0000000008", "0000000009"],
                             "name": ["ALPHA CORP", "BETA HOLDINGS INC", "DELTA PLC", "OMEGA INC", "ETA TRANSFER LP",
                                      "NULL TYPE CO"],
                             "valid_from": [None] * 6, "valid_to": [None] * 6},
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
            count("0000000008", D(2020, 12, 31), 100e6, D(2021, 2, 15)),
            count("0000000009", D(2020, 12, 31), 100e6, D(2021, 2, 15)),
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
            # Public float (10-K cover), dated the float date; usable the day after the 10-K.
            ("0000000001", "public_float", "USD", D(2020, 6, 30), 1e9, D(2021, 2, 16), None, None),   # no cap then
            ("0000000001", "public_float", "USD", D(2021, 6, 30), 4e9, D(2021, 11, 6), None, None),   # 80% of 5B
            ("0000000005", "public_float", "USD", D(2021, 6, 30), 1.5e9, D(2021, 11, 6), None, None), # 50% of 3B
            ("0000000002", "public_float", "USD", D(2021, 6, 30), 0.9e6, D(2021, 11, 6), None, None), # thousands
            ("0000000009", "public_float", "USD", D(2021, 6, 30), 2e9, D(2021, 11, 6), None, None),   # 2.5x cap
            ("0000000004", "public_float", "GBP", D(2021, 6, 30), 1e9, D(2021, 11, 6), None, None),   # not USD
        ]
        df = pl.DataFrame(rows, schema={"cik": pl.String, "item": pl.String, "unit": pl.String, "end": pl.Date,
                                        "value": pl.Float64, "available_from": pl.Date, "ttm": pl.Float64,
                                        "ttm_available_from": pl.Date}, orient="row")
        return df.filter(pl.col("item").is_in(list(items)) & pl.col("cik").is_in(list(ciks)))


class FakeFinra:
    """The parts of finra_sensor_array.FinraStore that vinculum uses (FINRA symbols)."""

    def short_interest(self, start=None, end=None, date_by="settlement"):
        si = pl.DataFrame({
            "settlement_date": [D(2021, 6, 15), D(2021, 11, 15), D(2021, 11, 15), D(2021, 11, 15)],
            "published_date": [D(2021, 6, 24), D(2021, 11, 24), D(2021, 11, 24), D(2021, 11, 24)],
            "symbol": ["ALPH", "ALPH", "OMGB", "ZZZZ"],          # ZZZZ: an OTC symbol, no security
            "market": ["NYSE", "NYSE", "NYSE", "OTC"],
            "short_interest": [10_000_000, 20_000_000, 150_000_000, 5],
            "change_pct": [0.0] * 4, "avg_daily_volume": [1_000_000] * 4, "days_to_cover": [10.0] * 4})
        col = f"{date_by}_date"
        if start:
            si = si.filter(pl.col(col) >= start)
        if end:
            si = si.filter(pl.col(col) <= end)
        return si

    def short_volume(self, start=None, end=None, lazy=False):
        sv = pl.DataFrame({"date": [D(2021, 6, 15), D(2021, 6, 15)], "symbol": ["ALPH", "BETA"],
                           "short_volume": [300_000, 50_000], "short_exempt_volume": [0, 0],
                           "total_volume": [600_000, 100_000], "markets": ["Q", "N"], "source": ["CNMS"] * 2})
        sv = sv.filter(pl.col("date").is_between(start or D(1900, 1, 1), end or D(2100, 1, 1)))
        return sv.lazy() if lazy else sv


@pytest.fixture(scope="module")
def v(tmp_path_factory):
    root = tmp_path_factory.mktemp("vinculum")
    settings = load_settings(data_root=str(root), start_year=2020)
    vin = Vinculum(settings=settings, store=FakeStore(), edgar=FakeEdgar(), finra=FakeFinra())
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
    assert not looks_equity("B. Riley Financial, Inc. 5.00% Senior Notes due 2026", "CS")
    assert not looks_equity("Acme Acquisition Corp Units, each consisting of one share and one warrant", "CS")
    assert looks_equity("Apple Inc.")
    # Massive's type decides for CS/ADRC; the wider net is for untyped old securities only.
    for name in ("Energy Transfer LP Common Units", "Banco Bradesco S.A. ADS (Each representing one Preferred Share)",
                 "Senior Housing Properties Trust", "UNIT Corporation", "Blackstone Secured Lending Fund"):
        assert looks_equity(name, "CS") and not looks_equity(name, None)


def test_cik_map_verifies_repairs_and_rejects(v):
    m = v.cik_map().group_by("security_id").agg(pl.col("cik").last(), pl.col("status").last())
    st = dict(zip(m["security_id"], zip(m["cik"], m["status"], strict=False), strict=False))
    assert st["S1"] == ("0000000001", "verified")
    assert st["S2"] == ("0000000002", "repaired")   # bogus shared CIK, found by name
    assert st["S3"] == (None, "rejected")
    ids = set(v.market_cap()["security_id"])
    assert "S7" not in ids                        # when-issued line left out
    assert {"S8", "S9"} <= ids                    # ...but not tickers ending in WI, MLP units or untyped names


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
    assert "NULT" in r["ticker"].to_list()                           # no recorded type still counts
    adr_only = v.ranks(D(2021, 6, 1), types=("ADRC",))["ticker"].to_list()
    assert "NULT" in adr_only and "ALPH" not in adr_only
    assert set(r["size"]) == {"large"} and SIZE_BUCKETS["large"] == (1, 200)
    u = v.universe(D(2021, 6, 15), top=2)
    assert u["rank_date"][0] == D(2021, 5, 31) and u.height == 2
    with pytest.raises(ValueError):
        v.universe(D(2021, 6, 15), size="huge")


def test_ranks_on_several_days_match_one_day_at_a_time(v):
    days = [D(2021, 6, 1), D(2021, 6, 15), D(2021, 7, 2)]
    both = v.ranks(days)
    one = pl.concat([v.ranks(d) for d in days])
    key = ["date", "rank"]
    assert both.columns == one.columns and both.height == one.height > 0
    assert both.sort(key).select(key + ["security_id", "size"]).equals(one.sort(key).select(key + ["security_id", "size"]))
    assert both.group_by("date").agg(pl.col("rank").min())["rank"].to_list() == [1] * len(days)


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


def test_delisted_ticker_by_security(v):
    old = v.market_cap("OLDA", by="security", start=D(2021, 6, 1), end=D(2021, 6, 1))
    assert old["security_id"].to_list() == ["S1"]                    # ALPHA's former ticker finds ALPHA
    assert v.market_cap("OLDA", start=D(2021, 6, 1), end=D(2021, 6, 1)).is_empty()


def test_checks_and_status_run(v):
    info = v.status()
    assert info["rows"] > 0 and "cik_status" in info
    names = [n for n, _, _ in v.check()]
    assert any("Taser" in n for n in names)


def test_public_float(v):
    st = dict(v.public_float().select(pl.col("cik").str.slice(-1) + pl.col("float_date").cast(pl.String),
                                      "status").iter_rows())
    assert st == {"12020-06-30": "no_market_cap", "12021-06-30": "ok", "52021-06-30": "ok",
                  "22021-06-30": "unit_fixed", "92021-06-30": "implausible"}  # the GBP float is left out
    assert v.public_float("ALPH")["float_date"].to_list() == [D(2020, 6, 30), D(2021, 6, 30)]
    assert cap(v, "ALPH", D(2021, 11, 5))["float_fraction"] is None          # 10-K filed 11-05: not yet
    a = cap(v, "ALPH", D(2021, 11, 8))
    assert a["float_fraction"] == pytest.approx(0.8) and a["float_market_cap"] == pytest.approx(4e9)
    assert a["float_date"] == D(2021, 6, 30)
    for t in ("OMGA", "OMGB"):                                              # company-wide, every class
        r = cap(v, t, D(2021, 12, 1))
        assert r["float_fraction"] == pytest.approx(0.5) and r["issuer_float_market_cap"] == pytest.approx(1.5e9)
    assert cap(v, "BETA", D(2021, 12, 1))["float_fraction"] == pytest.approx(0.9)
    assert cap(v, "NULT", D(2021, 12, 1))["float_fraction"] is None


def test_ranks_by_float(v):
    r = v.ranks(D(2021, 12, 1), by="float_market_cap")
    assert r["ticker"].to_list() == ["ALPH", "OMGB", "BETA"]               # no usable float: left out
    assert r["issuer_float_market_cap"].to_list() == pytest.approx([4e9, 1.5e9, 0.9e9])
    u = v.universe(D(2021, 12, 15), top=2, by="float_market_cap")
    assert u["ticker"].to_list() == ["ALPH", "OMGB"] and u["rank_market_cap"][0] == pytest.approx(4e9)
    with pytest.raises(ValueError):
        v.ranks(D(2021, 12, 1), by="free_float")


def test_float_goes_stale():
    from vinculum._build import FLOAT_STALE_DAYS, PANEL_SCHEMA, add_float
    days = [D(2021, 3, 1), D(2021, 3, 1) + timedelta(days=FLOAT_STALE_DAYS + 1)]
    panel = pl.DataFrame({"date": days, "security_id": ["S"] * 2, "cik": ["1"] * 2, "market_cap": [10.0] * 2,
                          "issuer_market_cap": [10.0] * 2}).with_columns(
        [pl.lit(None, t).alias(c) for c, t in PANEL_SCHEMA.items()
         if c not in ("date", "security_id", "cik", "market_cap", "issuer_market_cap")])
    floats = pl.DataFrame({"cik": ["1"], "available_from": [D(2021, 2, 1)], "float_date": [D(2020, 6, 30)],
                           "float_fraction": [0.5]})
    out = add_float(panel, floats)
    assert out["float_fraction"].to_list() == [0.5, None]


def test_short_interest_per_security(v):
    si = v.short_interest(date_by="settlement")
    assert set(si["symbol"]) == {"ALPH", "OMGB"}                          # OTC symbol without a security: out
    a = si.filter(pl.col("ticker") == "ALPH").sort("settlement_date")
    # Before the 2:1 split: 10M short x $50 of a $5B company; after: 20M x $25.
    assert a["si_pct_market_cap"].to_list() == pytest.approx([0.1, 0.1])
    assert a["si_pct_float"][0] is None and a["si_pct_float"][1] == pytest.approx(0.1 / 0.8)   # float from 11-06
    o = si.filter(pl.col("ticker") == "OMGB").row(0, named=True)
    assert o["si_pct_market_cap"] == pytest.approx(150e6 * 2.0 / 3e9)     # class B shares, whole company
    assert o["security_id"] == "S6" and o["type"] is not None
    # start/end filter on the publication date by default (point in time).
    assert v.short_interest("ALPH", end=D(2021, 6, 23)).is_empty()
    assert v.short_interest("ALPH", end=D(2021, 6, 24)).height == 1
    assert v.short_interest("OLDA", by="security", end=D(2021, 12, 31)).height == 2   # ALPHA's old ticker


def test_short_volume_per_security(v):
    sv = v.short_volume(start=D(2021, 6, 15), end=D(2021, 6, 15))
    a = sv.filter(pl.col("ticker") == "ALPH").row(0, named=True)
    vol = cap(v, "ALPH", D(2021, 6, 15))["volume"]
    assert a["short_ratio"] == pytest.approx(0.5) and a["off_exchange_share"] == pytest.approx(600_000 / vol)
    assert v.short_volume("BETA")["short_volume"].to_list() == [50_000]


# ---------------------------------------------------------------- per-share items across splits
def test_per_share_ttm_across_a_split():
    from vinculum._pershare import cum_splits, factor_at, per_share_ttm

    def q(start, end, value, filed, period="quarter"):
        return ("C", "item", "USD/shares", period, start, end, value, filed)

    rows = pl.DataFrame([
        q(D(2023, 4, 1), D(2023, 6, 30), 1.0, D(2023, 8, 1)),
        q(D(2023, 7, 1), D(2023, 9, 30), 1.0, D(2023, 11, 1)),
        q(D(2023, 10, 1), D(2023, 12, 31), 1.0, D(2024, 2, 15)),
        q(D(2023, 1, 1), D(2023, 12, 31), 4.2, D(2024, 2, 15), "annual"),
        q(D(2024, 1, 1), D(2024, 3, 31), 1.0, D(2024, 5, 1)),
        q(D(2024, 4, 1), D(2024, 6, 30), 0.5, D(2024, 8, 1)),       # filed after the 2:1 split
    ], schema={"cik": pl.String, "item": pl.String, "unit": pl.String, "period": pl.String, "start": pl.Date,
               "end": pl.Date, "value": pl.Float64, "filed": pl.Date}, orient="row")
    cum = cum_splits(pl.DataFrame({"security_id": ["S"], "execution_date": [D(2024, 6, 10)], "k": [2.0]}))
    t = per_share_ttm(rows, pl.DataFrame({"security_id": ["S"], "cik": ["C"]}), cum)
    w = dict(zip(t["end"].to_list(), t["w"].to_list(), strict=True))
    assert w[D(2023, 12, 31)] == 4.2                       # the annual figure, not the sum of quarters
    assert w[D(2024, 6, 30)] == pytest.approx(1 + 1 + 1 + 0.5 * 2)   # every quarter on one basis
    day = factor_at(pl.DataFrame({"security_id": ["S", "S"], "date": [D(2024, 5, 1), D(2024, 9, 1)]}),
                    "date", cum, "c")
    assert day["c"].to_list() == [1.0, 2.0]                # TTM EPS on 2024-09-01: 4.0 / 2 = 2.0 per new share


# ---------------------------------------------------------------- successors
def test_successor_units():
    from vinculum._successors import (
        cik_links,
        handoff_candidates,
        inherited_counts,
        lineage,
        security_links,
    )

    secs = pl.DataFrame({
        "security_id": ["OLD", "NEW", "PH", "OTHER"], "last_ticker": ["XO", "XO", "XO", "ZZ"],
        "tickers": [["XO"], ["XO"], ["XO"], ["ZZ"]],
        "first_trade": [D(2020, 1, 2), D(2021, 7, 2), D(2021, 7, 1), D(2020, 1, 2)],
        "last_trade": [D(2021, 6, 30), D(2022, 3, 31), D(2021, 7, 1), D(2022, 3, 31)]})
    cand = handoff_candidates(secs, ["OLD", "NEW", "PH", "OTHER"])
    assert cand.select("predecessor_id", "security_id").rows() == [("OLD", "NEW")]   # one-day listing skipped
    cmap = pl.DataFrame({"security_id": ["OLD", "NEW"], "valid_from": [D(1900, 1, 1)] * 2, "cik": ["1", "2"],
                         "status": ["verified"] * 2, "edgar_name": ["XO CORP", "XO HOLDINGS CORP"]})
    attrs = pl.DataFrame({"security_id": ["OLD", "NEW"], "name": ["Xo Corporation", "XO Holdings Corporation"]})
    closes = pl.DataFrame({"security_id": ["OLD", "NEW"], "date": [D(2021, 6, 30), D(2021, 7, 2)],
                           "close": [100.0, 101.0]})
    link = security_links(cand, cmap, attrs, closes).row(0, named=True)
    assert (link["predecessor_cik"], link["cik"], link["evidence"]) == ("1", "2", "name")
    assert link["price_ratio"] == pytest.approx(1.01)
    merger = closes.with_columns(pl.when(pl.col("security_id") == "NEW").then(50.0).otherwise(pl.col("close")).alias("close"))
    assert security_links(cand, cmap, attrs, merger).is_empty()           # not a 1:1 exchange
    # Same security, new CIK.
    runs = pl.DataFrame({"security_id": ["S", "S"], "valid_from": [D(1900, 1, 1), D(2016, 2, 1)],
                         "cik": ["7", "8"], "status": ["verified", "verified"], "edgar_name": [None, None]})
    c = cik_links(runs, ["S"]).row(0, named=True)
    assert (c["kind"], c["predecessor_cik"], c["cik"], c["link_date"]) == ("cik", "7", "8", D(2016, 2, 1))
    # Chains: B <- A (cik link), C <- B (security link).
    succ = pl.DataFrame({"security_id": ["A", "C"], "cik": ["2", "3"], "predecessor_id": ["A", "A"],
                         "predecessor_cik": ["1", "2"], "link_date": [D(2015, 1, 1), D(2020, 1, 1)]})
    lin = lineage(succ).filter(pl.col("security_id") == "C").sort("depth")
    assert lin["predecessor_cik"].to_list() == ["2", "1"]
    # Inherited counts: only those known before the link, through the old security's splits.
    obs = pl.DataFrame({"security_id": ["OLD", "OLD", "OLD"], "cik": ["1"] * 3,
                        "end": [D(2021, 3, 31), D(2021, 6, 30), D(2021, 9, 30)],
                        "shares": [10.0, 10.0, 10.0], "filed": [D(2021, 5, 1), D(2021, 8, 1), D(2021, 11, 1)],
                        "available_from": [D(2021, 5, 2), D(2021, 8, 2), D(2021, 11, 2)], "tag": ["dei:X"] * 3})
    ev = pl.DataFrame({"security_id": ["OLD"], "execution_date": [D(2021, 5, 15)], "k": [3.0]})
    inh = inherited_counts(obs, pl.DataFrame([link]), 400, ev)
    assert inh.select("security_id", "cik", "shares", "tag").rows() == [("NEW", "2", 30.0, "successor:dei:X")]


class HandoffStore(FakeStore):
    """ZETA moves to a new security and CIK (a holding company) on 2021-07-02."""
    SECS = [*SECURITIES, ("S10", "ZETA", "ZETA CORP", "CS", "0000000010", 40.0, 500_000),
            ("S11", "ZETA", "ZETA HOLDINGS CORP", "CS", "0000000011", 40.0, 500_000)]
    WINDOWS = {"S10": (START, D(2021, 6, 30)), "S11": (D(2021, 7, 2), END)}


class HandoffEdgar(FakeEdgar):
    def profiles(self):
        extra = pl.DataFrame({"cik": ["0000000010", "0000000011"], "tickers": [["ZETA"], ["ZETA"]],
                              "first_periodic": [D(2010, 1, 1), D(2021, 8, 1)], "last_periodic": [D(2021, 8, 1)] * 2})
        return pl.concat([super().profiles(), extra])

    def names(self):
        extra = pl.DataFrame({"cik": ["0000000010", "0000000011"], "name": ["ZETA CORP", "ZETA HOLDINGS CORP"],
                              "valid_from": [None] * 2, "valid_to": [None] * 2},
                             schema={"cik": pl.String, "name": pl.String, "valid_from": pl.Date, "valid_to": pl.Date})
        return pl.concat([super().names(), extra])

    def shares(self, ciks):
        extra = pl.DataFrame([count("0000000010", D(2020, 12, 31), 100e6, D(2021, 2, 15)),
                              count("0000000011", D(2021, 9, 30), 100e6, D(2021, 11, 5))],
                             schema={"cik": pl.String, "end": pl.Date, "shares": pl.Float64, "filed": pl.Date,
                                     "available_from": pl.Date, "tag": pl.String}, orient="row")
        return pl.concat([super().shares(ciks), extra.filter(pl.col("cik").is_in(list(ciks)))])

    def series(self, items, ciks, policy="first"):
        base = super().series(items, ciks, policy)
        extra = pl.DataFrame([
            ("0000000010", "revenue", "USD", D(2021, 3, 31), 75.0, D(2021, 5, 11), 300.0, D(2021, 5, 11)),
            ("0000000011", "revenue", "USD", D(2021, 9, 30), 90.0, D(2021, 11, 6), 333.0, D(2021, 11, 6)),
        ], schema=base.schema, orient="row")
        return pl.concat([base, extra.filter(pl.col("item").is_in(list(items)) & pl.col("cik").is_in(list(ciks)))])


def test_holding_company_reorganization(tmp_path):
    settings = load_settings(data_root=str(tmp_path / "vinculum"), start_year=2020)
    v = Vinculum(settings=settings, store=HandoffStore(), edgar=HandoffEdgar(), finra=FakeFinra())
    v.build()
    s = v.successors().row(0, named=True)
    assert (s["predecessor_id"], s["security_id"], s["kind"]) == ("S10", "S11", "security")
    # Market cap continues from the old company's count until the new CIK files its own.
    first = v.market_cap(["ZETA"], start=D(2021, 7, 2), end=D(2021, 7, 2)).row(0, named=True)
    assert first["security_id"] == "S11" and first["market_cap"] == pytest.approx(4e9)
    assert first["shares_tag"].startswith("successor:")
    assert v.ranks(D(2021, 7, 2)).filter(pl.col("ticker") == "ZETA").height == 1
    # Fundamentals: the predecessor's TTM until the successor has its own.
    f = v.fundamentals(["revenue"], tickers="ZETA", start=D(2021, 6, 1), end=D(2021, 12, 31), freq="1d")
    rev = dict(zip(f["date"].to_list(), f["revenue"].to_list(), strict=True))
    assert rev[D(2021, 6, 30)] == 300.0 and rev[D(2021, 8, 2)] == 300.0 and rev[D(2021, 11, 8)] == 333.0


# Penny ADRs. JUNK's ADS ratio is wrong (a $0.81 ADR shown as a $6B company on $36k a day,
# turnover 6e-6: above the general floor; $3 until 2021-09-01, so its first count's median price
# is above $2, as NCNA's was). XHGX's ratio is wrong too ($1 shown as $10B on $900k a day): its
# turnover, 9e-5, passes the penny floor, as SVRE's did, but no real $10B company trades so
# little. Both are worth tens of millions today (Massive's market cap). LYGX is a real penny ADR
# that trades heavily (Lloyds); THIN trades little in the US but is not a penny ADR (James
# Hardie); FEWX is a real $3B ADR that falls to $1.40 for its last 10 days on $230k a day, still
# near today's value (Enel Chile): kept.
PENNY_ADRS = [("S12", "JUNK", "Junk Bio PLC ADR", "ADRC", "0000000012", 0.81, 45_000),
              ("S13", "LYGX", "Lygx Banking Group PLC ADR", "ADRC", "0000000013", 1.5, 20_000_000),
              ("S14", "THIN", "Thin Industries PLC ADR", "ADRC", "0000000014", 25.0, 8_000),
              ("S15", "XHGX", "Xhgx Holdings Ltd ADR", "ADRC", "0000000015", 1.0, 900_000),
              ("S16", "FEWX", "Fewx Mining PLC ADR", "ADRC", "0000000016", 3.0, 166_000)]
# ticker: (SEC ordinary shares, Massive ADS count, Massive market cap today); ratio = ADS / ordinary
PENNY_COUNTS = {"JUNK": (500e6, 7.5e9, 20e6), "LYGX": (20e9, 5e9, 7.5e9), "THIN": (400e6, 400e6, 10e9),
                "XHGX": (1e9, 10e9, 50e6), "FEWX": (1e9, 1e9, 3e9)}
JUNK_PENNY_FROM = D(2021, 9, 1)


class PennyStore(FakeStore):
    SECS = [*SECURITIES, *PENNY_ADRS]

    def __init__(self):
        super().__init__()
        early = (pl.col("security_id") == "S12") & (pl.col("date") < JUNK_PENNY_FROM)
        late = (pl.col("security_id") == "S16") & (pl.col("date") >= DAYS[-10])
        self._bars = self._bars.with_columns(pl.when(early).then(3.0).when(late).then(1.4)
                                             .otherwise(pl.col("close")).alias("close"))

    def ticker_details(self):
        ads = [a for _, a, _ in PENNY_COUNTS.values()]
        extra = pl.DataFrame({"as_of": [END] * len(ads), "ticker": list(PENNY_COUNTS),
                              "weighted_shares_outstanding": ads, "share_class_shares_outstanding": ads,
                              "market_cap": [m for *_, m in PENNY_COUNTS.values()]})
        return pl.concat([super().ticker_details(), extra])

    def resolve(self, tickers, on=None):
        ids = {t: sid for sid, t, *_ in self.SECS}
        return pl.DataFrame({"ticker": list(tickers), "security_id": [ids.get(t) for t in tickers]})


class PennyEdgar(FakeEdgar):
    CIKS = {t: c for _, t, _, _, c, _, _ in PENNY_ADRS}
    NAMES = {"JUNK": "JUNK BIO PLC", "LYGX": "LYGX BANKING GROUP PLC", "THIN": "THIN INDUSTRIES PLC",
             "XHGX": "XHGX HOLDINGS LTD", "FEWX": "FEWX MINING PLC"}

    def profiles(self):
        n = len(self.CIKS)
        extra = pl.DataFrame({"cik": list(self.CIKS.values()), "tickers": [[t] for t in self.CIKS],
                              "first_periodic": [D(2010, 1, 1)] * n, "last_periodic": [D(2022, 3, 1)] * n})
        return pl.concat([super().profiles(), extra])

    def names(self):
        extra = pl.DataFrame({"cik": list(self.CIKS.values()), "name": [self.NAMES[t] for t in self.CIKS],
                              "valid_from": [None] * len(self.CIKS), "valid_to": [None] * len(self.CIKS)},
                             schema={"cik": pl.String, "name": pl.String, "valid_from": pl.Date, "valid_to": pl.Date})
        return pl.concat([super().names(), extra])

    def shares(self, ciks):
        extra = pl.DataFrame([count(c, end, PENNY_COUNTS[t][0], filed) for t, c in self.CIKS.items()
                              for end, filed in [(D(2020, 12, 31), D(2021, 3, 1)), (D(2021, 12, 31), D(2022, 3, 1))]],
                             schema={"cik": pl.String, "end": pl.Date, "shares": pl.Float64, "filed": pl.Date,
                                     "available_from": pl.Date, "tag": pl.String}, orient="row")
        return pl.concat([super().shares(ciks), extra.filter(pl.col("cik").is_in(list(ciks)))])


def test_penny_adr_with_wrong_ratio_is_withheld(tmp_path):
    settings = load_settings(data_root=str(tmp_path / "vinculum"), start_year=2020)
    v = Vinculum(settings=settings, store=PennyStore(), edgar=PennyEdgar(), finra=FakeFinra())
    v.build()
    day = D(2021, 6, 1)
    junk, lygx, thin, xhgx, fewx = (cap(v, t, day) for t in ("JUNK", "LYGX", "THIN", "XHGX", "FEWX"))
    # The ratio is one number for the whole history: JUNK's $3 days are withheld too.
    assert junk["close"] == 3.0 and junk["basis"] == "implausible"
    assert junk["basis_ratio"] == pytest.approx(15) and junk["implausible"] and junk["market_cap"] is None
    assert junk["issuer_market_cap"] is None
    assert xhgx["basis"] == "implausible" and xhgx["market_cap"] is None
    b = v.basis(["S12", "S13", "S14", "S15", "S16"]).sort("security_id")
    assert b["basis"].to_list() == ["implausible", "calibrated", "calibrated", "implausible", "calibrated"]
    assert b["penny_days"][0] >= 20 and b["penny_turnover"][0] == pytest.approx(36_450 / 6.075e9)
    assert b["penny_turnover"][3] == pytest.approx(9e-5) and b["thin_days"][3] >= 20   # thin, not slow
    assert lygx["market_cap"] == pytest.approx(1.5 * 20e9 * 0.25) and not lygx["implausible"]
    assert thin["market_cap"] == pytest.approx(10e9) and not thin["implausible"]   # 2e-5 a day, but $25
    # FEWX dips under $2 on $230k a day, but at about today's value: nothing withheld.
    assert fewx["market_cap"] == pytest.approx(3e9) and not fewx["implausible"]
    late = v.market_cap("FEWX", start=DAYS[-10], end=END)
    assert not late["implausible"].any() and late["market_cap"].min() == pytest.approx(1.4e9)
    assert b["penny_days"][4] == 10 and b["thin_days"][4] == 0
    ranked = v.ranks(day)["ticker"].to_list()
    assert not {"JUNK", "XHGX"} & set(ranked) and {"LYGX", "THIN", "FEWX"} <= set(ranked)
    # Only the ADR rules withheld these (the general floor passes them): JUNK's and XHGX's days
    # with a fresh count.
    days_with_count = v.market_cap("JUNK", start=START, end=END).filter(pl.col("shares_end").is_not_null()).height
    assert v.status()["penny_adr_days"] == 2 * days_with_count
    checks = {n: ok for n, ok, _ in v.check()}
    assert checks["no ranked ADR under $2 above $5B trading under $1M a day"]


# ---------------------------------------------------------------- ADSs typed as common stock
def test_adr_names_and_ratios():
    from vinculum._names import adr_ratio, looks_adr, looks_converted
    assert looks_adr("ISOFTSTONE HLDGS LTD ADS EACH REPSTG 10 ORD SHS (CYM)") and not looks_adr("ADS-TEC Energy PLC")
    assert adr_ratio("ISOFTSTONE HLDGS LTD ADS EACH REPSTG 10 ORD SHS (CYM)") == 10
    assert adr_ratio("SAP AKTIENGESELL ADS(RP1/4 COM)") == 0.25
    assert adr_ratio("NIDEC CP (NIHON KABUSHIKI KAISHA)1 ADS REPTG .25 SH") == 0.25
    assert adr_ratio("SOS Limited American Depositary Shares, each representing five hundred (500) Class A") == 500
    assert adr_ratio("TAOMEE HLDGS LTD SPON ADR (CYM)") is None and adr_ratio("Apple Inc.") is None
    # N ADSs for M shares: M / N per ADS
    assert adr_ratio("DiDi Global Inc. American depositary shares, each four representing one Class A ordinary share") == 0.25
    assert adr_ratio("Dingdong (Cayman) Limited American Depositary Shares (each two representing three Ordinary Shares)") == 1.5
    assert adr_ratio("Onion Global Limited American Depositary Shares (each ten (10) ADSs representing one (1) Class A") == 0.1
    assert adr_ratio("ETABLISSEMENT DELHZ ADS 4 ADS REPTGS 1 ORD SHS") == 0.25
    assert adr_ratio("Honda Motor Co., Ltd. American Depositary Share, each representing three Common Stock") == 3
    assert looks_converted("AIFU Inc. Class A Ordinary Share") and not looks_converted("YOUKU TUDOU INC.(CAYMAN ISLS)")


def test_basis_overrides_file():
    from vinculum._build import load_basis_overrides
    ov = load_basis_overrides()
    assert ov.height >= 40 and ov["security_id"].is_unique().all()
    assert ov["basis_ratio"].is_not_null().all() and (ov["basis_ratio"] > 0).all()
    assert ov["note"].str.len_chars().min() > 10


def test_adr_flags_and_dated_name_ratios():
    from vinculum._build import adr_flags, adr_name_ratios, name_ratio_as_of
    attrs = pl.DataFrame({
        "security_id": ["Y", "Y", "F", "F", "H", "H", "C"],
        "as_of": [D(2011, 12, 30), D(2013, 1, 31), D(2020, 1, 31), D(2025, 6, 30), D(2010, 1, 29), D(2024, 1, 31), D(2020, 1, 31)],
        "name": ["YOUKU INC SPONSORED ADR REPSTG 18 CL A ORD SHS", "YOUKU TUDOU INC", "Fanhua Inc. American Depositary Shares",
                 "AIFU Inc. Class A Ordinary Share", "HONDA MOTOR CO ADR ONE (1) COM STK ONE (1) ADS",
                 "Honda Motor Co., Ltd. American Depositary Share, each representing three (3) shares of Common", "Acme Inc."],
        "type": ["CS", "CS", "ADRC", "CS", "CS", "ADRC", "CS"]})
    equity = pl.DataFrame({"security_id": ["Y", "F", "H", "C"], "type": ["CS", "CS", "ADRC", "CS"]})
    f = {r["security_id"]: r for r in adr_flags(attrs, equity).iter_rows(named=True)}
    assert f["Y"]["adr"] and f["Y"]["adr_now"]                    # typed CS, but an ADS by name
    assert f["F"]["adr"] and not f["F"]["adr_now"]                # converted to ordinary shares
    assert not f["C"]["adr"]
    ratios = adr_name_ratios(attrs, ["Y", "H"])
    when = pl.DataFrame({"security_id": ["H", "Y"], "date": [D(2020, 6, 19), D(2015, 6, 1)]})
    got = dict(name_ratio_as_of(ratios, when).iter_rows())
    assert got == {"H": 1.0, "Y": 18.0}                           # Honda's 2020 count: 1 share per ADS, not 3


def test_basis_for_adrs_without_massive_counts():
    from vinculum._shares import basis_ratios
    latest = pl.DataFrame({"security_id": ["ISS", "TAOM", "PLL", "ORD"], "type": ["CS", "CS", "CS", "CS"],
                           "massive_shares": [None] * 4, "shares_now": [5.7e8, 7.7e8, 2.2e7, 1e8], "classes": [1] * 4,
                           "adr": [True, True, True, False], "adr_now": [True, True, False, False],
                           "name_ratio": [10.0, None, None, None]},
                          schema_overrides={"massive_shares": pl.Float64})
    b = {r["security_id"]: (r["basis_ratio"], r["basis"]) for r in basis_ratios(latest).iter_rows(named=True)}
    assert b["ISS"] == (pytest.approx(0.1), "name") and b["TAOM"] == (None, "unknown")
    assert b["PLL"] == (1.0, "assumed") and b["ORD"] == (1.0, "assumed")


def test_counts_from_before_a_new_security_are_dropped_when_far_off():
    from vinculum._shares import drop_inherited
    o = pl.DataFrame({"security_id": ["N"] * 3 + ["M"] * 2, "cik": ["1"] * 3 + ["2"] * 2,
                      "end": [D(2014, 5, 9), D(2014, 8, 8), D(2014, 11, 7), D(2017, 12, 31), D(2018, 12, 31)],
                      "shares_now": [5.88e8, 1.47e7, 1.48e7, 1.2e8, 1.6e8]})
    ft = pl.DataFrame({"security_id": ["N", "M"], "first_trade": [D(2014, 6, 11), D(2018, 2, 1)]})
    kept = drop_inherited(o, ft, ["security_id", "cik"])
    assert kept.filter(pl.col("security_id") == "N")["end"].to_list() == [D(2014, 8, 8), D(2014, 11, 7)]  # 40x off
    assert kept.filter(pl.col("security_id") == "M").height == 2                                    # within 3x



def test_counts_from_after_a_security_stopped_trading_are_dropped_when_far_off():
    from vinculum._shares import drop_inherited
    # LATAM's old ADS: last trade June 2020; the CIK's counts after its 2022 reorganization are 1000x.
    o = pl.DataFrame({"security_id": ["L"] * 5, "cik": ["1"] * 5,
                      "end": [D(2018, 12, 31), D(2019, 12, 31), D(2020, 12, 31), D(2022, 12, 31), D(2025, 12, 31)],
                      "shares_now": [6.06e8, 6.06e8, 6.06e8, 6.05e11, 5.74e11]})
    ft = pl.DataFrame({"security_id": ["L"], "first_trade": [D(2001, 1, 1)], "last_trade": [D(2020, 6, 9)]})
    kept = drop_inherited(o, ft, ["security_id", "cik"])
    assert kept["end"].to_list() == [D(2018, 12, 31), D(2019, 12, 31), D(2020, 12, 31)]   # within 3x is kept
    # Without last_trade only the first-trade rule applies.
    assert drop_inherited(o, ft.drop("last_trade"), ["security_id", "cik"]).height == 5

def test_adr_levels_keep_a_real_1000x_issuance_but_align_ratio_changes():
    from vinculum._shares import _unit_slips
    g = ["security_id", "cik"]
    # L: ratio-1 ADS, 606M shares until a reorganization issues 1000x (605B). C: an ADS ratio
    # change, 62x fewer ordinary shares behind each ADS; both judged on the same liquidity.
    o = pl.DataFrame({"security_id": ["L"] * 4 + ["C"] * 3, "cik": ["1"] * 4 + ["2"] * 3,
                      "end": [D(2018, 12, 31), D(2019, 12, 31), D(2022, 12, 31), D(2023, 12, 31),
                              D(2017, 12, 31), D(2018, 12, 31), D(2019, 12, 31)],
                      "shares_now": [6.06e8, 6.06e8, 6.05e11, 6.04e11, 1.11e5, 6.9e6, 2.1e7]})
    liq = pl.DataFrame({"security_id": ["L"] * 4 + ["C"] * 3, "year": [2018, 2019, 2022, 2023, 2017, 2018, 2019],
                        "dv_med": [7e6, 7e6, 2e5, 2e5, 7e4, 7e4, 1.8e5], "close_med": [10.0, 10.0, 0.01, 0.01, 200.0, 3.0, 2.0]})
    out = _unit_slips(o, g, ["L", "C"], liq).sort(*g, "end")
    lt = out.filter(pl.col("security_id") == "L")
    assert lt["shares_now"].to_list()[:2] == [6.06e8, 6.06e8] and not lt["unit_fixed"].any()
    ct = out.filter(pl.col("security_id") == "C")
    assert ct["shares_now"].to_list() == pytest.approx([2.1e7] * 3)    # all on the latest level
    assert ct["unit_fixed"].to_list() == [True, True, False]


def test_turnover_uses_only_years_the_security_traded():
    from vinculum._shares import _turnover
    o = pl.DataFrame({"security_id": ["NEW", "NEW"], "cik": ["1", "1"], "end": [D(2019, 12, 31), D(2023, 12, 31)],
                      "shares_now": [1e4, 3e7]})
    liq = pl.DataFrame({"security_id": ["OLD", "NEW"], "year": [2019, 2023], "dv_med": [5e5, 2e5],
                        "close_med": [3000.0, 0.2]})
    t = _turnover(pl.concat([o, o.with_columns(pl.lit("OLD").alias("security_id"))]), liq)
    new = t.filter(pl.col("security_id") == "NEW")
    assert new["end"].to_list() == [D(2023, 12, 31)]               # 2019: the predecessor's prices, other units


# A wrong ADS level can make a liquid-looking ADR a trillion-dollar company (LATAM's old ADS in
# 2020: $2.4T on $7M a day, now fixed in _unit_slips). ADR_GIANT, the net, withholds an ADR shown
# above $50B whose 60-day median volume is under 1e-5 of that; a real $100B ADR trades far more.
def test_giant_adr_with_almost_no_volume_is_withheld():
    from vinculum._build import screen

    n = 30
    days = [date(2020, 3, 2) + timedelta(days=i) for i in range(n)]

    def rows(sid, adr, cap, dv):
        return {"security_id": [sid] * n, "date": days, "type": ["CS"] * n, "adr": [adr] * n,
                "end": [date(2019, 12, 31)] * n, "filed": [date(2020, 2, 1)] * n, "basis": ["name"] * n,
                "raw_close": [10.0] * n, "dollar_volume": [dv] * n, "dv60": [dv] * n,
                "market_cap": [cap] * n, "massive_market_cap": [None] * n}

    df = pl.concat([pl.DataFrame(rows("STALE", True, 2e12, 7e6)), pl.DataFrame(rows("REAL", True, 1e11, 2e8)),
                    pl.DataFrame(rows("US", False, 2e12, 7e6))], how="vertical_relaxed")
    out = screen(df).group_by("security_id").agg(pl.col("implausible").all(), pl.col("penny_adr").any(),
                                                  pl.col("market_cap").is_null().all().alias("withheld"))
    got = {r["security_id"]: r for r in out.iter_rows(named=True)}
    assert got["STALE"]["withheld"] and got["STALE"]["penny_adr"]
    assert not got["REAL"]["implausible"]
    # Not an ADR: only the general turnover rule applies (3.5e-6 is inside PLAUSIBLE).
    assert not got["US"]["implausible"]


def test_multi_class_needs_common_trading_days():
    from vinculum._build import MIN_COMMON_DAYS, multi_class
    # TOPS trades 2008-01-02..05-30 except 03-20..04-17, when it trades as TOPSD (after a reverse
    # split); TOPSW is a when-issued line beside TOPS for 5 days. CLA/CLB: two real classes.
    gap = set(weekdays(D(2008, 3, 20), D(2008, 4, 17)))
    tops = [d for d in weekdays(D(2008, 1, 2), D(2008, 5, 30)) if d not in gap]
    days = pl.DataFrame(
        [("TOPS", d) for d in tops] + [("TOPSD", d) for d in sorted(gap)]
        + [("TOPSW", d) for d in tops[:5]]
        + [(s, d) for s in ("CLA", "CLB") for d in weekdays(D(2008, 1, 2), D(2008, 2, 28))],
        schema={"security_id": pl.String, "date": pl.Date}, orient="row")
    secs = days.group_by("security_id").agg(pl.col("date").min().alias("first_trade"),
                                            pl.col("date").max().alias("last_trade"))
    pairs = pl.DataFrame({"security_id": ["TOPS", "TOPSD", "TOPSW", "CLA", "CLB"],
                          "cik": ["1", "1", "1", "2", "2"]})
    assert sorted(multi_class(pairs, secs)) == ["CLA", "CLB", "TOPS", "TOPSD", "TOPSW"]   # spans overlap
    assert len(weekdays(D(2008, 1, 2), D(2008, 2, 28))) >= MIN_COMMON_DAYS
    assert sorted(multi_class(pairs, secs, days)) == ["CLA", "CLB"]


# SPIN: a spin-off that Massive records as a 1000 -> 1973 split on 2021-11-02 (Dell's VMware
# spin). The price halves, the SEC's count stays 100M: market cap 6B before, 3.04B after, with
# no window where a count dated before the spin is multiplied by its ratio.
SPIN_DAY, SPIN_K = D(2021, 11, 2), 1.973
SPIN = ("S20", "SPIN", "SPIN CORP", "CS", "0000000020", 60.0, 1_000_000)


class SpinStore(FakeStore):
    SECS = [*SECURITIES, SPIN]

    def __init__(self):
        super().__init__()
        after = (pl.col("security_id") == "S20") & (pl.col("date") >= SPIN_DAY)
        self._bars = self._bars.with_columns(pl.when(after).then(pl.col("close") / SPIN_K)
                                             .otherwise(pl.col("close")).alias("close"))
        self._splits = pl.concat([self._splits, pl.DataFrame(
            {"security_id": ["S20"], "ticker": ["SPIN"], "execution_date": [SPIN_DAY],
             "split_from": [1000.0], "split_to": [1973.0]})])

    def bars(self, tickers=None, start=None, end=None, adjust=None, lazy=False, **_):
        b = super().bars(tickers, start, end, adjust=None, lazy=False)
        spun = (pl.col("security_id") == "S20") & (pl.col("date") < SPIN_DAY)
        b = b.with_columns(pl.when(spun).then(1 / SPIN_K).otherwise(pl.col("split_factor")).alias("split_factor"))
        if adjust:
            b = b.with_columns(pl.col("close") * pl.col("split_factor"), pl.col("volume") / pl.col("split_factor"))
        return b.lazy() if lazy else b

    def ticker_details(self):
        extra = pl.DataFrame({"as_of": [END], "ticker": ["SPIN"], "weighted_shares_outstanding": [100e6],
                              "share_class_shares_outstanding": [100e6], "market_cap": [3.04e9]})
        return pl.concat([super().ticker_details(), extra])

    def resolve(self, tickers, on=None):
        ids = {t: sid for sid, t, *_ in self.SECS}
        return pl.DataFrame({"ticker": list(tickers), "security_id": [ids.get(t) for t in tickers]})


class SpinEdgar(FakeEdgar):
    def profiles(self):
        return pl.concat([super().profiles(), pl.DataFrame(
            {"cik": ["0000000020"], "tickers": [["SPIN"]], "first_periodic": [D(2010, 1, 1)],
             "last_periodic": [D(2022, 3, 1)]})])

    def names(self):
        return pl.concat([super().names(), pl.DataFrame(
            {"cik": ["0000000020"], "name": ["SPIN CORP"], "valid_from": [None], "valid_to": [None]},
            schema={"cik": pl.String, "name": pl.String, "valid_from": pl.Date, "valid_to": pl.Date})])

    def shares(self, ciks):
        extra = pl.DataFrame([count("0000000020", end, 100e6, filed) for end, filed in
                              [(D(2020, 12, 31), D(2021, 2, 15)), (D(2021, 6, 30), D(2021, 8, 5)),
                               (D(2021, 9, 30), D(2021, 11, 8)), (D(2021, 12, 31), D(2022, 2, 15))]],
                             schema={"cik": pl.String, "end": pl.Date, "shares": pl.Float64, "filed": pl.Date,
                                     "available_from": pl.Date, "tag": pl.String}, orient="row")
        return pl.concat([super().shares(ciks), extra.filter(pl.col("cik").is_in(list(ciks)))])


def test_spin_off_recorded_as_a_split_is_price_only(tmp_path):
    settings = load_settings(data_root=str(tmp_path / "vinculum"), start_year=2020)
    v = Vinculum(settings=settings, store=SpinStore(), edgar=SpinEdgar(), finra=FakeFinra())
    v.build()
    a = pl.read_parquet(v.layout.file("split_attribution"))
    spin = a.filter(pl.col("security_id") == "S20").row(0, named=True)
    assert spin["price_only"] and not spin["applies"]
    assert not a.filter(pl.col("security_id") == "S1")["price_only"].any()           # ALPH's 2:1 stays a split
    m = v.market_cap(["SPIN"], start=D(2021, 10, 1), end=D(2022, 3, 31))
    before, after = m.filter(pl.col("date") < SPIN_DAY), m.filter(pl.col("date") >= SPIN_DAY)
    assert before["market_cap"].to_list() == pytest.approx([6e9] * before.height)
    assert after["market_cap"].to_list() == pytest.approx([60 / SPIN_K * 100e6] * after.height)
    assert m["shares"].to_list() == pytest.approx([100e6] * m.height)              # no share change at the spin
    assert m["split_factor"].to_list() == pytest.approx([1.0] * m.height)          # real splits only
    assert m["close"].to_list()[0] == pytest.approx(60.0)                          # raw close as traded
    # Per-share values are converted with real splits only.
    assert v._share_events(["S1", "S20"])["security_id"].to_list() == ["S1"]


def test_price_only_events_need_flat_counts_and_an_odd_ratio():
    from vinculum._shares import is_round, price_only_events
    assert is_round(2.0) and is_round(0.05) and is_round(1.5) and is_round(1 / 12) and not is_round(1.973)
    def obs(sid, a, b):
        return [(sid, "1", D(2021, 6, 30), D(2021, 8, 5), a), (sid, "1", D(2021, 12, 31), D(2022, 2, 15), b)]
    o = pl.DataFrame(obs("SPIN", 100e6, 99e6) + obs("DIV", 100e6, 115e6) + obs("RND", 100e6, 100e6)
                     + obs("YUM", 100e6, 90.8e6) + [("LONE", "1", D(2021, 6, 30), D(2021, 8, 5), 100e6)],
                     schema={"security_id": pl.String, "cik": pl.String, "end": pl.Date, "filed": pl.Date,
                             "shares": pl.Float64}, orient="row")
    ev = pl.DataFrame({"security_id": ["SPIN", "DIV", "RND", "YUM", "LONE"], "execution_date": [SPIN_DAY] * 5,
                       "k": [1.973, 1.15, 2.0, 1.391, 1.7]})
    # SPIN: counts flat. DIV: a 15% stock dividend (counts up 15%). RND: a 2:1 split (round, whatever
    # the counts say). YUM: counts down 9% (buybacks) across a 1.391 spin. LONE: no count after it.
    assert sorted(price_only_events(o, ev)["security_id"].to_list()) == ["SPIN", "YUM"]


def test_suspect_splits_tell_a_missing_split_from_a_crash():
    """A missing split: the adjusted close halves and the SEC count doubles. A crash: the close
    halves, the count stays. A split the data has: the adjusted close does not fall."""
    from datetime import date as Day

    from vinculum._suspect import suspect_splits

    days = [Day(2022, 7, 14), Day(2022, 7, 15), Day(2022, 7, 18), Day(2022, 7, 19)]
    rows = []
    for sid, closes, factors in (("MISS", [100, 100, 50, 51], [1, 1, 1, 1]),       # 2:1 split not recorded
                                 ("CRASH", [100, 100, 40, 41], [1, 1, 1, 1]),      # trial failure
                                 ("KNOWN", [100, 100, 50, 51], [0.5, 0.5, 1, 1])):  # recorded: adjusted flat
        for d, c, f in zip(days, closes, factors, strict=True):
            rows.append({"security_id": sid, "ticker": sid, "cik": sid, "date": d, "close": float(c),
                         "split_factor": float(f)})
    panel = pl.DataFrame(rows).lazy()
    counts = pl.DataFrame({
        "security_id": ["MISS", "MISS", "CRASH", "CRASH", "KNOWN", "KNOWN"],
        "filed": [Day(2022, 5, 1), Day(2022, 8, 1)] * 3,
        "shares": [1e8, 2e8, 1e8, 1.02e8, 1e8, 2e8]})
    out = suspect_splits(panel, counts)
    assert out["security_id"].to_list() == ["MISS"]
    r = out.row(0, named=True)
    assert r["date"] == Day(2022, 7, 18) and r["date_before"] == Day(2022, 7, 15)
    assert r["price_ratio"] == pytest.approx(2.0) and r["share_ratio"] == pytest.approx(2.0)
    assert suspect_splits(panel, counts, start=Day(2022, 7, 19)).is_empty()
