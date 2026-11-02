"""Offline tests: FINRA's API and CDN are replaced by in-memory fakes."""

from __future__ import annotations

from datetime import date

import httpx
import polars as pl
import pytest
from finra_sensor_array import FinraStore, Ingestor, load_settings
from finra_sensor_array._calendar import business_days, si_published
from finra_sensor_array._client import FinraClient
from finra_sensor_array._parse import combine_facilities, finra_symbols, parse_daily_file

D = date


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for var in ("FINRA_DATA_ROOT", "COLLECTIVE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path):
    return load_settings(data_root=tmp_path / "finra", workers=2)


def si_row(symbol, qty, market="NYSE", name=None):
    return {"symbolCode": symbol, "issueName": name or f"{symbol} Inc", "marketClassCode": market,
            "currentShortPositionQuantity": qty, "previousShortPositionQuantity": qty - 10,
            "changePreviousNumber": 10, "changePercent": 1.0, "averageDailyVolumeQuantity": 1000,
            "daysToCoverQuantity": qty / 1000, "stockSplitFlag": None, "revisionFlag": None,
            "settlementDate": "x", "accountingYearMonthNumber": 0, "issuerServicesGroupExchangeCode": "A"}


def daily(day: date, rows, exempt=True) -> str:
    head = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market" if exempt else \
        "Date|Symbol|ShortVolume|TotalVolume|Market"
    lines = [head]
    for sym, short, total, market in rows:
        lines.append(f"{day:%Y%m%d}|{sym}|{short}|0|{total}|{market}" if exempt
                     else f"{day:%Y%m%d}|{sym}|{short}|{total}|{market}")
    return "\n".join(lines) + f"\n{len(rows)}\n"


class FakeClient:
    def __init__(self, si=None, files=None):
        self.si, self.files, self.calls = si or {}, files or {}, []

    def short_interest(self, settlement):
        self.calls.append(("si", settlement))
        return self.si.get(settlement, [])

    def daily_file(self, facility, day):
        self.calls.append((facility, day))
        return self.files.get((facility, day))


# ---------------------------------------------------------------- calendar and parsing
def test_publication_dates_match_finra_calendar():
    assert si_published(D(2025, 12, 31)) == D(2026, 1, 12)   # New Year's Day skipped
    assert si_published(D(2026, 7, 15)) == D(2026, 7, 24)
    assert si_published(D(2026, 7, 31)) == D(2026, 8, 11)
    assert D(2025, 12, 25) not in business_days(D(2025, 12, 22), D(2025, 12, 26))


def test_parse_daily_files_old_and_new():
    new = parse_daily_file(daily(D(2025, 10, 31), [("BRK/B", 10, 30, "B,Q,N"), ("NA", 1, 2, "Q")]))
    assert new["symbol"].to_list() == ["BRK/B", "NA"]                       # "NA" is a ticker, not null
    assert new["short_exempt_volume"].to_list() == [0, 0] and new.height == 2  # trailer line dropped
    crlf = parse_daily_file(daily(D(2010, 12, 13), [("A", 5, 9, "Q")], exempt=False).replace("\n", "\r\n"))
    headless = parse_daily_file("20101122|A|5|9|Q\n20101122|B|1|2|Q\n2\n")
    assert crlf["market"].to_list() == ["Q"] and headless["symbol"].to_list() == ["A", "B"]
    old = parse_daily_file(daily(D(2009, 8, 3), [("A", 5, 9, "Q")], exempt=False))
    assert old["short_exempt_volume"].to_list() == [None] and old["total_volume"].to_list() == [9]


def test_facilities_are_summed():
    d = D(2012, 6, 1)
    a = parse_daily_file(daily(d, [("A", 5, 10, "Q"), ("B", 1, 1, "Q")], exempt=False))
    b = parse_daily_file(daily(d, [("A", 3, 4, "N")], exempt=False))
    df = combine_facilities([a, b], "TRF+ADF")
    assert df.filter(pl.col("symbol") == "A").row(0, named=True) == {
        "date": d, "symbol": "A", "short_volume": 8, "short_exempt_volume": None, "total_volume": 14,
        "markets": "N,Q", "source": "TRF+ADF"}


def test_finra_symbols():
    assert finra_symbols(["BRK.B", "AAPL", "ABRpD"], "short_interest") == ["BRKB", "AAPL", "ABRPRD"]
    assert finra_symbols(["BRK.B", "ABRpD"], "short_volume") == ["BRK/B", "ABRpD"]
    assert finra_symbols(["NTGr"], "short_interest") == ["NTGr"]          # rights: never Netgear (NTGR)


# ---------------------------------------------------------------- client
def test_client_pages_retries_and_missing_files():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.host == "cdn.finra.org":
            return httpx.Response(403) if "CNMS" in str(request.url) else httpx.Response(200, text="x")
        body = request.read()
        if len(calls) == 1:
            return httpx.Response(429)
        offset = 0 if b'"offset":0' in body else 1
        return httpx.Response(200, json=[{"n": offset}], headers={"record-total": "2"})

    c = FinraClient("ua", requests_per_second=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.short_interest("2025-10-15") == [{"n": 0}, {"n": 1}]
    assert c.daily_file("CNMS", D(2009, 8, 3)) is None
    assert c.daily_file("FNSQ", D(2009, 8, 3)) == "x"
    assert calls[1].headers["User-Agent"] == "ua"


# ---------------------------------------------------------------- ingest and store
def test_short_interest_backfill_revisions_and_empty_days(settings):
    fake = FakeClient(si={"2025-09-30": [si_row("BRKB", 100), si_row("AAPL", 50, "NNM")],
                          "2025-10-15": [si_row("AAPL", 60, "NNM")]})
    ing = Ingestor(settings, client=fake, today=lambda: D(2025, 10, 27))
    r = ing.short_interest(D(2025, 9, 26), D(2025, 10, 27))
    assert r.ok and r.done == 2 and r.rows == 3
    # Days after 2025-10-15 are not yet settled (published < 3 business days ago): not recorded as empty.
    empty = set(ing.layout.read_state()["short_interest"]["empty"])
    assert "2025-09-29" in empty and "2025-10-16" not in empty
    store = FinraStore(settings=settings)
    aapl = store.short_interest("AAPL")
    assert aapl["short_interest"].to_list() == [50, 60]
    assert aapl["published_date"].to_list() == [si_published(D(2025, 9, 30)), si_published(D(2025, 10, 15))]
    assert store.short_interest("BRK.B")["symbol"].to_list() == ["BRKB"]
    assert store.short_interest("aapl").height == 2 and store.short_interest("BRKr").is_empty()
    assert store.short_interest(start="2025-10-01", date_by="published")["settlement_date"].unique().to_list() == \
        [D(2025, 9, 30), D(2025, 10, 15)]

    # A revision: the recent date is fetched again and its rows replaced; old ones are not.
    fake.si["2025-10-15"] = [si_row("AAPL", 65, "NNM")]
    fake.calls.clear()
    ing2 = Ingestor(settings, client=fake, today=lambda: D(2025, 10, 28))
    ing2.short_interest(D(2025, 9, 26), D(2025, 10, 28))
    asked = {c[1] for c in fake.calls}
    assert "2025-10-15" in asked and "2025-09-29" not in asked
    assert store.short_interest("AAPL")["short_interest"].to_list() == [50, 65]


def test_short_volume_before_and_after_cnms(settings):
    d_old, d_new = D(2018, 7, 31), D(2018, 8, 1)
    files = {("FNSQ", d_old): daily(d_old, [("A", 5, 10, "Q")]), ("FNYX", d_old): daily(d_old, [("A", 1, 2, "N")]),
             ("CNMS", d_new): daily(d_new, [("A", 7, 20, "B,Q,N"), ("BF/B", 1, 3, "N")]),
             ("FORF", d_new): daily(d_new, [("OTCX", 2, 5, "O")])}
    fake = FakeClient(files=files)
    ing = Ingestor(settings, client=fake, today=lambda: D(2018, 8, 3))
    r = ing.short_volume(D(2018, 7, 31), D(2018, 8, 3))
    assert r.ok and r.done == 2
    assert ("CNMS", d_old) not in fake.calls and ("FNSQ", d_new) not in fake.calls
    sv = FinraStore(settings=settings).short_volume("A")
    assert sv["short_volume"].to_list() == [6, 7] and sv["source"].to_list() == ["TRF+ADF", "CNMS"]
    assert FinraStore(settings=settings).short_volume("BF.B")["total_volume"].to_list() == [3]
    assert FinraStore(settings=settings).short_volume("OTCX")["source"].to_list() == ["ORF"]   # OTC stock
    # 08-02 and 08-03 have no file yet: tried again next time, not recorded as empty.
    assert ing.layout.read_state()["short_volume"]["empty"] == []


def test_sync_needs_a_backfill_first(settings):
    results = Ingestor(settings, client=FakeClient()).sync()
    assert all("never backfilled" in r.note for r in results)


def test_service_plist(settings):
    from finra_sensor_array import _services

    plist = _services.build_plist(settings)
    assert plist["Label"] == "com.collective-alpha.sensor.sync"
    assert plist["ProgramArguments"][-1] == "sync"
