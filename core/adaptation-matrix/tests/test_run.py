"""Schedule, ticker blocks, and a whole run against fake Massive files (offline)."""

from __future__ import annotations

import gzip
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from adaptation_matrix import AdaptationMatrix, BusyError, load_settings, schedule
from adaptation_matrix._run import PartWriter, ticker_blocks
from adaptation_matrix._schedule import event_days
from massive_subspace_stream import Ingestor

ET = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for var in ("ADAPTATION_DATA_ROOT", "COLLECTIVE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def sessions(start: date, end: date, early: set[date] = frozenset()) -> pl.DataFrame:
    rows = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(UTC)
            c = datetime(d.year, d.month, d.day, 13 if d in early else 16, 0, tzinfo=ET).astimezone(UTC)
            rows.append((d, o, c))
        d += timedelta(days=1)
    return pl.DataFrame(rows, schema={"date": pl.Date, "market_open": pl.Datetime("us", "UTC"),
                                      "market_close": pl.Datetime("us", "UTC")}, orient="row")


def test_schedule_sets_and_order():
    end = date(2024, 12, 31)
    early = {date(2024, 7, 10)}
    sched = schedule(sessions(date(2023, 1, 1), end, early), end, recent_months=6, first=date(2023, 1, 1))
    assert sched["day"].n_unique() == sched.height
    sample = sched.filter(pl.col("set") == "sample")
    # One day a month, a full Tue-Thu session on or after the 10th, before the recent window.
    assert sample.height == 18 and date(2023, 1, 10) in sample["day"].to_list()
    assert all(d.weekday() in (1, 2, 3) and d.day >= 10 for d in sample["day"].to_list())
    recent = sched.filter(pl.col("set") == "recent")["day"].to_list()
    assert min(recent) > date(2024, 6, 30) and max(recent) == end
    # Order: the last three months newest first, then the sample, then the rest of the recent window.
    pr = sched["priority"].to_list()
    assert pr == sorted(pr)
    assert sched["day"][0] == end
    first_sample = sched.filter(pl.col("priority") == 2)["day"][0]
    assert first_sample == max(sample["day"].to_list())
    assert early.isdisjoint(sample["day"].to_list())


def test_event_days_include_expiries_and_reconstitution():
    ev = event_days(date(2025, 12, 31))
    assert date(2025, 6, 20) in ev and date(2025, 6, 27) in ev   # 3rd Friday (expiry), 4th (Russell)
    assert date(2020, 3, 16) in ev and date(2022, 12, 16) not in ev   # stress day; too old an expiry


def test_ticker_blocks_split_by_quote_rows():
    counts = pl.DataFrame({"ticker": ["A", "B", "C", "D", "E"], "n": [5, 5, 30, 1, 1]})
    assert ticker_blocks(counts, 10) == [("A", "B"), ("C", "C"), ("D", "E")]


def test_part_writer_aligns_later_pieces(tmp_path):
    w = PartWriter(tmp_path / "x" / "part.parquet")
    w.write(pl.DataFrame({"a": [1], "b": [None]}))
    w.write(pl.DataFrame({"a": [2]}))
    w.write(pl.DataFrame({"b": [1.5], "a": [3]}))
    assert w.close() > 0
    out = pl.read_parquet(tmp_path / "x" / "part.parquet")
    assert out.columns == ["a", "b"] and out["a"].to_list() == [1, 2, 3] and out["b"].to_list() == [None, None, 1.5]


# ---------------------------------------------------------------------------- a whole run

D1, D2, D3 = date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11)


def _ns(d: date, h: int, m: int, s: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=UTC).timestamp() * 1e9)


def trades_csv(d: date) -> bytes:
    header = ("ticker,conditions,correction,exchange,id,participant_timestamp,price,sequence_number,"
              "sip_timestamp,size,tape,trf_id,trf_timestamp")
    rows = []
    for i, (t, p) in enumerate([("AAA", 10.02), ("AAA", 10.00), ("BBB", 20.05), ("ETF1", 50.01)]):
        ts = _ns(d, 15, 0, i)
        rows.append(f"{t},,0,11,{i},{ts},{p},{i},{ts},100,1,,0")
    ts = _ns(d, 21, 0, 1)
    rows.append(f'AAA,"19",0,11,9,{ts},10.05,9,{ts},1000,1,,0')
    return gzip.compress(("\n".join([header, *sorted(rows)]) + "\n").encode())


def quotes_csv(d: date) -> bytes:
    header = ("ticker,ask_exchange,ask_price,ask_size,bid_exchange,bid_price,bid_size,conditions,indicators,"
              "participant_timestamp,sequence_number,sip_timestamp,tape,trf_timestamp")
    rows = []
    for t, b, a in [("AAA", 10.00, 10.02), ("BBB", 20.00, 20.10), ("ETF1", 50.00, 50.02)]:
        ts = _ns(d, 14, 0)
        rows.append(f"{t},2,{a},200,1,{b},100,1,,{ts},1,{ts},1,0")
    return gzip.compress(("\n".join([header, *rows]) + "\n").encode())


class FakeIngestor:
    convert_ticks = staticmethod(Ingestor.convert_ticks)

    def __init__(self, days):
        self.files = {(k, d): (trades_csv(d) if k == "trades" else quotes_csv(d)) for d in days
                      for k in ("trades", "quotes")}
        self.downloads = []

    def tick_files(self, kind, start, end=None):
        end = end or start
        return sorted((d, len(b)) for (k, d), b in self.files.items() if k == kind and start <= d <= end)

    def download_ticks(self, kind, day, dest_dir):
        if (kind, day) not in self.files:
            return None
        self.downloads.append((kind, day))
        p = Path(dest_dir) / f"{kind}-{day}.csv.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.files[(kind, day)])
        return p


class FakeStore:
    def sessions(self, start, end=None):
        return sessions(start, end or start)

    def scan(self, dataset):
        assert dataset == "bars_day"
        rows = [(d, t, c, c, 1e6) for d in (D1, D2, D3) for t, c in (("AAA", 10.05), ("BBB", 20.0), ("ETF1", 50.0))]
        return pl.DataFrame(rows, schema=["date", "ticker", "open", "close", "volume"], orient="row").lazy()

    def universe(self, on, types=None):
        return pl.DataFrame({"ticker": ["AAA", "BBB", "ETF1"], "security_id": ["S-A", "S-B", "S-E"],
                             "type": ["CS", "CS", "ETF"]})


class NoVinculum:
    def universe(self, *a, **k):
        raise FileNotFoundError("no vinculum store")


def make(tmp_path, days=(D1, D2), **settings):
    s = load_settings(data_root=tmp_path / "am", disk_margin_gb=0, keep_top=1, keep_etfs=1, **settings)
    ing = FakeIngestor(days)
    return AdaptationMatrix(settings=s, store=FakeStore(), ingestor=ing, vinculum=NoVinculum()), ing


def test_run_measures_resumes_and_keeps_named_trades(tmp_path):
    m, ing = make(tmp_path)
    res = m.run(days=[D1, D2, D3])
    assert res.done == [D1, D2] and res.missing == [D3] and not res.failed
    daily = m.daily(start=D1, end=D2)
    assert daily.height == 6 and set(daily["security_id"]) == {"S-A", "S-B", "S-E"}
    aaa = m.daily("AAA", D1, D1).row(0, named=True)
    assert aaa["close_vs_mid_bps"] == pytest.approx((10.05 / 10.01 - 1) * 1e4)
    assert aaa["close_auction_volume"] == 1000
    # Kept: the top stock by dollar volume (BBB, vinculum unavailable) and the top ETF.
    assert set(m.trades(start=D1, end=D1)["ticker"]) == {"BBB", "ETF1"}
    assert m.buckets("AAA", D1, D1, minutes=390)["q_ns"].item() == 390 * 60 * 10**9
    # Nothing left in the work folder; a second run downloads nothing.
    assert not any(m.layout.work_dir.iterdir())
    n = len(ing.downloads)
    again = m.run(days=[D1, D2])
    assert again.skipped == [D1, D2] and len(ing.downloads) == n
    assert m.status()["days_done"] == 2


def test_run_stops_at_the_store_limit(tmp_path):
    m, _ = make(tmp_path, max_store_gb=0.0)
    res = m.run(days=[D1, D2])
    assert res.done == [] and "max_store_gb" in res.stopped


def test_one_run_at_a_time(tmp_path):
    m, _ = make(tmp_path)
    m.layout.ensure()
    with m.layout.run_lock("test"), pytest.raises(BusyError):
        m.run(days=[D1])
