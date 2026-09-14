"""MassiveProvider: orchestrates entitlement probing and every sync against Massive."""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import polars as pl

from collective_alpha.calendar import previous_trading_day, trading_days
from collective_alpha.config import Settings, get_settings
from collective_alpha.data.base import DataProvider
from collective_alpha.data.massive import auth
from collective_alpha.data.massive.datasets import (
    CORPORATE_ACTION_TABLES,
    FLATFILE_DATASETS,
    FUNDAMENTAL_TABLES,
    REFERENCE_TABLES,
    REST_TABLES,
    RestTable,
)
from collective_alpha.data.massive.flatfiles import FlatFileStore, S3Object
from collective_alpha.data.massive.rest import (
    MassiveHTTPError,
    RestClient,
    flatten_results,
    load_pages,
    save_pages,
)
from collective_alpha.data.massive.transform import convert_flatfile, records_to_frame
from collective_alpha.storage import layout
from collective_alpha.storage.manifest import Manifest
from collective_alpha.storage.parquet import write_parquet_atomic

log = logging.getLogger(__name__)


class MassiveProvider(DataProvider):
    name = "massive"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.s.ensure_dirs()
        self.manifest = Manifest(self.s.manifest_path)
        self._api_key = auth.load_api_key(self.s.api_key_file)
        self.rest = RestClient(self._api_key, self.s.rest_base_url)
        self._flat: FlatFileStore | None = None

    @property
    def flat(self) -> FlatFileStore:
        if self._flat is None:
            creds = auth.load_s3_credentials(self.s.s3_key_file, fallback_secret=self._api_key)
            self._flat = FlatFileStore(self.s, creds)
        return self._flat

    @property
    def horizon(self) -> dt.date:
        today = dt.date.today()
        return today.replace(year=today.year - self.s.history_years)

    # ------------------------------------------------------------------ probe
    def probe(self) -> dict[str, Any]:
        """Hit one cheap call per capability and report reachable / status."""
        out: dict[str, Any] = {"rest": {}, "flatfiles": {}}
        checks = {
            "tickers": ("/v3/reference/tickers", {"market": "stocks", "limit": 1}),
            "ticker_details": ("/v3/reference/tickers/AAPL", {}),
            "splits": ("/v3/reference/splits", {"limit": 1}),
            "dividends": ("/v3/reference/dividends", {"limit": 1}),
            "ipos": ("/vX/reference/ipos", {"limit": 1}),
            "ticker_events": ("/vX/reference/tickers/META/events", {}),
            "news": ("/v2/reference/news", {"limit": 1}),
            "grouped_daily": (
                "/v2/aggs/grouped/locale/us/market/stocks/" + previous_trading_day().isoformat(),
                {"adjusted": "false"},
            ),
            "aggs_5y": (
                "/v2/aggs/ticker/AAPL/range/1/day/"
                + self.horizon.isoformat()
                + "/"
                + (self.horizon + dt.timedelta(days=10)).isoformat(),
                {"limit": 5},
            ),
            "aggs_6y": (
                "/v2/aggs/ticker/AAPL/range/1/day/"
                + (self.horizon - dt.timedelta(days=370)).isoformat()
                + "/"
                + (self.horizon - dt.timedelta(days=360)).isoformat(),
                {"limit": 5},
            ),
            "trades": ("/v3/trades/AAPL", {"limit": 1}),
            "quotes": ("/v3/quotes/AAPL", {"limit": 1}),
            "short_interest": ("/stocks/v1/short-interest", {"limit": 1}),
            "short_volume": ("/stocks/v1/short-volume", {"limit": 1}),
            "financials_income": ("/stocks/financials/v1/income-statements", {"limit": 1}),
            "ratios": ("/stocks/financials/v1/ratios", {"limit": 1}),
            "float": ("/stocks/vX/float", {"limit": 1}),
            "market_holidays": ("/v1/marketstatus/upcoming", {}),
        }
        for name, (path, params) in checks.items():
            try:
                page = self.rest.get_json(path, params)
                res = page.get("results", page.get("resultsCount"))
                n = len(res) if isinstance(res, list) else (res if isinstance(res, int) else (1 if res else 0))
                out["rest"][name] = {"ok": True, "results": n}
            except MassiveHTTPError as e:
                out["rest"][name] = {"ok": False, "status": e.status}
            except Exception as e:  # noqa: BLE001
                out["rest"][name] = {"ok": False, "error": str(e)[:200]}
        try:
            prefixes = self.flat.list_prefixes()
            out["flatfiles"]["top_level"] = prefixes
            stock_sets = self.flat.list_prefixes(layout.FLATFILE_PREFIX + "/")
            out["flatfiles"]["us_stocks_sip"] = stock_sets
            for p in stock_sets:
                ds = p.rstrip("/").split("/")[-1]
                years = self.flat.list_prefixes(p)
                out["flatfiles"][ds] = {"years": [y.rstrip("/").split("/")[-1] for y in years]}
                try:
                    latest = self.flat.list_objects(ds, start=previous_trading_day() - dt.timedelta(days=7))
                    out["flatfiles"][ds]["latest"] = latest[-1].key if latest else None
                    out["flatfiles"][ds]["latest_mb"] = round(latest[-1].size / 1e6, 1) if latest else None
                except Exception as e:  # noqa: BLE001
                    out["flatfiles"][ds]["error"] = str(e)[:200]
        except Exception as e:  # noqa: BLE001
            out["flatfiles"]["error"] = str(e)[:300]
        return out

    # ------------------------------------------------------------------ REST syncs
    def _sync_snapshot(self, key: str, asof: dt.date, extra_params: dict | None = None) -> int:
        spec = REST_TABLES[key]
        raw = layout.raw_rest_path(self.s, key, f"asof={asof:%Y-%m-%d}")
        mkey = f"rest/{key}/asof={asof:%Y-%m-%d}"
        if raw.exists() and self.manifest.is_downloaded(mkey):
            pages = load_pages(raw)
        else:
            pages = self.rest.fetch_all_pages(spec.path, {**spec.params, **(extra_params or {})})
            n = save_pages(pages, raw)
            self.manifest.mark_downloaded(mkey, "rest", spec.table, asof.isoformat(), raw.stat().st_size, None)
            log.info("%s: %d rows", key, n)
        return self._write_snapshot(key, spec, pages, asof, mkey)

    def _write_snapshot(self, key: str, spec: RestTable, pages: list[dict], asof: dt.date, mkey: str) -> int:
        recs = flatten_results(pages)
        df = records_to_frame(recs, asof=asof)
        dest = layout.curated_snapshot_path(self.s, spec.table, asof)
        if spec.table == "tickers":
            # active + inactive land in the same table; merge with anything already written today
            if dest.exists():
                prev = pl.read_parquet(dest)
                prev = prev.filter(pl.col("active") != df["active"][0]) if df.height else prev
                df = pl.concat([prev, df], how="diagonal_relaxed") if df.height else prev
            df = df.sort(["ticker", "active"])
        n = write_parquet_atomic(df, dest) if df.height else 0
        self.manifest.mark_converted(mkey, dest, n)
        return len(recs)

    def sync_reference(self, asof: dt.date | None = None, details: bool = False) -> dict[str, int]:
        asof = asof or dt.date.today()
        out = {}
        for key in REFERENCE_TABLES:
            try:
                out[key] = self._sync_snapshot(key, asof)
            except MassiveHTTPError as e:
                log.warning("%s unavailable (%s)", key, e.status)
                out[key] = -e.status
        if details:
            out.update(self.sync_ticker_details(asof))
        return out

    def sync_corporate_actions(self, asof: dt.date | None = None) -> dict[str, int]:
        asof = asof or dt.date.today()
        out = {}
        for key in CORPORATE_ACTION_TABLES:
            try:
                out[key] = self._sync_snapshot(key, asof)
            except MassiveHTTPError as e:
                log.warning("%s unavailable (%s)", key, e.status)
                out[key] = -e.status
        return out

    def sync_fundamentals(self, asof: dt.date | None = None) -> dict[str, int]:
        asof = asof or dt.date.today()
        out = {}
        for key in FUNDAMENTAL_TABLES:
            try:
                out[key] = self._sync_snapshot(key, asof)
            except MassiveHTTPError as e:
                log.warning("%s unavailable (%s)", key, e.status)
                out[key] = -e.status
        return out

    def active_tickers(self, asof: dt.date | None = None, include_inactive: bool = False) -> list[str]:
        d = layout.curated_table_dir(self.s, "tickers")
        parts = sorted(d.glob("asof=*"))
        if not parts:
            raise RuntimeError("run `ca sync reference` first")
        df = pl.read_parquet(parts[-1] / "data.parquet")
        if not include_inactive:
            df = df.filter(pl.col("active") == True)  # noqa: E712
        return df["ticker"].unique().sort().to_list()

    def sync_ticker_details(self, asof: dt.date | None = None, tickers: list[str] | None = None) -> dict[str, int]:
        """Fan out /tickers/{t} and /tickers/{t}/events over the universe. Idempotent per ticker."""
        asof = asof or dt.date.today()
        tickers = tickers or self.active_tickers(asof)
        results: dict[str, list[dict]] = {"ticker_details": [], "ticker_events": []}
        raw_dir = self.s.raw_dir / "rest" / "per_ticker" / f"asof={asof:%Y-%m-%d}"
        raw_dir.mkdir(parents=True, exist_ok=True)

        def one(t: str) -> tuple[str, dict | None, dict | None]:
            p = raw_dir / f"{t}.json.gz"
            if p.exists():
                pages = load_pages(p)
                return t, pages[0].get("results"), pages[1].get("results") if len(pages) > 1 else None
            det = ev = None
            try:
                det = self.rest.get_json(f"/v3/reference/tickers/{t}")
            except MassiveHTTPError as e:
                if e.status not in (404,):
                    raise
                det = {"results": None}
            try:
                ev = self.rest.get_json(f"/vX/reference/tickers/{t}/events")
            except MassiveHTTPError as e:
                if e.status not in (404, 403):
                    raise
                ev = {"results": None}
            save_pages([det, ev], p)
            return t, det.get("results"), ev.get("results")

        done = 0
        with ThreadPoolExecutor(max_workers=self.s.max_workers) as ex:
            futs = [ex.submit(one, t) for t in tickers]
            for f in as_completed(futs):
                try:
                    t, det, ev = f.result()
                except Exception as e:  # noqa: BLE001
                    log.error("ticker detail failed: %s", e)
                    continue
                if det:
                    results["ticker_details"].append(det)
                if ev:
                    events = ev.get("events") or []
                    for e_ in events:
                        results["ticker_events"].append(
                            {"ticker": t, "name": ev.get("name"), "cik": ev.get("cik"), **e_}
                        )
                done += 1
                if done % 500 == 0:
                    log.info("ticker details: %d/%d", done, len(tickers))
        out = {}
        for table, recs in results.items():
            df = records_to_frame(recs, asof=asof)
            dest = layout.curated_snapshot_path(self.s, table, asof)
            out[table] = write_parquet_atomic(df, dest) if df.height else 0
            self.manifest.mark_downloaded(
                f"rest/{table}/asof={asof:%Y-%m-%d}", "rest", table, asof.isoformat(), 0, None
            )
            self.manifest.mark_converted(f"rest/{table}/asof={asof:%Y-%m-%d}", dest, out[table])
        return out

    def sync_monthly(
        self, key: str, start: dt.date | None = None, end: dt.date | None = None, refresh_last: bool = True
    ) -> dict[str, int]:
        """Time-partitioned REST tables (news, short interest, short volume), one file per month."""
        spec = REST_TABLES[key]
        assert spec.kind == "monthly" and spec.date_field
        start = start or self.horizon
        end = end or dt.date.today()
        months: list[dt.date] = []
        m = start.replace(day=1)
        while m <= end:
            months.append(m)
            m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        out = {}
        for m in months:
            nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            part = f"{m:%Y-%m}"
            mkey = f"rest/{key}/{part}"
            raw = layout.raw_rest_path(self.s, key, part)
            is_current = nxt > end
            if raw.exists() and self.manifest.is_converted(mkey) and not (is_current and refresh_last):
                continue
            params = {**spec.params, f"{spec.date_field}.gte": m.isoformat(), f"{spec.date_field}.lt": nxt.isoformat()}
            try:
                pages = self.rest.fetch_all_pages(spec.path, params)
            except MassiveHTTPError as e:
                log.warning("%s %s unavailable (%s)", key, part, e.status)
                self.manifest.mark_error(mkey, "rest", spec.table, part, f"HTTP {e.status}")
                if e.status in (401, 403):
                    return out
                continue
            save_pages(pages, raw)
            self.manifest.mark_downloaded(mkey, "rest", spec.table, part, raw.stat().st_size, None)
            df = records_to_frame(flatten_results(pages))
            dest = layout.curated_month_path(self.s, spec.table, m)
            n = write_parquet_atomic(df, dest) if df.height else 0
            self.manifest.mark_converted(mkey, dest, n)
            out[part] = n
            log.info("%s %s: %d rows", key, part, n)
        return out

    # ------------------------------------------------------------------ flat files
    def sync_bars(
        self, dataset: str, start: dt.date | None = None, end: dt.date | None = None, convert: bool | None = None
    ) -> dict[str, int]:
        spec = FLATFILE_DATASETS[dataset]
        start = start or self.horizon
        end = end or previous_trading_day()
        convert = spec.convert_by_default if convert is None else convert
        objs = self.flat.list_objects(dataset, start, end)
        log.info("%s: %d files listed on S3 for %s..%s", dataset, len(objs), start, end)

        def hook(obj: S3Object, path: Path) -> None:
            if convert:
                self._convert_one(dataset, obj.key, path, obj.date)

        downloaded, skipped = self.flat.sync(objs, self.manifest, dataset, on_downloaded=hook)
        converted = self.convert_pending(dataset) if convert else 0
        return {"listed": len(objs), "downloaded": downloaded, "skipped": skipped, "converted": converted}

    def _convert_one(self, dataset: str, key: str, path: Path, day: dt.date) -> int:
        dest = layout.curated_bars_path(self.s, dataset, day)
        n = convert_flatfile(path, dataset, day, dest)
        self.manifest.mark_converted(key, dest, n)
        return n

    def convert_pending(self, dataset: str, workers: int | None = None) -> int:
        keys = self.manifest.pending_conversion(dataset)
        if not keys:
            return 0
        log.info("%s: converting %d pending files", dataset, len(keys))
        workers = workers or max(2, self.s.max_workers // 2)
        n = 0

        def one(key: str) -> int:
            day = layout.date_from_flatfile_key(key)
            return self._convert_one(dataset, key, layout.raw_flatfile_path(self.s, key), day)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, k): k for k in keys}
            for f in as_completed(futs):
                k = futs[f]
                try:
                    f.result()
                    n += 1
                except Exception as e:  # noqa: BLE001
                    log.error("convert failed %s: %s", k, e)
                    self.manifest.mark_error(
                        k, "flatfile", dataset, layout.date_from_flatfile_key(k).isoformat(), f"convert: {e}"
                    )
        return n

    def missing_days(self, dataset: str, start: dt.date | None = None, end: dt.date | None = None) -> list[dt.date]:
        """Trading days with no curated file — for coverage reports."""
        start = start or self.horizon
        end = end or previous_trading_day()
        have = {layout.date_from_flatfile_key(k) for k in self._converted_keys(dataset)}
        return [d for d in trading_days(start, end) if d not in have]

    def _converted_keys(self, dataset: str) -> list[str]:
        rows = self.manifest._conn.execute(
            "SELECT key FROM files WHERE dataset=? AND converted_at IS NOT NULL", (dataset,)
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ composite
    def backfill(self, details: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {}
        out["reference"] = self.sync_reference(details=False)
        out["corporate_actions"] = self.sync_corporate_actions()
        for ds in self.s.flatfile_datasets:
            out[ds] = self.sync_bars(ds)
        for key in ("news", "short_interest", "short_volume"):
            out[key] = self.sync_monthly(key)
        out["fundamentals"] = self.sync_fundamentals()
        if details:
            out["ticker_details"] = self.sync_ticker_details()
        return out

    def update(self, days_back: int = 5, details: bool = False) -> dict[str, Any]:
        """Incremental daily refresh: last few sessions of bars + fresh snapshots."""
        end = previous_trading_day()
        start = end - dt.timedelta(days=days_back)
        out: dict[str, Any] = {}
        for ds in self.s.flatfile_datasets:
            out[ds] = self.sync_bars(ds, start, end)
        out["reference"] = self.sync_reference(details=details)
        out["corporate_actions"] = self.sync_corporate_actions()
        for key in ("news", "short_interest", "short_volume"):
            out[key] = self.sync_monthly(key, start=start.replace(day=1))
        return out

    def close(self) -> None:
        self.rest.close()
        self.manifest.close()
