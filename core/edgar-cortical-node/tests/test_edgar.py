"""edgar-cortical-node on small fixture companies, with a fake SEC client."""

import io
import zipfile
from datetime import date
from pathlib import Path

import orjson
import polars as pl
import pytest
from edgar_cortical_node import CONCEPTS, EdgarStore, Ingestor, load_settings
from edgar_cortical_node._config import ConfigError
from edgar_cortical_node._parse import parse_companyfacts, parse_master_index


# ---------------------------------------------------------------- fixture companies and a fake SEC
def fact(start, end, val, accn, filed, form="10-Q", fy=2023, fp="Q1"):
    row = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start:
        row["start"] = start
    return row


def acme(extra_revenue=()) -> dict:
    """Calendar-year company: restated Q1, YTD-only Q3, Q4 from the 10-K, a 2022 tag switch."""
    q = "2023"
    revenue = [
        fact(f"{q}-01-01", f"{q}-03-31", 100, "A1", "2023-05-01"),
        fact(f"{q}-01-01", f"{q}-03-31", 105, "B1", "2024-05-01", fy=2024),    # restated later
        fact(f"{q}-04-01", f"{q}-06-30", 110, "A2", "2023-08-01", fp="Q2"),
        fact(f"{q}-01-01", f"{q}-06-30", 210, "A2", "2023-08-01", fp="Q2"),    # 6-month YTD
        fact(f"{q}-01-01", f"{q}-09-30", 330, "A3", "2023-11-01", fp="Q3"),    # 9-month YTD only
        fact(f"{q}-01-01", f"{q}-12-31", 460, "A4", "2024-02-15", form="10-K", fp="FY"),
        *extra_revenue,
    ]
    old_tag = [fact("2022-10-01", "2022-12-31", 90, "A0", "2023-02-15", form="10-K", fy=2022, fp="FY")]
    cfo = [
        fact(f"{q}-01-01", f"{q}-03-31", 50, "A1", "2023-05-01"),
        fact(f"{q}-01-01", f"{q}-06-30", 120, "A2", "2023-08-01", fp="Q2"),
        fact(f"{q}-01-01", f"{q}-09-30", 200, "A3", "2023-11-01", fp="Q3"),
        fact(f"{q}-01-01", f"{q}-12-31", 300, "A4", "2024-02-15", form="10-K", fp="FY"),
    ]
    assets = [fact(None, f"{q}-03-31", 1000, "A1", "2023-05-01"),
              fact(None, f"{q}-06-30", 1100, "A2", "2023-08-01", fp="Q2")]
    shares = [fact(None, "2023-04-25", 1_000_000, "A1", "2023-05-01"),
              fact(None, "2023-07-25", 1_010_000, "A2", "2023-08-01", fp="Q2"),
              fact(None, "2023-10-25", 1_010, "A3", "2023-11-01", fp="Q3"),        # unit slip
              fact(None, "2024-02-01", 1_020_000, "A4", "2024-02-15", form="10-K", fp="FY")]
    eps = [fact(f"{q}-01-01", f"{q}-03-31", 0.5, "A1", "2023-05-01")]
    return {"cik": 1111, "entityName": "ACME CORP", "facts": {
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": shares}}},
        "us-gaap": {
            "Revenues": {"units": {"USD": revenue}},
            "SalesRevenueNet": {"units": {"USD": old_tag + [fact(f"{q}-04-01", f"{q}-06-30", 999, "A2", "2023-08-01", fp="Q2")]}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": cfo}},
            "Assets": {"units": {"USD": assets}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": eps}},
            "DocumentText": {"units": {"pure": [{"end": "2023-03-31", "val": "n/a", "accn": "A1", "filed": "2023-05-01"}]}},
        }}}


def multiclass() -> dict:
    dei = [fact(None, "2023-04-20", 2_000, "M1", "2023-04-28"), fact(None, "2023-04-20", 300, "M1", "2023-04-28")]
    wavg = [fact("2023-01-01", "2023-03-31", 2_290, "M1", "2023-04-28"),
            fact("2023-04-01", "2023-06-30", 2_280, "M2", "2023-07-28", fp="Q2"),
            fact("2023-01-01", "2023-06-30", 2_285, "M2", "2023-07-28", fp="Q2")]
    return {"cik": 2222, "entityName": "MULTI CLASS INC", "facts": {
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": dei}}},
        "us-gaap": {"WeightedAverageNumberOfSharesOutstandingBasic": {"units": {"shares": wavg}}}}}


def ifrs_filer() -> dict:
    rev = [fact("2023-01-01", "2023-12-31", 5_000, "F1", "2024-04-10", form="20-F", fp="FY")]
    return {"cik": 3333, "entityName": "EURO SA", "facts": {"ifrs-full": {"Revenue": {"units": {"EUR": rev}}}}}


def make_zip(docs: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for d in docs:
            zf.writestr(f"CIK{d['cik']:010d}.json", orjson.dumps(d))
    return buf.getvalue()


class FakeSec:
    """Stands in for SecClient: a bulk zip, daily indexes and per-company facts."""

    def __init__(self, docs: list[dict], last_modified="Wed, 01 May 2024 04:00:00 GMT"):
        self.zip_bytes = make_zip(docs)
        self.docs = {f"{d['cik']:010d}": d for d in docs}
        self.modified = last_modified
        self.indexes: dict[str, str] = {}
        self.requests: list[str] = []

    def last_modified(self, url):
        return self.modified

    def download(self, url, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.zip_bytes)
        self.requests.append(url)
        return len(self.zip_bytes)

    def get_text(self, url):
        self.requests.append(url)
        ymd = url.rsplit(".", 2)[-2]
        return self.indexes.get(ymd)

    def get_json(self, url):
        self.requests.append(url)
        return self.docs.get(url.split("CIK")[1][:10])


def master_index(rows: list[tuple]) -> str:
    head = "Description: Master Index\n\nCIK|Company Name|Form Type|Date Filed|Filename\n" + "-" * 40 + "\n"
    return head + "\n".join("|".join(map(str, r)) for r in rows) + "\n"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    for var in ("EDGAR_DATA_ROOT", "EDGAR_USER_AGENT", "COLLECTIVE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path):
    return load_settings(data_root=tmp_path / "edgar", parse_workers=2, workers=2)

D = date


@pytest.fixture
def loaded(settings):
    fake = FakeSec([acme(), multiclass(), ifrs_filer()])
    ing = Ingestor(settings, client=fake)
    result = ing.bulk()
    return {"ingestor": ing, "fake": fake, "result": result, "store": EdgarStore(settings=settings)}


def q(df, end):
    return df.filter((pl.col("end") == end) & (pl.col("period") == "quarter")).row(0, named=True)


def test_parse_keeps_numeric_facts_only():
    df = parse_companyfacts(acme())
    assert df["cik"].unique().to_list() == ["0000001111"]
    assert "DocumentText" not in df["tag"].to_list()
    assert df.filter(pl.col("tag") == "Assets")["start"].null_count() == 2


def test_bulk_load(loaded):
    store, res = loaded["store"], loaded["result"]
    assert res.ok and res.done == 3
    assert set(store.companies()["name"]) == {"ACME CORP", "MULTI CLASS INC", "EURO SA"}
    state = store.layout.read_state()
    assert state["sync_through"] == "2024-04-30"
    # Every filing is kept: the Q1 value and its restatement.
    q1 = store.facts(1111, tags="us-gaap:Revenues", start="2023-03-31", end="2023-03-31")
    assert sorted(q1["val"].to_list()) == [100.0, 105.0]


def test_quarters_derived_from_ytd_and_annual(loaded):
    rev = loaded["store"].series("revenue", ciks=1111)
    assert q(rev, D(2023, 3, 31))["value"] == 100            # first-filed, not the restatement
    q3 = q(rev, D(2023, 9, 30))
    assert q3["value"] == 120 and q3["source"] == "derived" and q3["filed"] == D(2023, 11, 1)
    q4 = q(rev, D(2023, 12, 31))
    assert q4["value"] == 130 and q4["available_from"] == D(2024, 2, 16)
    assert q4["ttm"] == 460                                    # 100 + 110 + 120 + 130
    assert q(rev, D(2023, 9, 30))["ttm"] == 420                # includes Q4 2022 under the old tag
    assert q(rev, D(2022, 12, 31))["tag"] == "us-gaap:SalesRevenueNet"
    assert q(rev, D(2023, 6, 30))["value"] == 110              # Revenues beats SalesRevenueNet (999)
    latest = loaded["store"].series("revenue", ciks=1111, policy="latest")
    assert q(latest, D(2023, 3, 31))["value"] == 105 and q(latest, D(2023, 12, 31))["ttm"] == 465
    cfo = loaded["store"].series("operating_cash_flow", ciks=1111).filter(pl.col("period") == "quarter")
    assert cfo.sort("end")["value"].to_list() == [50, 70, 80, 100]


def test_instants_per_share_and_annual_only_filers(loaded):
    s = loaded["store"]
    assets = s.series("assets", ciks=1111)
    assert assets["value"].to_list() == [1000, 1100] and set(assets["period"]) == {"instant"}
    eps = s.series("eps_diluted", ciks=1111)
    assert eps.row(0, named=True)["value"] == 0.5
    euro = s.series("revenue", ciks=3333)
    row = euro.row(0, named=True)
    assert row["period"] == "annual" and row["unit"] == "EUR" and row["ttm"] == 5000


def test_shares_concept_choice_and_outliers(loaded):
    sh = loaded["store"].shares([1111, 2222])
    acme_sh = sh.filter(pl.col("cik") == "0000001111")
    assert acme_sh["shares"].to_list() == [1_000_000, 1_010_000, 1_020_000]   # unit slip dropped
    assert acme_sh["tag"][0] == "dei:EntityCommonStockSharesOutstanding"
    multi = sh.filter(pl.col("cik") == "0000002222")
    assert multi["tag"][0] == "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"   # per-class dei skipped
    assert multi.filter(pl.col("end") == D(2023, 6, 30))["shares"][0] == 2_280          # quarter, not YTD


def test_as_of_respects_filing_dates(loaded):
    s = loaded["store"]
    before = s.as_of("2023-11-01", ciks=1111, items=["revenue"])
    rev = before.filter(pl.col("item") == "revenue").row(0, named=True)
    assert rev["end"] == D(2023, 6, 30)            # Q3 was filed on 11-01: usable only from 11-02
    after = s.as_of("2023-11-02", ciks=1111, items=["revenue"]).filter(pl.col("item") == "revenue").row(0, named=True)
    assert after["end"] == D(2023, 9, 30) and after["ttm"] == 420
    shares = s.as_of("2023-11-02", ciks=1111, items=[]).filter(pl.col("item") == "shares").row(0, named=True)
    assert shares["value"] == 1_010_000
    stale = s.as_of("2025-06-30", ciks=1111, items=["revenue"])
    assert stale.filter(pl.col("item") == "revenue").height == 0   # last period ended > 400 days before


def test_sync_refreshes_only_filers(loaded, settings):
    ing, fake = loaded["ingestor"], loaded["fake"]
    new_q1 = fact("2024-01-01", "2024-03-31", 140, "C1", "2024-05-02", fy=2024)
    fake.docs["0000001111"] = acme(extra_revenue=[new_q1])
    fake.indexes["20240502"] = master_index([(1111, "ACME CORP", "10-Q", "20240502", "edgar/data/1111/x.txt"),
                                             (9999, "SOMEONE", "4", "20240502", "edgar/data/9999/y.txt")])
    fake.requests.clear()
    res = ing.sync(through=D(2024, 5, 3))
    assert res.ok and res.done == 1
    assert sum("companyfacts" in r for r in fake.requests) == 1        # form 4 filer not fetched
    rev = EdgarStore(settings=settings).series("revenue", ciks=1111)
    assert q(rev, D(2024, 3, 31))["value"] == 140
    assert EdgarStore(settings=settings).layout.read_state()["sync_through"] == "2024-05-03"
    # Other companies untouched.
    assert EdgarStore(settings=settings).series("revenue", ciks=3333).height == 1


def test_bulk_skips_unchanged_file(loaded):
    again = loaded["ingestor"].bulk()
    assert again.note.startswith("bulk file unchanged")


def test_master_index_and_settings():
    idx = parse_master_index(master_index([(320193, "Apple Inc.", "10-Q", "20240503", "edgar/data/320193/a.txt")]))
    assert idx.row(0, named=True)["cik"] == "0000320193" and idx["filed"][0] == D(2024, 5, 3)
    with pytest.raises(ConfigError):
        load_settings(user_agent="no contact")
    assert all(c.tags for c in CONCEPTS.values())


def test_cli_show_status_and_service_plist(loaded, settings, monkeypatch):
    from edgar_cortical_node import _services
    from edgar_cortical_node.cli import app
    from typer.testing import CliRunner

    monkeypatch.setenv("EDGAR_DATA_ROOT", str(settings.data_root))
    runner = CliRunner()
    out = runner.invoke(app, ["show", "1111", "--items", "revenue"])
    assert out.exit_code == 0, out.output
    assert "ACME CORP" in out.output and "shares outstanding: 1,020,000" in out.output
    st = runner.invoke(app, ["status"])
    assert st.exit_code == 0 and '"companies": 3' in st.output
    monkeypatch.setattr(_services, "cortical_executable", lambda: "/bin/cortical")
    plist = _services.build_plist(settings)
    assert plist["ProgramArguments"] == ["/bin/cortical", "sync"]
    assert plist["EnvironmentVariables"]["EDGAR_DATA_ROOT"] == str(settings.data_root)
    assert {d["Weekday"] for d in plist["StartCalendarInterval"]} == {2, 3, 4, 5, 6}


def test_restated_annual_does_not_make_negative_revenue():
    from edgar_cortical_node._pit import build_series

    doc = {"cik": 4444, "entityName": "RESTATER", "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        fact("2023-01-01", "2023-09-30", 500, "R3", "2023-11-01", fp="Q3"),
        fact("2023-07-01", "2023-09-30", 170, "R3", "2023-11-01", fp="Q3"),
        fact("2023-01-01", "2023-12-31", 420, "R4", "2024-02-15", form="10-K", fp="FY"),  # disc. ops restated
    ]}}}}}
    s = build_series(parse_companyfacts(doc), "revenue", CONCEPTS["revenue"])
    assert s.filter(pl.col("end") == D(2023, 12, 31), pl.col("period") == "quarter").height == 0
    assert s.filter(pl.col("period") == "annual")["value"].to_list() == [420]


def test_semiannual_filer_gets_halves_and_ttm():
    from edgar_cortical_node._pit import build_series, latest_as_of

    doc = {"cik": 5555, "entityName": "HALFER PLC", "facts": {"ifrs-full": {"Revenue": {"units": {"USD": [
        fact("2023-01-01", "2023-06-30", 400, "H1", "2023-09-01", form="6-K", fp="Q2"),
        fact("2023-01-01", "2023-12-31", 900, "H2", "2024-04-01", form="20-F", fp="FY"),
        fact("2024-01-01", "2024-06-30", 450, "H3", "2024-09-02", form="6-K", fp="Q2", fy=2024),
    ]}}}}}
    s = build_series(parse_companyfacts(doc), "revenue", CONCEPTS["revenue"])
    halves = s.filter(pl.col("period") == "half").sort("end")
    assert halves["value"].to_list() == [400, 500, 450]           # H2 2023 = 900 - 400
    assert halves["ttm"].to_list()[1:] == [900, 950]               # 400 + 500, 500 + 450
    pick = latest_as_of(s, D(2024, 9, 3), ["cik", "item"])
    assert pick["period"][0] == "half" and pick["ttm"][0] == 950
    # A quarterly filer's six-month YTD figure is not reported as a half.
    q = build_series(parse_companyfacts(acme()), "revenue", CONCEPTS["revenue"])
    assert q.filter(pl.col("period") == "half").height == 0


def test_tag_switch_keeps_the_original_filing_date():
    from edgar_cortical_node._pit import build_series

    doc = {"cik": 6666, "entityName": "SWITCHER", "facts": {"us-gaap": {
        "SalesRevenueNet": {"units": {"USD": [fact("2017-04-01", "2017-06-30", 200, "S1", "2017-08-01", fy=2017, fp="Q2")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            fact("2017-04-01", "2017-06-30", 201, "S2", "2018-08-01", fy=2018, fp="Q2")]}}}}}
    row = build_series(parse_companyfacts(doc), "revenue", CONCEPTS["revenue"]).row(0, named=True)
    assert (row["value"], row["filed"]) == (200, D(2017, 8, 1))       # known in 2017, under the old tag
    latest = build_series(parse_companyfacts(doc), "revenue", CONCEPTS["revenue"], policy="latest").row(0, named=True)
    assert latest["value"] == 201


def test_shares_keep_splits_fill_gaps_and_skip_disagreeing_concepts():
    from edgar_cortical_node._pit import build_shares

    def rows(tag, vals):
        tax, name = tag.split(":")
        return [{"cik": "0000004444", "taxonomy": tax, "tag": name, "unit": "shares", "start": None,
                 "end": date.fromisoformat(e), "val": v, "accn": a, "fy": 2020, "fp": "Q1", "form": "10-Q",
                 "filed": date.fromisoformat(f), "frame": None} for e, v, a, f in vals]

    dei = rows("dei:EntityCommonStockSharesOutstanding", [
        ("2019-04-20", 1.00e8, "D1", "2019-05-01"), ("2019-07-20", 1.01e8, "D2", "2019-08-01"),
        ("2019-10-20", 1.01e5, "D3", "2019-11-01"),                       # isolated unit slip
        ("2020-01-20", 1.02e8, "D4", "2020-02-01"), ("2020-04-20", 4.08e8, "D5", "2020-05-01"),  # 4:1 split
        ("2020-07-20", 4.10e8, "D6", "2020-08-01")])
    bs = rows("us-gaap:CommonStockSharesOutstanding", [
        ("2017-12-31", 0.95e8, "B0", "2018-02-15"), ("2018-12-31", 0.98e8, "B1", "2019-02-15"),
        ("2019-03-31", 1.00e8, "D1", "2019-05-01")])                       # overlaps dei: agrees
    ads = rows("us-gaap:WeightedAverageNumberOfSharesOutstandingBasic", [
        ("2016-12-31", 5.0e8, "W0", "2017-02-15"), ("2019-03-31", 5.0e8, "D1", "2019-05-01")])  # 5x off: never mixed
    facts = pl.DataFrame(dei + bs + ads, schema=FACT_SCHEMA_FOR_TEST)
    sh = build_shares(facts).sort("end")
    assert sh["end"].min() == date(2017, 12, 31)                        # filled from the balance sheet
    assert 5.0e8 not in sh["shares"].to_list()                          # disagreeing concept left out
    assert 1.01e5 not in sh["shares"].to_list()                         # slip dropped
    assert 4.08e8 in sh["shares"].to_list() and 1.00e8 in sh["shares"].to_list()   # split kept (no 20x rule)


FACT_SCHEMA_FOR_TEST = {"cik": pl.String, "taxonomy": pl.String, "tag": pl.String, "unit": pl.String,
                        "start": pl.Date, "end": pl.Date, "val": pl.Float64, "accn": pl.String, "fy": pl.Int32,
                        "fp": pl.String, "form": pl.String, "filed": pl.Date, "frame": pl.String}


def submission(cik, name, forms, former=(), tickers=()):
    return {"cik": f"{cik:010d}", "name": name, "entityType": "operating", "sic": "3571",
            "sicDescription": "Electronic Computers", "category": "Large accelerated filer", "fiscalYearEnd": "0930",
            "stateOfIncorporation": "CA", "tickers": list(tickers), "exchanges": ["Nasdaq"] * len(tickers),
            "formerNames": [{"name": n, "from": f"{a}T00:00:00.000Z", "to": f"{b}T00:00:00.000Z"} for n, a, b in former],
            "filings": {"recent": {"form": [f for f, _ in forms], "filingDate": [d for _, d in forms]}}}


class FakeSecWithProfiles(FakeSec):
    def __init__(self, docs, subs):
        super().__init__(docs)
        self.subs = {s["cik"]: s for s in subs}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for s in subs:
                zf.writestr(f"CIK{s['cik']}.json", orjson.dumps(s))
                zf.writestr(f"CIK{s['cik']}-submissions-001.json", b"{}")   # old-filings page: skipped
        self.sub_zip = buf.getvalue()

    def download(self, url, dest: Path):
        if "submissions" not in url:
            return super().download(url, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.sub_zip)
        return len(self.sub_zip)

    def get_json(self, url):
        if "/submissions/" in url:
            self.requests.append(url)
            return self.subs.get(url.split("CIK")[1][:10])
        return super().get_json(url)


def test_profiles_bulk_and_refresh(settings):
    subs = [submission(1111, "ACME CORP", [("10-Q", "2023-05-01"), ("10-K", "2024-02-15")],
                       former=[("ACME WIDGETS INC", "2001-01-01", "2015-06-30")], tickers=["ACME"]),
            submission(7777, "JOHN SMITH", [("4", "2023-01-01")])]                     # an individual: left out
    fake = FakeSecWithProfiles([acme()], subs)
    ing = Ingestor(settings, client=fake)
    res = ing.profiles()
    store = EdgarStore(settings=settings)
    p = store.profiles()
    assert res.ok and p["cik"].to_list() == ["0000001111"]
    row = p.row(0, named=True)
    assert row["sic"] == "3571" and row["tickers"] == ["ACME"] and row["last_periodic"] == date(2024, 2, 15)
    names = store.names(1111).sort("valid_from", nulls_last=True)
    assert names["name"].to_list() == ["ACME WIDGETS INC", "ACME CORP"]
    assert names["valid_to"][0] == date(2015, 6, 30) and names["valid_from"][1] == date(2015, 6, 30)
    assert ing.profiles().note.startswith("submissions file unchanged")
    fake.subs["0000001111"]["name"] = "ACME HOLDINGS INC"
    assert ing.refresh_profiles(["0000001111"]) == []
    assert store.profiles(1111)["name"][0] == "ACME HOLDINGS INC"
