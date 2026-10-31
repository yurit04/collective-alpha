import asyncio
import json
from datetime import UTC, date, datetime

import orjson
import polars as pl
from massive_subspace_stream import StreamService
from massive_subspace_stream._ingest.stream import LiveWriter, normalize
from massive_subspace_stream._layout import Layout
from massive_subspace_stream._store import live_files

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


def test_live_files_skips_parts_already_merged(tmp_path):
    for name in ["hour-2024120214.parquet", "part-2024120214-1-000001.parquet", "part-2024120215-2-000002.parquet"]:
        (tmp_path / name).touch()
    assert [f.name for f in live_files(tmp_path)] == ["hour-2024120214.parquet", "part-2024120215-2-000002.parquet"]


class FakeWS:
    def __init__(self, svc, messages):
        self.svc, self.messages, self.sent = svc, list(messages), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        self.svc.stop()
        await asyncio.sleep(3600)


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
