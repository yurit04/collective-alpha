import gzip
from datetime import date

import polars as pl
import pyarrow.parquet as pq
import pytest
from massive_subspace_stream import Ingestor
from massive_subspace_stream._datasets.flat import convert_ticks, read_bars_csv

from .conftest import FakeFlatClient, bars_csv, day_rows, minute_rows

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2025, 1, 2)


def test_read_bars_csv_types_and_eastern_date(tmp_path):
    path = tmp_path / "d.csv.gz"
    path.write_bytes(bars_csv(day_rows(D1, {"AAPL": 10.0, "A": 5.0})))
    df = read_bars_csv(path)
    assert df.columns == ["ticker", "date", "timestamp", "open", "high", "low", "close", "volume", "transactions"]
    assert df["ticker"].to_list() == ["A", "AAPL"]
    assert df["date"].unique().to_list() == [D1]
    assert str(df.schema["timestamp"]) == "Datetime(time_unit='ns', time_zone='UTC')"


def test_convert_trades_filters_and_parses(tmp_path):
    src = tmp_path / "t.csv.gz"
    header = "ticker,conditions,correction,exchange,id,participant_timestamp,price,sequence_number,sip_timestamp,size,tape,trf_id,trf_timestamp"
    rows = [
        'A,"12,37",0,4,1,1793260802914466179,184.85,3444,1793260802928367158,0.04,1,202,1793260802928005450',
        "AAPL,,0,11,2,1793260802914466179,250.0,3445,1793260802928367159,100,3,,0",
        "MSFT,14,0,12,3,0,400.0,3446,1793260802928367160,5,3,,0",
    ]
    src.write_bytes(gzip.compress((header + "\n" + "\n".join(rows) + "\n").encode()))
    dest = tmp_path / "out.parquet"
    assert convert_ticks("trades", src, dest, tickers=["A", "AAPL"], block_size=1 << 16) == 2
    df = pl.read_parquet(dest)
    assert df["ticker"].to_list() == ["A", "AAPL"]
    assert df["conditions"].to_list() == [[12, 37], None]
    assert df["trf_timestamp"][1] is None and df["trf_timestamp"][0] is not None
    assert df.schema["sip_timestamp"] == pl.Datetime("ns", "UTC")

    empty = tmp_path / "empty.parquet"
    assert convert_ticks("trades", src, empty, tickers=["ZZZZ"]) == 0
    assert pq.read_metadata(empty).num_rows == 0


def make_ingestor(settings):
    flat = FakeFlatClient()
    flat.add("day_aggs_v1", D1, bars_csv(day_rows(D1, {"AAPL": 10.0, "MSFT": 20.0})))
    flat.add("day_aggs_v1", D2, bars_csv(day_rows(D2, {"AAPL": 11.0, "MSFT": 21.0})))
    flat.add("day_aggs_v1", D3, bars_csv(day_rows(D3, {"AAPL": 12.0})))
    flat.add("minute_aggs_v1", D1, bars_csv(minute_rows(D1)))
    flat.add("minute_aggs_v1", D2, bars_csv(minute_rows(D2)))
    return Ingestor(settings, flat_client=flat), flat


def test_day_bars_backfill_is_resumable_and_merges_years(settings):
    ing, flat = make_ingestor(settings)
    r = ing.flat("bars_day", "2024-01-01", "2024-01-02")
    assert (r.done, r.rows, r.ok) == (1, 2, True)
    r = ing.flat("bars_day", "2024-01-01", "2025-12-31")
    assert (r.done, r.skipped) == (2, 1)
    year_2024 = pl.read_parquet(ing.layout.bars_day_file(2024))
    assert sorted(year_2024["date"].unique().to_list()) == [D1, D2]
    assert ing.catalog.partition_keys("bars_day") == ["2024-01-02", "2024-01-03", "2025-01-02"]
    assert ing.flat("bars_day", "2024-01-01", "2025-12-31").done == 0
    assert not any(ing.layout.root.joinpath("raw").rglob("*.gz"))  # raw files cleaned up


def test_disk_guard_uses_the_margin_setting(settings):
    from dataclasses import replace

    from massive_subspace_stream._ingest.backfill import DiskSpaceError

    ing, flat = make_ingestor(replace(settings, disk_margin_gb=1e9))   # more than any disk has
    with pytest.raises(DiskSpaceError, match="disk_margin_gb"):
        ing.flat("bars_day", "2024-01-01", "2024-01-02")
    assert flat.downloads == []                                         # refused before downloading


def test_minute_backfill_and_verify(settings):
    ing, flat = make_ingestor(settings)
    r = ing.flat("bars_minute", "2024-01-01", "2024-01-31", workers=2)
    assert (r.done, r.rows) == (2, 6)
    assert ing.verify("bars_minute") == {"missing": [], "broken": [], "unrepairable": []}
    ing.layout.bars_minute_file(D2).unlink()
    assert ing.verify("bars_minute")["broken"] == ["2024-01-03"]
    ing.verify("bars_minute", repair=True)
    assert ing.layout.bars_minute_file(D2).exists()
    assert ing.verify("bars_minute") == {"missing": [], "broken": [], "unrepairable": []}


def test_tick_coverage_union(settings):
    ing, flat = make_ingestor(settings)
    header = "ticker,conditions,correction,exchange,id,participant_timestamp,price,sequence_number,sip_timestamp,size,tape,trf_id,trf_timestamp"
    body = "\n".join(f"{t},,0,4,{i},1,{p},{i},1793260802928367158,1,1,,0"
                     for i, (t, p) in enumerate([("AAPL", 1.0), ("MSFT", 2.0), ("NVDA", 3.0)]))
    flat.add("trades_v1", D1, gzip.compress((header + "\n" + body + "\n").encode()))
    ing.flat("trades", D1, D1, tickers=["AAPL"])
    assert ing.catalog.get("trades", D1.isoformat())["detail"] == {"tickers": ["AAPL"]}
    assert ing.flat("trades", D1, D1, tickers=["aapl"]).done == 0  # already covered
    ing.flat("trades", D1, D1, tickers=["MSFT"])
    assert ing.catalog.get("trades", D1.isoformat())["detail"] == {"tickers": ["AAPL", "MSFT"]}
    assert sorted(pl.read_parquet(ing.layout.ticks_file("trades", D1))["ticker"]) == ["AAPL", "MSFT"]
    ing.flat("trades", D1, D1)  # all tickers
    assert ing.catalog.get("trades", D1.isoformat())["detail"] is None
    assert ing.flat("trades", D1, D1, tickers=["NVDA"]).done == 0


def test_sync_skips_datasets_never_backfilled(settings):
    ing, _ = make_ingestor(settings)
    ing._rest = type("R", (), {"paginate": lambda self, *a, **k: iter([])})()
    results = {r.dataset: r for r in ing.sync()}
    assert "never backfilled" in results["bars_minute"].note


def test_sync_retries_a_day_that_failed_on_an_earlier_night(settings, monkeypatch):
    """A day whose download failed while later days were stored is fetched again by the next
    sync (not skipped forever because sync starts after the newest stored day); days before
    the first stored day are never fetched."""
    import massive_subspace_stream._ingest.backfill as bf

    days = [date(2024, 1, d) for d in (2, 3, 4, 5)]

    class Flaky(FakeFlatClient):
        failed = False

        def download(self, key, dest):
            if "minute_aggs_v1" in key and key.endswith("2024-01-03.csv.gz") and not self.failed:
                self.failed = True
                raise ConnectionError("simulated network error")
            return super().download(key, dest)

    flat = Flaky()
    flat.add("minute_aggs_v1", date(2023, 12, 29), bars_csv(minute_rows(date(2023, 12, 29))))  # before the backfill
    for d in days[:3]:
        flat.add("minute_aggs_v1", d, bars_csv(minute_rows(d)))
    ing = Ingestor(settings, flat_client=flat)
    ing._rest = type("R", (), {"paginate": lambda self, *a, **k: iter([])})()
    first = ing.flat("bars_minute", days[0], days[2])
    assert list(first.failed) == ["2024-01-03"]
    flat.add("minute_aggs_v1", days[3], bars_csv(minute_rows(days[3])))
    monkeypatch.setattr(bf, "today_et", lambda: days[3])
    results = {r.dataset: r for r in ing.sync()}
    assert results["bars_minute"].done == 2 and results["bars_minute"].ok      # the failed day and the new one
    assert ing.catalog.partition_keys("bars_minute") == [d.isoformat() for d in days]
    assert not any("2023-12-29" in k for k in flat.downloads)
