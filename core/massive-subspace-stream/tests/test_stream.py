import asyncio
import json
from datetime import UTC, date, datetime

import orjson
import polars as pl
from massive_subspace_stream import StreamService
from massive_subspace_stream._ingest.stream import LiveWriter, normalize
from massive_subspace_stream._layout import Layout
from massive_subspace_stream._store import live_files

from .conftest import FakeWS

T0 = int(datetime(2024, 12, 2, 14, 30, tzinfo=UTC).timestamp() * 1000)


def test_normalize_trade_and_quote():
    trades = normalize("T", [{"ev": "T", "sym": "AAPL", "x": 4, "i": "9", "z": 3, "p": 250.5, "s": 100,
                              "c": [12, 37], "t": T0, "q": 77}])
    assert trades.columns[:3] == ["ticker", "date", "sip_timestamp"]
    assert trades["date"].item() == date(2024, 12, 2)
    assert trades["conditions"].to_list() == [[12, 37]]
    assert trades["trf_timestamp"].item() is None
    quotes = normalize("Q", [{"ev": "Q", "sym": "AAPL", "bp": 1.0, "ap": 1.1, "bs": 2, "as": 3, "t": T0}])
    assert quotes["ask_size"].item() == 3.0


def test_subscriptions(settings):
    svc = StreamService(settings, channels=["AM"], watchlist=["aapl", "MSFT"], watch_channels=["T", "AM"])
    assert svc.subscriptions() == ["AM.*", "T.AAPL", "T.MSFT"]


def test_flush_and_hourly_compaction(settings):
    layout = Layout(settings.data_root)
    writer = LiveWriter(layout)
    for i in range(3):
        writer.add({"ev": "A", "sym": "SPY", "s": T0 + i * 1000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})
        writer.flush(now=datetime(2024, 12, 2, 14, 31 + i, tzinfo=UTC))
    day_dir = layout.live_dir("bars_1s", date(2024, 12, 2))
    assert len(list(day_dir.glob("part-*.parquet"))) == 3
    writer.add({"ev": "status", "status": "connected"})  # ignored
    writer.flush(now=datetime(2024, 12, 2, 15, 1, tzinfo=UTC))  # next hour -> compaction
    assert [f.name for f in day_dir.glob("*.parquet")] == ["hour-2024120214.parquet"]
    assert pl.read_parquet(live_files(day_dir)).height == 3


def test_live_files_tolerates_other_file_names(tmp_path):
    for name in ("hour-2024120214.parquet", "part-2024120214-1-000001.parquet", "notes.parquet"):
        (tmp_path / name).touch()
    assert [f.name for f in live_files(tmp_path)] == ["hour-2024120214.parquet", "notes.parquet"]


def test_live_files_skips_parts_already_merged(tmp_path):
    for name in ["hour-2024120214.parquet", "part-2024120214-1-000001.parquet", "part-2024120215-2-000002.parquet"]:
        (tmp_path / name).touch()
    assert [f.name for f in live_files(tmp_path)] == ["hour-2024120214.parquet", "part-2024120215-2-000002.parquet"]


def test_service_run_end_to_end(settings, monkeypatch):
    import websockets

    svc = StreamService(settings, channels=["AM"], watchlist=[], flush_seconds=0.05)
    msgs = [
        orjson.dumps([{"ev": "status", "status": "connected"}]),
        orjson.dumps([{"ev": "status", "status": "auth_success"}]),
        orjson.dumps([{"ev": "AM", "sym": "SPY", "s": T0, "e": T0 + 60000, "o": 1, "h": 2, "l": 0.5, "c": 1.5,
                       "v": 10, "vw": 1.2}]),
    ]
    ws = FakeWS(svc, msgs)
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: ws)
    asyncio.run(svc.run())
    assert ws.sent[0] == {"action": "auth", "params": "test-key"}
    assert ws.sent[1] == {"action": "subscribe", "params": "AM.*"}
    df = pl.read_parquet(live_files(Layout(settings.data_root).live_dir("bars_1m", date(2024, 12, 2))))
    assert df["close"].to_list() == [1.5] and df["vwap"].to_list() == [1.2]
    status = json.loads(svc.status_file.read_text())
    assert status["messages"] == {"AM": 1} and not svc.pid_file.exists()


def _bar(sym, ts_ms, close):
    return {"ev": "AM", "sym": sym, "s": ts_ms, "e": ts_ms + 60_000, "o": close, "h": close, "l": close,
            "c": close, "v": 100}


def test_follow_yields_only_new_rows(settings, monkeypatch):
    from massive_subspace_stream import Store

    layout = Layout(settings.data_root)
    writer = LiveWriter(layout)
    store = Store(settings=settings)
    old = datetime(2024, 12, 2, 14, 31, tzinfo=UTC)
    writer.add(_bar("SPY", T0, 1.0))
    writer.flush(now=old)  # already there before following starts

    # Each poll: write one new flush (SPY + QQQ), then let follow() read it.
    flushes = iter([datetime(2024, 12, 2, 14, 32, tzinfo=UTC), datetime(2024, 12, 2, 15, 1, tzinfo=UTC)])

    def fake_sleep(_):
        now = next(flushes, None)
        if now is None:
            return
        writer.add(_bar("SPY", T0 + 60_000, 2.0))
        writer.add(_bar("QQQ", T0 + 60_000, 3.0))
        writer.flush(now=now)  # the 15:01 flush also compacts the 14:xx parts into an hour file

    monkeypatch.setattr("massive_subspace_stream._store.time.sleep", fake_sleep)
    real_now = datetime(2024, 12, 2, 16, 0, tzinfo=UTC)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now

    monkeypatch.setattr("massive_subspace_stream._store.datetime", FakeDatetime)
    gen = store.follow("bars_1m", tickers="SPY", poll_seconds=0, since=old, settle_seconds=0)
    first, second = next(gen), next(gen)
    assert first["close"].to_list() == [2.0] and first["ticker"].to_list() == ["SPY"]
    assert second["close"].to_list() == [2.0]  # the 15:01 flush, not re-reading compacted rows
    assert len(list(layout.live_dir("bars_1m", date(2024, 12, 2)).glob("hour-*.parquet"))) == 1


def test_follow_timeout_and_validation(settings):
    import pytest
    from massive_subspace_stream import Store

    store = Store(settings=settings)
    assert list(store.follow("bars_1m", poll_seconds=0.01, timeout=0.05)) == []
    with pytest.raises(ValueError):
        next(store.follow("nope"))
    with pytest.raises(ValueError):
        next(store.follow(since=datetime(2024, 1, 1)))


def test_stream_status_when_not_running(settings):
    from massive_subspace_stream import Store

    assert Store(settings=settings).stream_status() == {"running": False}
