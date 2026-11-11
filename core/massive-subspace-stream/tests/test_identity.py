"""Security master on a small synthetic market with a fake reference API."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from massive_subspace_stream._catalog import Catalog
from massive_subspace_stream._identity.build import MAX_BLANK_LOOKUPS, TEST_SYMBOL, compare
from massive_subspace_stream._identity.checks import resolve, run_checks
from massive_subspace_stream._identity.run import OVERRIDES_SCHEMA, build_identity, load_overrides
from massive_subspace_stream._layout import Layout
from massive_subspace_stream._sources.rest import RestError
from massive_subspace_stream._storage import write_parquet


@dataclass
class Life:
    """Who a ticker really was between two dates (what the fake API answers)."""
    ticker: str
    start: date
    end: date
    price: float
    figi: str | None = None
    cik: str | None = None
    hidden: bool = False  # unknown to the vendor
    type: str = "CS"


D = date
TRUTH = [
    Life("OLD", D(2020, 1, 2), D(2020, 6, 15), 100.0, figi="F1", cik="C1"),   # renamed to NEW
    Life("NEW", D(2020, 6, 16), D(2020, 12, 31), 101.0, figi="F1", cik="C1"),
    Life("OLD", D(2020, 10, 1), D(2020, 12, 31), 5.0, figi="F2", cik="C2"),   # symbol reused after a gap
    Life("REU", D(2020, 1, 2), D(2020, 7, 13), 10.0, figi="F3", cik="C3"),    # reused without a gap
    Life("REU", D(2020, 7, 14), D(2020, 12, 31), 50.0, figi="F4", cik="C4"),
    Life("AAA", D(2020, 1, 2), D(2020, 3, 31), 20.0, cik="C5"),               # renamed, no FIGI
    Life("BBB", D(2020, 4, 1), D(2020, 5, 29), 20.2, cik="C5"),
    Life("BBB", D(2020, 6, 1), D(2020, 12, 31), 10.1, cik="C5"),              # 2-for-1 split on 06-01
    Life("REO", D(2020, 1, 2), D(2020, 5, 29), 30.0, cik="C6"),               # reorganized, new CIK
    Life("REO", D(2020, 6, 1), D(2020, 12, 31), 30.1, cik="C7"),
    Life("GAP", D(2020, 1, 2), D(2020, 3, 31), 40.0, figi="F8", cik="C8"),    # gap, same security
    Life("GAP", D(2020, 6, 1), D(2020, 12, 31), 41.0, figi="F8", cik="C8"),
    Life("CLS", D(2020, 1, 2), D(2020, 12, 31), 100.0, cik="C9"),             # share class switch (override)
    Life("CLA", D(2020, 7, 1), D(2020, 12, 31), 50.0, cik="C9"),
    Life("ETA", D(2020, 1, 2), D(2020, 3, 31), 25.0, cik="C10", type="ETF"),  # one trust, two funds
    Life("ETB", D(2020, 4, 1), D(2020, 12, 31), 25.0, cik="C10", type="ETF"),
    Life("PAR", D(2020, 1, 2), D(2020, 12, 31), 60.0, figi="F11", cik="C11"),  # + a when-issued line
    Life("PARw", D(2020, 6, 1), D(2020, 6, 12), 45.0, figi="F11", cik="C11"),
    Life("UNK", D(2020, 2, 3), D(2020, 2, 28), 1.0, hidden=True),
    Life("ZVZZT", D(2020, 1, 2), D(2020, 12, 31), 10.0, hidden=True),
]


def weekdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def record(life: Life) -> dict:
    return {"ticker": life.ticker, "name": f"{life.ticker} {life.figi or life.cik}", "type": life.type,
            "primary_exchange": "XNAS", "cik": life.cik, "composite_figi": life.figi,
            "share_class_figi": None, "active": True}


class FakeRefClient:
    def __init__(self, truth):
        self.truth = truth
        self.lookups: list[tuple[str, date]] = []

    def _alive(self, d: date):
        return [life for life in self.truth if life.start <= d <= life.end and not life.hidden]

    def get(self, path, params=None):
        ticker = path.rsplit("/", 1)[-1]
        d = date.fromisoformat(params["date"])
        self.lookups.append((ticker, d))
        for life in self._alive(d):
            if life.ticker == ticker:
                return {"results": record(life)}
        raise RestError(404, path, "Ticker not found.")

    def paginate(self, path, params=None):
        yield [record(life) for life in self._alive(date.fromisoformat(params["date"]))]


def make_bars(truth) -> pl.DataFrame:
    rows = []
    for life in truth:
        for d in weekdays(life.start, life.end):
            ts = datetime(d.year, d.month, d.day, 5, tzinfo=UTC)
            rows.append((life.ticker, d, ts, life.price, life.price, life.price, life.price, 1000.0, 10))
    return pl.DataFrame(rows, schema={"ticker": pl.String, "date": pl.Date, "timestamp": pl.Datetime("ns", "UTC"),
                                      "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                                      "close": pl.Float64, "volume": pl.Float64, "transactions": pl.Int64},
                        orient="row").sort("ticker", "date")


OVERRIDES = pl.DataFrame([
    ("break", "CLS", D(2020, 7, 1), None, None, None, None, "class A moved to CLA"),
    ("link", "CLS", D(2020, 6, 30), "CLA", D(2020, 7, 1), None, None, "class A continues as CLA"),
    ("split", "CLA", D(2020, 7, 1), None, None, 1.0, 2.0, "class distribution"),
], schema=OVERRIDES_SCHEMA, orient="row")


@pytest.fixture
def built(settings):
    layout = Layout(settings.data_root)
    layout.ensure()
    catalog = Catalog(layout.catalog_path)
    bars = make_bars(TRUTH)
    write_parquet(bars, layout.bars_day_file(2020))
    write_parquet(pl.DataFrame({"ticker": ["BBB", "PAR"], "execution_date": [D(2020, 6, 1), D(2020, 9, 1)],
                                "split_from": [1.0, 1.0], "split_to": [2.0, 3.0]}),   # PAR's: price says no
                  layout.rest_file("corporate_actions/splits"))
    write_parquet(pl.DataFrame({
        "ticker": ["NEW", "OLD", "OLD", "GAP"],
        "ex_dividend_date": [D(2020, 3, 2), D(2020, 3, 2), D(2020, 11, 2), D(2020, 7, 1)],
        "cash_amount": [1.0, 1.0, 0.05, 0.41],
        "currency": ["USD"] * 4,
        "historical_adjustment_factor": [0.99, 0.99, 0.99, 0.99],
    }), layout.rest_file("corporate_actions/dividends"))
    client = FakeRefClient(TRUTH)
    summary = build_identity(layout, catalog, client, overrides=OVERRIDES)
    master = pl.read_parquet(layout.identity_file("security_master"))
    return {"layout": layout, "catalog": catalog, "client": client, "summary": summary, "master": master,
            "bars": bars}


def sid(master, ticker, y, m, d):
    return resolve(master, ticker, date(y, m, d))


def test_rename_with_same_figi_is_one_security(built):
    m = built["master"]
    assert sid(m, "OLD", 2020, 3, 2) == sid(m, "NEW", 2020, 9, 1) == "F1"


def test_reused_symbol_after_gap_is_a_new_security(built):
    assert sid(built["master"], "OLD", 2020, 11, 2) == "F2"


def test_reuse_without_gap_found_by_bisect(built):
    m = built["master"]
    assert sid(m, "REU", 2020, 7, 13) == "F3"
    assert sid(m, "REU", 2020, 7, 14) == "F4"
    row = m.filter((pl.col("ticker") == "REU") & (pl.col("security_id") == "F4")).row(0, named=True)
    assert row["method"] == "bisect" and row["valid_from"] == date(2020, 7, 14)


def test_cik_handoff_links_rename_without_figi(built):
    m = built["master"]
    a, b = sid(m, "AAA", 2020, 2, 3), sid(m, "BBB", 2020, 9, 1)
    assert a == b == "CIK:C5:AAA"
    assert m.filter(pl.col("ticker") == "BBB")["linked_by"][0] == "cik_handoff"


def test_funds_in_one_trust_are_not_linked_by_cik(built):
    m = built["master"]
    assert sid(m, "ETA", 2020, 2, 3) != sid(m, "ETB", 2020, 9, 1)


def test_when_issued_line_is_split_off(built):
    m = built["master"]
    assert sid(m, "PAR", 2020, 6, 5) == "F11"
    assert sid(m, "PARw", 2020, 6, 5) == "F11:PARw"


def test_reorganization_with_new_cik_stays_one_segment(built):
    m = built["master"].filter(pl.col("ticker") == "REO")
    assert m.height == 1 and m["cik"][0] == "C7"          # latest attributes
    assert m["security_id"][0] == "CIK:C6:REO"             # id from the earliest CIK, so it stays stable


def test_gap_with_same_figi_is_one_security(built):
    m = built["master"].filter(pl.col("ticker") == "GAP")
    assert m.height == 2 and m["security_id"].unique().to_list() == ["F8"]


def test_unknown_and_test_symbols(built):
    m = built["master"]
    unk = m.filter(pl.col("ticker") == "UNK").row(0, named=True)
    assert unk["security_id"] == "SYM:UNK:2020-02-03" and unk["method"] == "unresolved"
    assert m.filter(pl.col("ticker") == "ZVZZT")["is_test"].all()
    assert TEST_SYMBOL.match("ZXZZT") and not TEST_SYMBOL.match("ZION")


def test_overrides_break_link_and_split(built):
    m, layout = built["master"], built["layout"]
    pre, post, cla = sid(m, "CLS", 2020, 6, 30), sid(m, "CLS", 2020, 7, 1), sid(m, "CLA", 2020, 7, 1)
    assert pre == cla and post != pre
    splits = pl.read_parquet(layout.identity_file("splits"))
    ov = splits.filter(pl.col("source") == "override").row(0, named=True)
    assert ov["security_id"] == cla and ov["execution_date"] == date(2020, 7, 1)
    # Massive's BBB split lands on the AAA/BBB security; PAR's is contradicted by its price.
    assert splits.filter(pl.col("ticker") == "BBB")["security_id"][0] == "CIK:C5:AAA"
    assert splits.filter(pl.col("ticker") == "BBB")["price_check"][0] == "confirmed"
    assert splits.filter(pl.col("ticker") == "PAR").is_empty()


def test_dividends_follow_current_ticker_and_dedupe(built):
    divs = pl.read_parquet(built["layout"].identity_file("dividends"))
    f1 = divs.filter(pl.col("security_id") == "F1")
    assert f1.height == 1  # NEW and OLD copies of the same March dividend
    assert f1["factor"][0] == pytest.approx(1 - 1.0 / 100.0)
    assert divs.filter(pl.col("ticker") == "OLD", pl.col("ex_dividend_date") == date(2020, 11, 2))["security_id"][0] == "F2"
    assert divs.filter(pl.col("ticker") == "GAP")["security_id"][0] == "F8"


def test_checks_and_cached_lookups(built, settings):
    layout, catalog, client = built["layout"], built["catalog"], built["client"]
    master = built["master"]
    securities = pl.read_parquet(layout.identity_file("securities"))
    report = run_checks(master, securities, built["bars"])
    assert report["overlapping_windows"] == 0
    assert report["bar_days_mapped_pct"] == 100.0
    assert report["securities_with_several_tickers"] >= 3
    assert built["summary"]["lookups_needed_left"] == 0
    # A rebuild uses the cache: no new lookups or snapshots.
    before = len(client.lookups)
    again = build_identity(layout, catalog, client, overrides=OVERRIDES)
    assert len(client.lookups) == before and again["snapshots_fetched"] == 0
    assert pl.read_parquet(layout.identity_file("security_master")).equals(master)


def test_compare_and_packaged_overrides():
    assert compare({"composite_figi": "A", "cik": "1"}, {"composite_figi": "A", "cik": "2"}) == ("same", None)
    assert compare({"composite_figi": "A"}, {"composite_figi": "B"}) == ("figi", "composite_figi")
    assert compare({"cik": "1"}, {"cik": "2"}) == ("cik", "cik")
    assert compare({"cik": "1"}, {"composite_figi": "B"}) == ("same", None)
    ov = load_overrides()
    assert set(ov["action"]) <= {"break", "link", "split"} and ov["note"].null_count() == 0


# ---------------------------------------------------------------------------- Store integration
@pytest.fixture
def store(built, settings):
    from massive_subspace_stream import Store

    return Store(settings=settings)


def test_bars_by_security_spans_renames(store):
    df = store.bars("NEW", start="2020-01-01", end="2020-12-31", by="security")
    assert set(df["ticker"]) == {"OLD", "NEW"} and set(df["security_id"]) == {"F1"}
    assert df["date"].min() == date(2020, 1, 2) and df.filter(pl.col("ticker") == "OLD")["date"].max() == date(2020, 6, 15)
    # by ticker: a reused symbol returns both securities, told apart by security_id.
    old = store.bars("OLD", start="2020-01-01", end="2020-12-31")
    assert set(old["security_id"]) == {"F1", "F2"}


def test_adjustment_follows_the_security(store):
    df = store.bars("OLD", start="2020-01-01", end="2020-12-31", adjust="all")
    f1 = df.filter(pl.col("date") == date(2020, 2, 3))
    assert f1["close"][0] == pytest.approx(99.0)        # F1's own dividend only, not F2's
    f2 = df.filter(pl.col("date") == date(2020, 10, 1))
    assert f2["close"][0] == pytest.approx(5.0 * 0.99)  # F2's dividend
    # BBB's split adjusts AAA's history (same security after the rename).
    aaa = store.bars("AAA", start="2020-01-01", end="2020-03-31", adjust="splits")
    assert aaa["close"][0] == pytest.approx(10.0) and aaa["split_factor"][0] == pytest.approx(0.5)


def test_universe_resolve_and_securities(store):
    u = store.universe("2020-11-02")
    old = u.filter(pl.col("ticker") == "OLD").row(0, named=True)
    assert old["security_id"] == "F2" and old["type"] == "CS" and old["cik"] == "C2"
    assert store.universe("2020-03-02").filter(pl.col("ticker") == "OLD")["security_id"][0] == "F1"
    r = store.resolve(["OLD", "NEW"], on="2020-11-02")
    assert dict(zip(r["ticker"], r["security_id"])) == {"OLD": "F2", "NEW": "F1"}
    assert store.securities(tickers="OLD").height == 2
    assert store.security_master("REU").height == 2


def test_malformed_stock_dividends_are_read_as_small_dividends():
    from massive_subspace_stream._identity.actions import fix_stock_dividends

    s = pl.DataFrame({"split_from": [1.0, 1.0, 1.0], "split_to": [91.625, 2.0, 50.0],
                      "adjustment_type": ["stock_dividend", "stock_dividend", "forward_split"]})
    out = fix_stock_dividends(s)
    assert out["split_from"].to_list() == [90.625, 1.0, 1.0]   # 1 -> 91.6 is a 1.1% dividend; 2:1 and splits kept


def test_price_chooses_the_reading_of_a_large_stock_dividend():
    """Massive files Alphabet's 20-for-1 split (2022) as a 1 -> 20 stock dividend, like
    TotalEnergies' scrip dividends: the security's own close decides which one it is."""
    from massive_subspace_stream._identity.actions import (
        check_splits_against_prices,
        fix_stock_dividends,
        read_stock_dividends,
    )

    raw = fix_stock_dividends(pl.DataFrame({
        "split_from": [1.0, 1.0, 1.0], "split_to": [20.0, 91.625, 20.0],
        "adjustment_type": ["stock_dividend"] * 3}))
    splits = raw.with_columns(pl.Series("security_id", ["GOOGL", "TOT", "NOBARS"]),
                              pl.Series("execution_date", [D(2022, 7, 18), D(2019, 3, 15), D(2020, 1, 2)]),
                              pl.lit("massive").alias("source"))
    closes = pl.DataFrame({
        "security_id": ["GOOGL", "GOOGL", "TOT", "TOT"],
        "date": [D(2022, 7, 15), D(2022, 7, 18), D(2019, 3, 14), D(2019, 3, 15)],
        "close": [2235.55, 109.03, 58.37, 57.82]}).lazy()
    out = read_stock_dividends(splits, closes)
    assert "__split_from_literal" not in out.columns
    got = dict(zip(out["security_id"], out["split_from"]))
    assert got == {"GOOGL": 1.0, "TOT": 90.625, "NOBARS": 19.0}   # untestable: the (N-1) -> N reading
    checked = check_splits_against_prices(out, closes)
    assert checked.filter(pl.col("security_id") == "GOOGL")["price_check"][0] == "confirmed"


class NoFigiLookups(FakeRefClient):
    """Snapshots carry FIGIs; dated lookups answer without them (Massive does this for some
    leveraged ETFs across a reverse split)."""

    def get(self, path, params=None):
        res = super().get(path, params)
        return {"results": {**res["results"], "composite_figi": None}}


def test_search_gives_up_on_answers_without_the_id(settings):
    truth = [Life("LEV", D(2020, 1, 2), D(2020, 7, 13), 10.0, figi="F20", cik="C20", type="ETF"),
             Life("LEV", D(2020, 7, 14), D(2020, 12, 31), 40.0, figi="F21", cik="C20", type="ETF")]  # 1:4 reverse split
    layout = Layout(settings.data_root)
    layout.ensure()
    catalog = Catalog(layout.catalog_path)
    write_parquet(make_bars(truth), layout.bars_day_file(2020))
    client = NoFigiLookups(truth)
    summary = build_identity(layout, catalog, client, overrides=OVERRIDES.clear())
    asked = [d for t, d in client.lookups if t == "LEV"]
    assert len(asked) <= MAX_BLANK_LOOKUPS            # was one per round until max_rounds
    assert summary["lookups_needed_left"] == 0
    master = pl.read_parquet(layout.identity_file("security_master"))
    assert resolve(master, "LEV", D(2020, 7, 13)) == "F20"
    assert resolve(master, "LEV", D(2020, 7, 14)) == "F21"   # placed at the price jump
    client.lookups.clear()
    build_identity(layout, catalog, client, overrides=OVERRIDES.clear())
    assert client.lookups == []                         # and never asked again
