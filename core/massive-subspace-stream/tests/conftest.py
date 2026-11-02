from __future__ import annotations

import asyncio
import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from massive_subspace_stream import load_settings
from massive_subspace_stream._sources.flatfiles import RemoteFile


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Never let tests see the developer's real credentials, config, or data root."""
    for var in ("MASSIVE_API_KEY", "POLYGON_API_KEY", "MASSIVE_S3_ACCESS_KEY_ID", "MASSIVE_S3_SECRET_ACCESS_KEY",
                "MASSIVE_REST_URL", "MASSIVE_WS_URL", "MASSIVE_S3_ENDPOINT", "MASSIVE_S3_BUCKET",
                "SUBSPACE_DATA_ROOT", "COLLECTIVE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path):
    return load_settings(data_root=tmp_path / "data", env_file=None, api_key="test-key",
                         s3_access_key_id="id", s3_secret_access_key="secret")


def ns(dt: datetime) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp() * 1_000_000_000)


def bars_csv(rows: list[tuple]) -> bytes:
    """rows: (ticker, volume, open, close, high, low, window_start_ns, transactions)"""
    lines = ["ticker,volume,open,close,high,low,window_start,transactions"]
    lines += [",".join(str(v) for v in r) for r in rows]
    return gzip.compress(("\n".join(lines) + "\n").encode())


class FakeFlatClient:
    """Serves in-memory gzipped CSVs as if they were Massive flat files."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.downloads: list[str] = []

    def add(self, s3_dir: str, day: date, content: bytes) -> None:
        self.files[f"us_stocks_sip/{s3_dir}/{day:%Y/%m}/{day.isoformat()}.csv.gz"] = content

    def list_files(self, s3_dir: str, start: date, end: date) -> list[RemoteFile]:
        out = []
        for key, content in self.files.items():
            if f"/{s3_dir}/" not in key:
                continue
            day = date.fromisoformat(key.rsplit("/", 1)[-1][:10])
            if start <= day <= end:
                out.append(RemoteFile(day, key, len(content)))
        return sorted(out, key=lambda f: f.day)

    def download(self, key: str, dest: Path) -> int:
        self.downloads.append(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.files[key])
        return len(self.files[key])


def day_rows(day: date, closes: dict[str, float]):
    midnight_et = ns(datetime(day.year, day.month, day.day, 5, 0))  # 00:00 ET in winter
    return [(t, 1000.5, c, c, c + 1, c - 1, midnight_et, 10) for t, c in sorted(closes.items())]


def minute_rows(day: date):
    t0 = ns(datetime(day.year, day.month, day.day, 14, 30))  # 09:30 ET
    return [("AAPL", 100, 10, 10.5, 11, 9.5, t0, 3), ("AAPL", 50, 10.5, 10.2, 10.6, 10.1, t0 + 60_000_000_000, 2),
            ("MSFT", 70, 20, 20.1, 20.2, 19.9, t0, 4)]


def fake_rest(pages_by_endpoint):
    class Fake:
        def __init__(self):
            self.calls = []

        def paginate(self, endpoint, params):
            self.calls.append((endpoint, dict(params)))
            data = pages_by_endpoint.get(endpoint)
            if isinstance(data, Exception):
                raise data
            yield from data or []

    return Fake()


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
