"""The run: download each day's trades and quotes, measure them, keep the results, delete the ticks.

One day at a time, with the next day's files downloading while the current one is measured
(downloads are the slow part: 10-30 MB/s, about 14 GB a day in 2026). Every finished day is
recorded in state.json with the measure version, so a run can be stopped and started again;
finished days are skipped. A run stops before the store passes `max_store_gb` or free disk falls
below `disk_margin_gb`.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from ._config import Settings
from ._layout import Layout
from ._measure import measure_block, second_stamped

log = logging.getLogger("adaptation_matrix")

MEASURE_VERSION = 1
PARQUET_PER_GZ = 1.3   # converted tick Parquet relative to the downloaded .csv.gz (measured: 0.93-1.1)


@dataclass
class RunResult:
    done: list[date] = field(default_factory=list)
    skipped: list[date] = field(default_factory=list)
    missing: list[date] = field(default_factory=list)
    failed: dict[date, str] = field(default_factory=dict)
    stopped: str | None = None

    def line(self) -> str:
        s = f"{len(self.done)} day(s) measured, {len(self.skipped)} already done"
        if self.missing:
            s += f", {len(self.missing)} without files"
        if self.failed:
            s += f", {len(self.failed)} failed"
        if self.stopped:
            s += f"; stopped: {self.stopped}"
        return s


class PartWriter:
    """Writes one table of one day in pieces (one per ticker block) to a temporary file, renamed
    into place by `close`. The first non-empty piece fixes the schema."""

    def __init__(self, path: Path):
        self.path = path
        self.tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        self.writer: pq.ParquetWriter | None = None
        self.schema: pa.Schema | None = None
        self.columns: dict[str, pl.DataType] | None = None
        self.rows = 0

    def write(self, df: pl.DataFrame) -> None:
        if df.height == 0:
            return
        if self.columns is None:
            df = df.with_columns([pl.col(c).cast(pl.Float64) for c, t in df.schema.items() if t == pl.Null])
            self.columns = dict(df.schema)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        else:
            df = df.select([pl.col(c).cast(t, strict=False) if c in df.columns else pl.lit(None, dtype=t).alias(c)
                            for c, t in self.columns.items()])
        table = df.to_arrow()
        if self.writer is None:
            self.schema = table.schema
            self.writer = pq.ParquetWriter(self.tmp, self.schema, compression="zstd", compression_level=3)
        self.writer.write_table(table.cast(self.schema), row_group_size=256_000)
        self.rows += df.height

    def close(self) -> int:
        """Rename into place; returns bytes (0 and no file when nothing was written)."""
        if self.writer is None:
            return 0
        self.writer.close()
        self.writer = None
        os.replace(self.tmp, self.path)
        return self.path.stat().st_size

    def abort(self) -> None:
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        self.tmp.unlink(missing_ok=True)


def ticker_blocks(counts: pl.DataFrame, max_rows: int) -> list[tuple[str, str]]:
    """Contiguous (first, last) ticker ranges, in sorted order, each with at most `max_rows`
    quote rows (a ticker with more is a block of its own)."""
    blocks: list[tuple[str, str]] = []
    first = last = None
    rows = 0
    for ticker, n in counts.sort("ticker").select("ticker", "n").iter_rows():
        if first is not None and rows + n > max_rows:
            blocks.append((first, last))
            first, rows = None, 0
        if first is None:
            first = ticker
        last, rows = ticker, rows + n
    if first is not None:
        blocks.append((first, last))
    return blocks


class Runner:
    def __init__(self, settings: Settings, store: Any = None, ingestor: Any = None, vinculum: Any = None):
        self.settings = settings
        self.layout = Layout(settings.data_root)
        self._store, self._ingestor, self._vinculum = store, ingestor, vinculum

    # Dependencies are created lazily so reads and dry runs need no credentials.
    @property
    def store(self):
        if self._store is None:
            from massive_subspace_stream import Store
            self._store = Store()
        return self._store

    @property
    def ingestor(self):
        if self._ingestor is None:
            from massive_subspace_stream import Ingestor, load_settings
            self._ingestor = Ingestor(load_settings())
        return self._ingestor

    @property
    def vinculum(self):
        if self._vinculum is None:
            from vinculum import Vinculum
            self._vinculum = Vinculum(store=self.store)
        return self._vinculum

    # ------------------------------------------------------------------ state
    def done(self, day: date) -> bool:
        rec = self.layout.read_state().get("days", {}).get(day.isoformat())
        return bool(rec and rec.get("status") == "done" and rec.get("version") == MEASURE_VERSION)

    def _record(self, day: date, rec: dict[str, Any]) -> None:
        state = self.layout.read_state()
        state.setdefault("days", {})[day.isoformat()] = rec
        self.layout.write_state(state)

    # ------------------------------------------------------------------ inputs for one day
    def _session(self, day: date) -> tuple[int, int]:
        s = self.store.sessions(day, day)
        if s.height == 0:
            raise ValueError(f"{day} is not a trading day")
        row = s.row(0, named=True)
        return int(row["market_open"].timestamp() * 1e9), int(row["market_close"].timestamp() * 1e9)

    def _reference(self, day: date) -> tuple[pl.DataFrame, pl.DataFrame, set[str]]:
        """(official open/close per ticker, ticker -> security_id/type, tickers whose trades are kept)."""
        bars = (self.store.scan("bars_day").filter(pl.col("date") == day)
                .select("ticker", pl.col("open").alias("official_open"), pl.col("close").alias("official_close"),
                        "volume").collect())
        try:
            uni = self.store.universe(day).select("ticker", "security_id", "type")
        except Exception as exc:  # no security master: tickers only
            log.warning("%s: no security ids (%s)", day, exc)
            uni = pl.DataFrame(schema={"ticker": pl.String, "security_id": pl.String, "type": pl.String})
        keep: set[str] = set()
        top = self.settings.keep_top
        if top > 0:
            try:
                keep |= set(self.vinculum.universe(day, top=top)["ticker"].to_list())
            except Exception as exc:  # noqa: BLE001 - topped up below
                log.warning("%s: vinculum universe unavailable (%s)", day, exc)
            if len(keep) < top:
                # vinculum has market caps only where SEC share counts exist (from about 2009):
                # top up with the day's largest stocks by dollar volume (old reference data often
                # lacks `type`, so untyped tickers count as stocks).
                stocks = (bars.join(uni, on="ticker", how="left")
                          .filter(pl.col("type").is_in(["CS", "ADRC"]) | pl.col("type").is_null())
                          .filter(~pl.col("ticker").is_in(list(keep)))
                          .sort(pl.col("official_close") * pl.col("volume"), descending=True))
                keep |= set(stocks.head(top - len(keep))["ticker"].to_list())
        if self.settings.keep_etfs > 0:
            etfs = bars.join(uni.filter(pl.col("type") == "ETF"), on="ticker")
            keep |= set(etfs.sort(pl.col("official_close") * pl.col("volume"), descending=True)
                        .head(self.settings.keep_etfs)["ticker"].to_list())
        return bars.drop("volume"), uni, keep

    # ------------------------------------------------------------------ measuring
    def measure_day(self, day: date, trades: pl.LazyFrame, quotes: pl.LazyFrame, set_name: str = "manual",
                    extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Measure one day from tick frames in massive-subspace-stream's schema (sorted by ticker)
        and store daily, minute and matched-trade tables. Returns the day's state record."""
        t0 = time.time()
        open_ns, close_ns = self._session(day)
        official, ids, keep = self._reference(day)
        interp = second_stamped(quotes)
        counts = (pl.concat([quotes.group_by("ticker").agg(pl.len().alias("n")).collect(),
                             trades.group_by("ticker").agg((pl.len() // 4).alias("n")).collect()])
                  .group_by("ticker").agg(pl.col("n").sum()))
        blocks = ticker_blocks(counts, self.settings.chunk_quotes)
        writers = {t: PartWriter(self.layout.file(t, day)) for t in ("daily", "minute", "trades")}
        try:
            for lo, hi in blocks:
                rng = (pl.col("ticker") >= lo) & (pl.col("ticker") <= hi)
                b = measure_block(trades.filter(rng), quotes.filter(rng), open_ns, close_ns, interp,
                                  keep={k for k in keep if lo <= k <= hi}, official=official.filter(rng))
                for name, df in (("daily", b.daily), ("minute", b.minute), ("trades", b.trades)):
                    if df.height:
                        df = (df.join(ids.select("ticker", "security_id"), on="ticker", how="left")
                              .with_columns(pl.lit(day).alias("date"))
                              .select("date", "ticker", "security_id", pl.exclude("date", "ticker", "security_id")))
                    writers[name].write(df)
            sizes = {name: w.close() for name, w in writers.items()}
        except BaseException:
            for w in writers.values():
                w.abort()
            raise
        rec = {"status": "done", "set": set_name, "version": MEASURE_VERSION, "interpolated": interp,
               "rows": {name: w.rows for name, w in writers.items()}, "bytes": sizes,
               "kept_tickers": len(keep), "blocks": len(blocks), "measure_s": round(time.time() - t0, 1),
               "finished": datetime.now(UTC).isoformat(timespec="seconds")}
        rec.update(extra or {})
        self._record(day, rec)
        return rec

    def _convert_and_measure(self, day: date, raw: dict[str, Path], set_name: str, extra: dict) -> dict:
        work = self.layout.work_dir / day.isoformat()
        work.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        paths = {}
        try:
            for kind in ("trades", "quotes"):
                paths[kind] = work / f"{kind}.parquet"
                self.ingestor.convert_ticks(kind, raw[kind], paths[kind])
                raw[kind].unlink(missing_ok=True)
            extra = {**extra, "convert_s": round(time.time() - t0, 1)}
            return self.measure_day(day, pl.scan_parquet(paths["trades"]), pl.scan_parquet(paths["quotes"]),
                                    set_name, extra)
        finally:
            for p in (*paths.values(), *raw.values()):
                p.unlink(missing_ok=True)
            try:
                work.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------ the run
    def _room(self, need_bytes: float) -> str | None:
        """Why there is no room for `need_bytes` more on disk (None: there is)."""
        s = self.settings
        if self.layout.store_bytes() >= s.max_store_gb * 1e9:
            return f"the store reached max_store_gb ({s.max_store_gb:.0f} GB)"
        if self.layout.free_bytes() - need_bytes < s.disk_margin_gb * 1e9:
            return f"free disk would fall below disk_margin_gb ({s.disk_margin_gb:.0f} GB)"
        return None

    def run(self, days: Iterable[tuple[date, str]], limit: int | None = None,
            on_day: Callable[[date, dict], None] | None = None) -> RunResult:
        """Measure `days` ((day, set) in order), skipping finished ones; at most `limit` new days."""
        result = RunResult()
        todo = []
        for day, set_name in days:
            (result.skipped.append(day) if self.done(day) else todo.append((day, set_name)))
        if limit is not None:
            todo = todo[:limit]
        if not todo:
            return result
        self.layout.ensure()
        with self.layout.run_lock(f"run of {len(todo)} day(s)"):
            for leftover in self.layout.work_dir.iterdir():   # from a run that was stopped
                shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir() else leftover.unlink()
            self._run(todo, result, on_day)
        return result

    def _run(self, todo: list[tuple[date, str]], result: RunResult, on_day) -> None:
        q: queue.Queue = queue.Queue(maxsize=1)   # one day downloaded ahead
        stop = threading.Event()

        def downloader() -> None:
            for day, set_name in todo:
                if stop.is_set():
                    break
                try:
                    listed = [f for k in ("trades", "quotes") for f in self.ingestor.tick_files(k, day)]
                    why = self._room(sum(size for _, size in listed) * (1 + PARQUET_PER_GZ))
                    if why:
                        q.put(("stop", day, set_name, why))
                        return
                    t0 = time.time()
                    raw: dict[str, Path] = {}
                    dest = self.layout.work_dir / day.isoformat()
                    for kind in ("trades", "quotes"):
                        p = self.ingestor.download_ticks(kind, day, dest)
                        if p is None:
                            break
                        raw[kind] = p
                    dt = time.time() - t0
                    nbytes = sum(p.stat().st_size for p in raw.values())
                    extra = {"download_s": round(dt, 1), "download_gb": round(nbytes / 1e9, 2),
                             "download_mb_s": round(nbytes / 1e6 / dt, 1) if dt > 0 else None}
                    q.put(("ok" if len(raw) == 2 else "missing", day, set_name, (raw, extra)))
                except BaseException as exc:  # noqa: BLE001 - reported per day
                    q.put(("error", day, set_name, f"{type(exc).__name__}: {exc}"))
            q.put(("end", None, None, None))

        th = threading.Thread(target=downloader, name="adaptation-download", daemon=True)
        th.start()
        try:
            while True:
                status, day, set_name, payload = q.get()
                if status == "end":
                    break
                if status == "stop":
                    result.stopped = payload
                    log.warning("stopping before %s: %s", day, payload)
                    break
                if status == "missing":
                    raw, _ = payload
                    for p in raw.values():
                        p.unlink(missing_ok=True)
                    result.missing.append(day)
                    self._record(day, {"status": "missing", "set": set_name})
                    continue
                if status == "error":
                    result.failed[day] = payload
                    log.error("%s: download failed: %s", day, payload)
                    self._record(day, {"status": "failed", "set": set_name, "error": payload})
                    continue
                raw, extra = payload
                try:
                    rec = self._convert_and_measure(day, raw, set_name, extra)
                except Exception as exc:  # keep going; the day is retried on the next run
                    result.failed[day] = f"{type(exc).__name__}: {exc}"
                    log.exception("%s: measuring failed", day)
                    self._record(day, {"status": "failed", "set": set_name, "error": result.failed[day]})
                    continue
                result.done.append(day)
                log.info("%s (%s): %s; download %.0fs at %s MB/s, convert %.0fs, measure %.0fs", day, set_name,
                         ", ".join(f"{k} {v:,}" for k, v in rec["rows"].items()), rec.get("download_s", 0),
                         rec.get("download_mb_s"), rec.get("convert_s", 0), rec["measure_s"])
                if on_day:
                    on_day(day, rec)
        finally:
            # The downloader is a daemon thread: unblock it once and leave it; a partly downloaded
            # day left in _work is cleared at the start of the next run.
            stop.set()
            try:
                q.get_nowait()
            except queue.Empty:
                pass
