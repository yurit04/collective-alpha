"""Batch ingest: flat-file backfills, REST refreshes, daily sync, and verification.

Every unit of work (one trading day of a flat file, one REST dataset) is
recorded in the catalog once it is safely on disk, so any job can be stopped
and re-run; finished work is skipped.
"""

from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from .._catalog import Catalog
from .._config import Settings
from .._datasets.flat import FLAT_DATASETS, FlatSpec, convert_ticks, read_bars_csv
from .._datasets.rest import REST_DATASETS, RestSpec, to_frame
from .._dates import to_date, today_et
from .._layout import Layout
from .._sources.flatfiles import FlatFileClient, RemoteFile
from .._sources.rest import NotEntitledError, RestClient, RestError
from .._storage import dir_size, read_parquet_if_exists, upsert_parquet, write_parquet

log = logging.getLogger("massive_subspace_stream")

REST_FLUSH_ROWS = 250_000   # convert raw API rows to a DataFrame this often
REST_WRITE_ROWS = 3_000_000  # merge into year files this often (each merge rewrites them)
DISK_MARGIN_BYTES = 50 * 1024**3
# Parquet output relative to the downloaded .csv.gz size (measured; used for disk checks).
OUTPUT_RATIO = {"bars": 1.0, "ticks": 1.3}
REFERENCE_FIRST = ("tickers", "ticker_types", "exchanges", "market_holidays", "splits", "dividends", "ipos")


class DiskSpaceError(RuntimeError):
    pass


@dataclass
class JobResult:
    dataset: str
    done: int = 0
    skipped: int = 0
    rows: int = 0
    bytes: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    def line(self) -> str:
        parts = [f"{self.dataset}: {self.done} done", f"{self.skipped} already present"]
        if self.rows:
            parts.append(f"{self.rows:,} rows")
        if self.failed:
            parts.append(f"{len(self.failed)} FAILED")
        if self.note:
            parts.append(self.note)
        return ", ".join(parts)


class Ingestor:
    def __init__(self, settings: Settings, flat_client: Any = None, rest_client: Any = None):
        self.settings = settings
        self.layout = Layout(settings.data_root)
        self.layout.ensure()
        self.catalog = Catalog(self.layout.catalog_path)
        self._flat = flat_client
        self._rest = rest_client

    # Clients are created lazily so commands that don't need them work without credentials.
    @property
    def flat_client(self) -> FlatFileClient:
        if self._flat is None:
            key_id, secret = self.settings.require_s3()
            self._flat = FlatFileClient(self.settings.s3_endpoint, self.settings.s3_bucket, key_id, secret,
                                        max_pool=max(16, self.settings.download_workers * 4))
        return self._flat

    @property
    def rest_client(self) -> RestClient:
        if self._rest is None:
            self._rest = RestClient(self.settings.require_api_key(), self.settings.rest_url)
        return self._rest

    # ------------------------------------------------------------------ flat files
    def flat(
        self,
        dataset: str,
        start: Any = None,
        end: Any = None,
        tickers: Iterable[str] | None = None,
        workers: int | None = None,
        force: bool = False,
    ) -> JobResult:
        """Download and convert flat files for `dataset` between start and end (inclusive)."""
        if dataset not in FLAT_DATASETS:
            raise ValueError(f"unknown flat-file dataset '{dataset}'; choose from {sorted(FLAT_DATASETS)}")
        spec = FLAT_DATASETS[dataset]
        start_d = to_date(start) or to_date(self.settings.backfill_since)
        end_d = to_date(end) or today_et()
        tickers = sorted({t.strip().upper() for t in tickers if t.strip()}) if tickers else None
        if spec.kind == "bars" and tickers:
            log.warning("%s always stores every ticker; ignoring the ticker filter", dataset)
            tickers = None

        remote = self.flat_client.list_files(spec.s3_dir, start_d, end_d)
        result = JobResult(dataset)
        todo = self._todo(spec, remote, tickers, force)
        result.skipped = len(remote) - len(todo)
        if not todo:
            result.note = "nothing to do" if remote else f"no files between {start_d} and {end_d}"
            return result
        self._check_disk(spec, todo)
        log.info("%s: %d day(s) to fetch between %s and %s (%d already present)",
                 dataset, len(todo), todo[0][0].day, todo[-1][0].day, result.skipped)

        if dataset == "bars_day":
            self._ingest_day_bars([f for f, _ in todo], result, workers or self.settings.download_workers)
        elif dataset == "bars_minute":
            self._run_parallel(todo, self._ingest_minute_file, workers or self.settings.download_workers, result)
        else:
            self._run_parallel(todo, self._ingest_tick_file, workers or self.settings.tick_workers, result)
        return result

    def _todo(self, spec: FlatSpec, remote: list[RemoteFile], tickers: list[str] | None,
              force: bool) -> list[tuple[RemoteFile, list[str] | None]]:
        todo = []
        for f in remote:
            existing = self.catalog.get(spec.name, f.day.isoformat())
            if spec.kind == "bars":
                if force or existing is None:
                    todo.append((f, None))
                continue
            have = None if existing is None else (existing["detail"] or {}).get("tickers", "*")
            if existing is not None and not force and (have == "*" or (tickers and set(tickers) <= set(have))):
                continue
            if tickers is None or have == "*" or existing is None or force:
                todo.append((f, tickers))
            else:
                todo.append((f, sorted(set(have) | set(tickers))))
        return todo

    def _check_disk(self, spec: FlatSpec, todo: list[tuple[RemoteFile, Any]]) -> None:
        download = sum(f.size for f, _ in todo)
        workers = self.settings.tick_workers if spec.kind == "ticks" else self.settings.download_workers
        biggest = sorted((f.size for f, _ in todo), reverse=True)[:workers]
        need = download * OUTPUT_RATIO[spec.kind] + sum(biggest)
        if spec.kind == "ticks" and todo[0][1]:  # ticker-filtered: output is a small fraction
            need = sum(biggest) * 1.2
        free = shutil.disk_usage(self.layout.root).free
        if need + DISK_MARGIN_BYTES > free:
            raise DiskSpaceError(
                f"{spec.name}: needs about {need / 1e9:,.0f} GB (+{DISK_MARGIN_BYTES / 1e9:.0f} GB margin) but only "
                f"{free / 1e9:,.0f} GB is free under {self.layout.root}. Free up space or narrow the date range."
            )

    def _run_parallel(self, todo, fn: Callable, workers: int, result: JobResult) -> None:
        total = len(todo)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fn, f, t): f for f, t in todo}
            try:
                for n, fut in enumerate(as_completed(futures), 1):
                    f = futures[fut]
                    try:
                        rows, nbytes = fut.result()
                    except Exception as exc:  # keep going; report at the end
                        result.failed[f.day.isoformat()] = f"{type(exc).__name__}: {exc}"
                        log.error("[%d/%d] %s %s failed: %s", n, total, result.dataset, f.day, exc)
                        continue
                    result.done += 1
                    result.rows += rows
                    result.bytes += nbytes
                    log.info("[%d/%d] %s %s: %s rows", n, total, result.dataset, f.day, f"{rows:,}")
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                raise

    def _download(self, f: RemoteFile):
        raw = self.layout.raw_file(f.key)
        if not (raw.exists() and raw.stat().st_size == f.size):
            self.flat_client.download(f.key, raw)
        return raw

    def _cleanup(self, raw) -> None:
        if not self.settings.keep_raw:
            raw.unlink(missing_ok=True)

    def _ingest_minute_file(self, f: RemoteFile, _tickers) -> tuple[int, int]:
        raw = self._download(f)
        df = read_bars_csv(raw)
        nbytes = write_parquet(df, self.layout.bars_minute_file(f.day))
        self.catalog.mark("bars_minute", f.day.isoformat(), df.height, nbytes, source=f.key)
        self._cleanup(raw)
        return df.height, nbytes

    def _ingest_tick_file(self, f: RemoteFile, tickers: list[str] | None) -> tuple[int, int]:
        kind = "trades" if "trades" in f.key else "quotes"
        raw = self._download(f)
        dest = self.layout.ticks_file(kind, f.day)
        rows = convert_ticks(kind, raw, dest, tickers)
        detail = {"tickers": tickers} if tickers else None
        self.catalog.mark(kind, f.day.isoformat(), rows, dest.stat().st_size, detail=detail, source=f.key)
        self._cleanup(raw)
        return rows, dest.stat().st_size

    def _ingest_day_bars(self, files: list[RemoteFile], result: JobResult, workers: int) -> None:
        by_year: dict[int, list[RemoteFile]] = defaultdict(list)
        for f in files:
            by_year[f.day.year].append(f)

        def fetch(f: RemoteFile) -> pl.DataFrame:
            raw = self._download(f)
            df = read_bars_csv(raw)
            self._cleanup(raw)
            return df

        for year in sorted(by_year):
            frames, days = [], []
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {pool.submit(fetch, f): f for f in by_year[year]}
                for fut in as_completed(futures):
                    f = futures[fut]
                    try:
                        frames.append(fut.result())
                        days.append(f.day)
                    except Exception as exc:
                        result.failed[f.day.isoformat()] = f"{type(exc).__name__}: {exc}"
                        log.error("bars_day %s failed: %s", f.day, exc)
            if not frames:
                continue
            new = pl.concat(frames)
            path = self.layout.bars_day_file(year)
            existing = read_parquet_if_exists(path)
            if existing is not None:
                new = pl.concat([existing.filter(~pl.col("date").is_in(days)), new], how="vertical_relaxed")
            new = new.sort(["date", "ticker"])
            nbytes = write_parquet(new, path)
            counts = dict(new.group_by("date").len().iter_rows())
            self.catalog.mark_many(
                "bars_day",
                [(d.isoformat(), counts.get(d, 0), int(nbytes * counts.get(d, 0) / max(new.height, 1))) for d in days],
                source="day_aggs_v1",
            )
            result.done += len(days)
            result.rows += sum(counts.get(d, 0) for d in days)
            log.info("bars_day %d: %d day(s) written (%s rows in year file)", year, len(days), f"{new.height:,}")

    # ------------------------------------------------------------------ REST datasets
    def rest(self, names: Iterable[str] | None = None, full: bool = False) -> list[JobResult]:
        names = list(names) if names else list(REST_DATASETS)
        unknown = [n for n in names if n not in REST_DATASETS]
        if unknown:
            raise ValueError(f"unknown REST dataset(s) {unknown}; choose from {sorted(REST_DATASETS)}")
        results = []
        for name in names:
            spec = REST_DATASETS[name]
            try:
                results.append(self._refresh_rest(spec, full))
            except NotEntitledError:
                log.warning("%s: not included in your Massive plan (HTTP 403); skipped", name)
                results.append(JobResult(name, note="skipped: not in your plan"))
            except RestError as exc:
                log.error("%s: %s", name, exc)
                results.append(JobResult(name, failed={"all": str(exc)}))
        return results

    def _refresh_rest(self, spec: RestSpec, full: bool) -> JobResult:
        result = JobResult(spec.name)
        base = dict(spec.params)
        if spec.limit:
            base["limit"] = spec.limit
        if spec.mode == "timeseries" and spec.filter_param and not full:
            last = self.catalog.get_state(spec.name, "last_date")
            if last:
                base[spec.filter_param] = (date.fromisoformat(last[:10]) - timedelta(days=spec.overlap_days)).isoformat()
        log.info("%s: fetching %s%s", spec.name, spec.endpoint,
                 f" since {base[spec.filter_param]}" if spec.filter_param in base else "")

        buffer: list[dict] = []
        snapshot_frames: list[pl.DataFrame] = []
        pending: list[pl.DataFrame] = []  # time-series frames not yet merged into year files
        max_date: date | None = None
        touched_years: set[int] = set()

        def write_pending() -> None:
            nonlocal max_date
            frames = [f for f in pending if f.height]
            pending.clear()
            if not frames:
                return
            years, newest = self._write_timeseries(spec, pl.concat(frames, how="diagonal_relaxed"))
            touched_years.update(years)
            if newest and (max_date is None or newest > max_date):
                max_date = newest

        def flush(final: bool = False) -> None:
            if buffer:
                df = to_frame(buffer)
                buffer.clear()
                result.rows += df.height
                (pending if spec.mode == "timeseries" else snapshot_frames).append(df)
            # Merging into year files rewrites them, so do it in large batches.
            if spec.mode == "timeseries" and (final or sum(f.height for f in pending) >= REST_WRITE_ROWS):
                write_pending()

        for extra in spec.param_sets:
            for page in self.rest_client.paginate(spec.endpoint, {**base, **extra}):
                buffer.extend(page)
                if len(buffer) >= REST_FLUSH_ROWS:
                    flush()
        flush(final=True)

        if spec.mode in ("snapshot", "accumulate"):
            frames = [f for f in snapshot_frames if f.height]
            if frames:
                df = pl.concat(frames, how="diagonal_relaxed")
                path = self.layout.rest_file(spec.path)
                if spec.mode == "snapshot":
                    if spec.sort:
                        df = df.sort([c for c in spec.sort if c in df.columns], nulls_last=True)
                    rows, nbytes = df.height, write_parquet(df, path)
                else:
                    rows, nbytes = upsert_parquet(path, df, [k for k in spec.keys if k in df.columns], list(spec.sort))
                self.catalog.mark(spec.name, "all", rows, nbytes, source=spec.endpoint)
            result.done = 1
        else:
            for year in sorted(touched_years):
                path = self.layout.rest_file(spec.path, year)
                self.catalog.mark(spec.name, str(year), pq.read_metadata(path).num_rows, path.stat().st_size,
                                  source=spec.endpoint)
            result.done = len(touched_years)
            if max_date:
                self.catalog.set_state(spec.name, "last_date", max_date.isoformat())
        self.catalog.set_state(spec.name, "last_refresh", datetime.now(UTC).isoformat(timespec="seconds"))
        log.info("%s: %s rows fetched", spec.name, f"{result.rows:,}")
        return result

    def _write_timeseries(self, spec: RestSpec, df: pl.DataFrame) -> tuple[set[int], date | None]:
        col = spec.date_field
        if df.height == 0 or col not in df.columns:
            return set(), None
        if df.schema[col] == pl.Datetime or isinstance(df.schema[col], pl.Datetime):
            day = pl.col(col).dt.date()
        else:
            day = pl.col(col)
        df = df.with_columns(day.dt.year().alias("__year"))
        newest = df.select(day.max()).item()
        years = set()
        for (year,), part in df.partition_by("__year", as_dict=True).items():
            if year is None:
                continue
            path = self.layout.rest_file(spec.path, int(year))
            keys = [k for k in spec.keys if k in part.columns]
            upsert_parquet(path, part.drop("__year"), keys, list(spec.sort))
            years.add(int(year))
        return years, newest

    # ------------------------------------------------------------------ composite jobs
    def initial(self, since: Any = None, minute: bool = True, rest: bool = True,
                workers: int | None = None) -> list[JobResult]:
        """First-time load: reference data, then day bars, minute bars, then the other REST datasets."""
        results = []
        if rest:
            results += self.rest(REFERENCE_FIRST)
        results.append(self.flat("bars_day", start=since, workers=workers))
        if minute:
            results.append(self.flat("bars_minute", start=since, workers=workers))
        if rest:
            results += self.rest([n for n in REST_DATASETS if n not in REFERENCE_FIRST])
        return results

    def sync(self) -> list[JobResult]:
        """Daily catch-up: new flat-file days since the last one stored, plus incremental REST refreshes."""
        results = []
        for dataset in ("bars_day", "bars_minute"):
            keys = self.catalog.partition_keys(dataset)
            if not keys:
                results.append(JobResult(dataset, note="skipped: never backfilled (run `subspace backfill initial`)"))
                continue
            start = date.fromisoformat(keys[-1]) + timedelta(days=1)
            results.append(self.flat(dataset, start=start, end=today_et()))
        results += self.rest()
        removed = prune_live(self.layout, self.catalog, self.settings.keep_live_days)
        if removed:
            log.info("pruned %d old live partition(s)", removed)
        return results

    def verify(self, dataset: str, start: Any = None, end: Any = None, repair: bool = False) -> dict[str, list[str]]:
        """Compare the catalog with the bucket and the files on disk.

        Returns {"missing": [...], "broken": [...]} as ISO dates; with repair=True
        the affected days are downloaded again.
        """
        spec = FLAT_DATASETS[dataset]
        keys = self.catalog.partition_keys(dataset)
        if not keys and start is None:
            return {"missing": [], "broken": []}
        start_d = to_date(start) or date.fromisoformat(keys[0])
        end_d = to_date(end) or (date.fromisoformat(keys[-1]) if keys else today_et())
        remote = {f.day.isoformat() for f in self.flat_client.list_files(spec.s3_dir, start_d, end_d)}
        have = {p["partition"]: p for p in self.catalog.partitions(dataset, start_d.isoformat(), end_d.isoformat())}
        missing = sorted(remote - have.keys())
        broken = []
        day_years: dict[int, set[date]] = {}
        for key, part in have.items():
            day = date.fromisoformat(key)
            if dataset == "bars_day":
                if day.year not in day_years:
                    path = self.layout.bars_day_file(day.year)
                    try:
                        day_years[day.year] = set(pl.read_parquet(path, columns=["date"])["date"].unique())
                    except Exception:
                        day_years[day.year] = set()
                if part["rows"] and day not in day_years[day.year]:
                    broken.append(key)
                continue
            path = (self.layout.bars_minute_file(day) if dataset == "bars_minute"
                    else self.layout.ticks_file(dataset, day))
            try:
                if pq.read_metadata(path).num_rows != part["rows"]:
                    broken.append(key)
            except Exception:
                broken.append(key)
        if repair and (missing or broken):
            for key in broken:
                self.catalog.remove(dataset, key)
            for key in sorted(set(missing) | set(broken)):
                tickers = (have.get(key, {}).get("detail") or {}).get("tickers")
                self.flat(dataset, start=key, end=key, tickers=tickers)
        return {"missing": missing, "broken": sorted(broken)}


def prune_live(layout: Layout, catalog: Catalog, keep_days: int) -> int:
    """Delete live partitions older than `keep_days`.

    Live bar partitions are only deleted once the official minute bars for that
    day are stored. keep_days <= 0 keeps everything.
    """
    if keep_days <= 0:
        return 0
    cutoff = today_et() - timedelta(days=keep_days)
    official = set(catalog.partition_keys("bars_minute"))
    removed = 0
    live_root = layout.root / "live"
    if not live_root.exists():
        return 0
    for kind_dir in live_root.iterdir():
        if not kind_dir.is_dir():
            continue
        for part in kind_dir.glob("date=*"):
            day = part.name.split("=", 1)[1]
            if day >= cutoff.isoformat():
                continue
            if kind_dir.name.startswith("bars") and day not in official:
                continue
            shutil.rmtree(part)
            removed += 1
    return removed


def disk_report(layout: Layout) -> dict[str, int]:
    usage = shutil.disk_usage(layout.root)
    return {"data_bytes": dir_size(layout.root), "free_bytes": usage.free, "total_bytes": usage.total}
