"""Live websocket stream: one connection, buffered into Parquet every few seconds.

Massive allows one stocks websocket connection per key, so this service is the
single owner of the connection. Data lands in live/<kind>/date=YYYY-MM-DD/ as
small part files, merged into one file per hour once the hour is over. Live data
is provisional; the daily sync stores the official files and prunes old live days.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import polars as pl

from .._config import Settings
from .._layout import Layout
from .._storage import write_parquet

log = logging.getLogger("massive_subspace_stream.stream")


class StreamRefused(PermissionError):
    """Massive refused the login (wrong key, or a feed the plan doesn't include).
    Restarting cannot fix it, so the service stops instead of retrying."""

ET_ZONE = "America/New_York"

# Websocket event code -> (storage kind, {output column: (message key, dtype)})
_AGG = {
    "ticker": ("sym", pl.String),
    "timestamp": ("s", "ms"),
    "end_timestamp": ("e", "ms"),
    "open": ("o", pl.Float64),
    "high": ("h", pl.Float64),
    "low": ("l", pl.Float64),
    "close": ("c", pl.Float64),
    "volume": ("v", pl.Float64),
    "vwap": ("vw", pl.Float64),
    "day_open": ("op", pl.Float64),
    "day_volume": ("av", pl.Float64),
    "day_vwap": ("a", pl.Float64),
    "avg_trade_size": ("z", pl.Float64),
    "otc": ("otc", pl.Boolean),
}
CHANNELS: dict[str, tuple[str, dict[str, tuple[str, Any]]]] = {
    "AM": ("bars_1m", _AGG),
    "A": ("bars_1s", _AGG),
    "T": ("trades", {
        "ticker": ("sym", pl.String),
        "sip_timestamp": ("t", "ms"),
        "price": ("p", pl.Float64),
        "size": ("s", pl.Float64),
        "exchange": ("x", pl.Int16),
        "conditions": ("c", pl.List(pl.Int16)),
        "id": ("i", pl.String),
        "sequence_number": ("q", pl.Int64),
        "tape": ("z", pl.Int8),
        "trf_id": ("trfi", pl.Int32),
        "trf_timestamp": ("trft", "ms"),
    }),
    "Q": ("quotes", {
        "ticker": ("sym", pl.String),
        "sip_timestamp": ("t", "ms"),
        "bid_price": ("bp", pl.Float64),
        "bid_size": ("bs", pl.Float64),
        "bid_exchange": ("bx", pl.Int16),
        "ask_price": ("ap", pl.Float64),
        "ask_size": ("as", pl.Float64),
        "ask_exchange": ("ax", pl.Int16),
        "condition": ("c", pl.Int16),
        "indicators": ("i", pl.List(pl.Int16)),
        "sequence_number": ("q", pl.Int64),
        "tape": ("z", pl.Int8),
    }),
    "LULD": ("luld", {
        "ticker": ("T", pl.String),
        "timestamp": ("t", "ms"),
        "high_price": ("h", pl.Float64),
        "low_price": ("l", pl.Float64),
        "indicators": ("i", pl.List(pl.Int16)),
        "sequence_number": ("q", pl.Int64),
        "tape": ("z", pl.Int8),
    }),
}
TIME_COLUMN = {"bars_1m": "timestamp", "bars_1s": "timestamp", "trades": "sip_timestamp",
               "quotes": "sip_timestamp", "luld": "timestamp"}


def normalize(event: str, rows: list[dict[str, Any]], received_at: datetime | None = None) -> pl.DataFrame:
    """Turn raw websocket events of one type into a typed DataFrame with a `date` column."""
    kind, mapping = CHANNELS[event]
    cols = {}
    for out, (key, dtype) in mapping.items():
        values = [r.get(key) for r in rows]
        if dtype == "ms":
            cols[out] = pl.Series(out, values, pl.Int64, strict=False).cast(pl.Datetime("ms")).dt.replace_time_zone(
                "UTC").cast(pl.Datetime("ns", "UTC"))
        else:
            cols[out] = pl.Series(out, values, dtype, strict=False)
    df = pl.DataFrame(cols)
    ts = TIME_COLUMN[kind]
    df = df.with_columns(pl.col(ts).dt.convert_time_zone(ET_ZONE).dt.date().alias("date"))
    if received_at is not None:
        df = df.with_columns(pl.lit(received_at).cast(pl.Datetime("ns", "UTC")).alias("received_at"))
    first = ["ticker", "date", ts]
    return df.select(first + [c for c in df.columns if c not in first])


@dataclass
class StreamStats:
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    connected: bool = False
    connects: int = 0
    last_message_at: str | None = None
    last_flush_at: str | None = None
    messages: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rows_written: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_error: str | None = None
    stopped_reason: str | None = None


class LiveWriter:
    """Buffers events per channel and writes them as Parquet part files."""

    def __init__(self, layout: Layout):
        self.layout = layout
        self.buffers: dict[str, list[dict]] = defaultdict(list)
        self._seq = 0

    def add(self, event: dict[str, Any]) -> bool:
        ev = event.get("ev")
        if ev in CHANNELS:
            self.buffers[ev].append(event)
            return True
        return False

    def flush(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(UTC)
        written: dict[str, int] = {}
        for ev, rows in list(self.buffers.items()):
            if not rows:
                continue
            self.buffers[ev] = []
            df = normalize(ev, rows, received_at=now)
            kind = CHANNELS[ev][0]
            for (day,), part in df.partition_by("date", as_dict=True).items():
                if day is None:
                    continue
                self._seq += 1
                name = f"part-{now:%Y%m%d%H}-{int(now.timestamp() * 1000)}-{self._seq:06d}.parquet"
                write_parquet(part, self.layout.live_dir(kind, day) / name)
            written[kind] = written.get(kind, 0) + df.height
        self.compact(now)
        return written

    def compact(self, now: datetime | None = None) -> int:
        """Merge part files from finished hours into hour-YYYYMMDDHH.parquet."""
        now = now or datetime.now(UTC)
        current = f"{now:%Y%m%d%H}"
        merged = 0
        live_root = self.layout.root / "live"
        if not live_root.exists():
            return 0
        for day_dir in live_root.glob("*/date=*"):
            groups: dict[str, list[Path]] = defaultdict(list)
            for f in day_dir.glob("part-*.parquet"):
                hour = f.stem.split("-")[1]
                if hour < current:
                    groups[hour].append(f)
            for hour, parts in groups.items():
                target = day_dir / f"hour-{hour}.parquet"
                frames = [pl.read_parquet(p) for p in sorted(parts)]
                if target.exists():
                    frames.insert(0, pl.read_parquet(target))
                write_parquet(pl.concat(frames, how="diagonal_relaxed"), target)
                for p in parts:
                    p.unlink(missing_ok=True)
                merged += 1
        return merged


class StreamService:
    def __init__(self, settings: Settings, channels: list[str] | None = None,
                 watchlist: list[str] | None = None, watch_channels: list[str] | None = None,
                 flush_seconds: float | None = None):
        self.settings = settings
        self.layout = Layout(settings.data_root)
        self.layout.ensure()
        self.channels = [c.upper() for c in (channels if channels is not None else settings.stream_channels)]
        self.watchlist = [t.upper() for t in (watchlist if watchlist is not None else settings.stream_watchlist)]
        self.watch_channels = [c.upper() for c in (watch_channels if watch_channels is not None
                                                   else settings.stream_watchlist_channels)]
        unknown = [c for c in self.channels + self.watch_channels if c not in CHANNELS]
        if unknown:
            raise ValueError(f"unknown channel(s) {unknown}; choose from {sorted(CHANNELS)}")
        plan = settings.plan_info
        dropped = sorted({c for c in self.channels + (self.watch_channels if self.watchlist else [])
                          if c not in plan.stream_channels})
        if dropped:
            log.warning("channel(s) %s are not in the Massive %s plan; not subscribing to them",
                        ", ".join(dropped), plan.name)
            self.channels = [c for c in self.channels if c in plan.stream_channels]
            self.watch_channels = [c for c in self.watch_channels if c in plan.stream_channels]
        if not self.subscriptions():
            raise ValueError(f"nothing to subscribe to on the {plan.name} plan "
                             f"(it streams {', '.join(plan.stream_channels)})")
        self.flush_seconds = flush_seconds or settings.stream_flush_seconds
        self.writer = LiveWriter(self.layout)
        self.stats = StreamStats()
        self._stop = asyncio.Event()

    @property
    def pid_file(self) -> Path:
        return self.layout.run_dir / "stream.pid"

    @property
    def status_file(self) -> Path:
        return self.layout.run_dir / "stream.status.json"

    def subscriptions(self) -> list[str]:
        subs = [f"{c}.*" for c in self.channels]
        subs += [f"{c}.{t}" for c in self.watch_channels if c not in self.channels for t in self.watchlist]
        return subs

    def _write_status(self) -> None:
        data = {
            "pid": os.getpid(),
            "subscriptions": self.subscriptions(),
            "data_root": str(self.layout.root),
            **{k: (dict(v) if isinstance(v, defaultdict) else v) for k, v in self.stats.__dict__.items()},
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        tmp = self.status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.status_file)

    def _flush(self) -> None:
        written = self.writer.flush()
        for kind, n in written.items():
            self.stats.rows_written[kind] += n
        self.stats.last_flush_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._write_status()

    async def _flusher(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.flush_seconds)
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(self._flush)
            except Exception as exc:  # never let a bad flush kill the stream
                self.stats.last_error = f"flush: {exc}"
                log.exception("flush failed")

    async def _consume(self) -> None:
        import websockets

        url = f"{self.settings.stream_url.rstrip('/')}/stocks"
        key = self.settings.require_api_key()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=60,
                                              max_queue=None) as ws:
                    await ws.send(orjson.dumps({"action": "auth", "params": key}).decode())
                    await self._await_status(ws, {"auth_success"}, {"auth_failed"})
                    subs = self.subscriptions()
                    await ws.send(orjson.dumps({"action": "subscribe", "params": ",".join(subs)}).decode())
                    self.stats.connected, self.stats.connects = True, self.stats.connects + 1
                    log.info("connected to %s, subscribed to %s", url, ", ".join(subs))
                    self._write_status()
                    backoff = 1.0
                    stop_wait = asyncio.ensure_future(self._stop.wait())
                    try:
                        while not self._stop.is_set():
                            recv = asyncio.ensure_future(ws.recv())
                            done, _ = await asyncio.wait({recv, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
                            if recv not in done:
                                recv.cancel()
                                break
                            self._handle(recv.result())
                    finally:
                        stop_wait.cancel()
            except PermissionError:
                raise
            except Exception as exc:
                self.stats.connected = False
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                if self._stop.is_set():
                    break
                log.warning("stream disconnected (%s); reconnecting in %.0fs", exc, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)
        self.stats.connected = False

    async def _await_status(self, ws, ok: set[str], fail: set[str]) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            for ev in orjson.loads(msg):
                if ev.get("ev") != "status":
                    continue
                status = ev.get("status")
                if status in ok:
                    return
                if status in fail:
                    raise StreamRefused(f"websocket {status}: {ev.get('message')}")
                if status == "max_connections":
                    raise PermissionError(f"websocket {status}: {ev.get('message')}")
        raise TimeoutError("no auth response from websocket")

    def _handle(self, message: str | bytes) -> None:
        events = orjson.loads(message)
        if isinstance(events, dict):
            events = [events]
        self.stats.last_message_at = datetime.now(UTC).isoformat(timespec="seconds")
        for ev in events:
            code = ev.get("ev")
            if code == "status":
                log.info("websocket status: %s %s", ev.get("status"), ev.get("message", ""))
                if ev.get("status") == "max_connections":
                    self.stats.last_error = "max_connections: another client is using this key's stream"
                continue
            if self.writer.add(ev):
                self.stats.messages[code] += 1

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Run until stop() or SIGINT/SIGTERM. Holds an exclusive lock so only one instance runs."""
        lock_path = self.layout.run_dir / "stream.lock"
        lock = open(lock_path, "w")  # noqa: SIM115 - held for the process lifetime
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another stream service is already running (see `subspace stream status`)") from None
        self.pid_file.write_text(str(os.getpid()))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        self._write_status()
        flusher = asyncio.create_task(self._flusher())
        try:
            await self._consume()
        except PermissionError as exc:
            self.stats.stopped_reason = self.stats.last_error = str(exc)
            raise
        finally:
            self._stop.set()
            await flusher
            self._flush()
            self.pid_file.unlink(missing_ok=True)
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
            log.info("stream stopped; rows written: %s", dict(self.stats.rows_written))


def read_status(layout: Layout) -> dict[str, Any]:
    """Status of the stream service: running or not, plus its last reported stats."""
    pid_file = layout.run_dir / "stream.pid"
    status_file = layout.run_dir / "stream.status.json"
    info: dict[str, Any] = {"running": False}
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            info.update(running=True, pid=pid)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    if status_file.exists():
        try:
            info["last_status"] = json.loads(status_file.read_text())
        except json.JSONDecodeError:
            pass
    return info
