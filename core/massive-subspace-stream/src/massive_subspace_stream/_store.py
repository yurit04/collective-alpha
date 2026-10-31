"""Read-side API: query stored Massive data into polars or pandas DataFrames."""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from ._adjust import adjust_bars
from ._catalog import Catalog
from ._config import Settings, load_settings
from ._datasets.rest import REST_DATASETS
from ._dates import to_date, today_et
from ._layout import Layout
from ._sessions import filter_session, session_table, trading_days

Backend = Literal["polars", "pandas"]
LIVE_KINDS = ("bars_1m", "bars_1s", "trades", "quotes", "luld")


class Store:
    """Query interface over the data root.

    Example:
        store = Store()
        df = store.bars(["AAPL", "MSFT"], start="2024-01-01", freq="1min",
                        adjust="splits", session="regular", backend="pandas")
    """

    def __init__(self, root: Path | str | None = None, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.layout = Layout(root or self.settings.data_root)
        self.root = self.layout.root
        self.catalog = Catalog(self.layout.catalog_path)

    def __repr__(self) -> str:
        return f"Store(root='{self.root}')"

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _tickers(tickers: str | Iterable[str] | None) -> list[str] | None:
        if tickers is None:
            return None
        if isinstance(tickers, str):
            tickers = tickers.split(",")
        return [t.strip().upper() for t in tickers if t.strip()]

    @staticmethod
    def _out(lf: pl.LazyFrame, backend: Backend, lazy: bool):
        if lazy:
            if backend != "polars":
                raise ValueError("lazy=True is only available with backend='polars'")
            return lf
        df = lf.collect()
        if backend == "pandas":
            return df.to_pandas()
        if backend != "polars":
            raise ValueError("backend must be 'polars' or 'pandas'")
        return df

    def _scan(self, files: list[Path], schema_hint: dict | None = None) -> pl.LazyFrame | None:
        files = [f for f in files if f.exists()]
        if not files:
            return None
        return pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore") if len(files) > 1 \
            else pl.scan_parquet(files[0])

    def _range(self, start: Any, end: Any, default_days: int | None = None) -> tuple[date, date]:
        end_d = to_date(end) or today_et()
        start_d = to_date(start) or (end_d - timedelta(days=default_days) if default_days else date(2003, 1, 1))
        if start_d > end_d:
            raise ValueError(f"start {start_d} is after end {end_d}")
        return start_d, end_d

    @staticmethod
    def _filter(lf: pl.LazyFrame, tickers: list[str] | None, start: date, end: date,
                date_col: str = "date") -> pl.LazyFrame:
        lf = lf.filter(pl.col(date_col).is_between(start, end))
        if tickers:
            lf = lf.filter(pl.col("ticker").is_in(tickers))
        return lf

    # ------------------------------------------------------------------ bars
    def bars(
        self,
        tickers: str | Iterable[str] | None = None,
        start: Any = None,
        end: Any = None,
        freq: Literal["1d", "1min"] = "1d",
        adjust: Literal["splits", "all"] | None = None,
        session: Literal["all", "regular", "extended"] = "all",
        columns: list[str] | None = None,
        include_live: bool = False,
        backend: Backend = "polars",
        lazy: bool = False,
    ):
        """OHLCV bars.

        tickers   one ticker, a list, a comma-separated string, or None for all.
        start/end inclusive trading dates ('YYYY-MM-DD', date, datetime, Timestamp).
        freq      '1d' (daily) or '1min'. Other bar sizes belong in feature packages.
        adjust    None (raw, as traded), 'splits', or 'all' (splits + dividends = total return).
        session   minute bars only: 'regular' (exchange hours, early closes respected),
                  'extended' (pre/post market), or 'all'.
        include_live  minute bars only: append live-stream bars for days not yet in the official files.
        """
        tickers = self._tickers(tickers)
        if freq in ("1d", "day", "D"):
            start_d, end_d = self._range(start, end)
            files = [self.layout.bars_day_file(y) for y in range(start_d.year, end_d.year + 1)]
            lf = self._scan(files)
            if session not in (None, "all"):
                raise ValueError("session filtering applies to minute bars only")
        elif freq in ("1min", "1m", "minute"):
            if start is None and not tickers:
                raise ValueError("minute bars for every ticker need a start date (or pass lazy=True and filter yourself)")
            start_d, end_d = self._range(start, end)
            days = self.catalog.partition_keys("bars_minute", start_d.isoformat(), end_d.isoformat())
            lf = self._scan([self.layout.bars_minute_file(date.fromisoformat(d)) for d in days])
            if include_live:
                live = self._live_bars(set(days), start_d, end_d)
                if live is not None:
                    lf = live if lf is None else pl.concat([lf, live], how="diagonal_relaxed")
        else:
            raise ValueError("freq must be '1d' or '1min'")

        if lf is None:
            return self._out(self._empty_bars().lazy(), backend, lazy)
        lf = self._filter(lf, tickers, start_d, end_d)
        if freq not in ("1d", "day", "D"):
            lf = filter_session(lf, session, start_d, end_d)
        if adjust:
            lf = adjust_bars(lf, adjust, self._reference_frame("splits"),
                             self._reference_frame("dividends") if adjust == "all" else None)
        else:
            lf = lf.sort("ticker", "timestamp")
        if columns:
            lf = lf.select(columns)
        return self._out(lf, backend, lazy)

    def _live_bars(self, official_days: set[str], start: date, end: date) -> pl.LazyFrame | None:
        files = []
        for day_dir in sorted(self.layout.live_dir("bars_1m").glob("date=*")):
            day = day_dir.name.split("=", 1)[1]
            if start.isoformat() <= day <= end.isoformat() and day not in official_days:
                files += live_files(day_dir)
        if not files:
            return None
        lf = pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore")
        return lf.select("ticker", "date", "timestamp", "open", "high", "low", "close", "volume",
                         pl.lit(None, pl.Int64).alias("transactions"))

    @staticmethod
    def _empty_bars() -> pl.DataFrame:
        return pl.DataFrame(schema={
            "ticker": pl.String, "date": pl.Date, "timestamp": pl.Datetime("ns", "UTC"),
            "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
            "volume": pl.Float64, "transactions": pl.Int64,
        })

    # ------------------------------------------------------------------ ticks
    def _ticks(self, kind: str, tickers, start, end, backend: Backend, lazy: bool):
        tickers = self._tickers(tickers)
        if not tickers and not lazy:
            raise ValueError(f"{kind} for every ticker are huge; pass tickers, or lazy=True and filter yourself")
        start_d, end_d = self._range(start, end if end is not None else start, default_days=0)
        parts = self.catalog.partitions(kind, start_d.isoformat(), end_d.isoformat())
        if tickers:
            gaps = [p["partition"] for p in parts
                    if p["detail"] and not set(tickers) <= set(p["detail"].get("tickers", []))]
            missing_days = len(trading_days(start_d, end_d)) - len(parts)
            if gaps or missing_days > 0:
                warnings.warn(
                    f"{kind}: stored data does not fully cover {tickers} for {start_d}..{end_d} "
                    f"({missing_days} trading day(s) missing, {len(gaps)} partial). "
                    f"Fetch with: subspace backfill history --dataset {kind} --start {start_d} --end {end_d} "
                    f"--symbols {','.join(tickers)}",
                    stacklevel=3,
                )
        lf = self._scan([self.layout.ticks_file(kind, date.fromisoformat(p["partition"])) for p in parts])
        if lf is None:
            return self._out(pl.DataFrame({"ticker": [], "sip_timestamp": []},
                                          schema={"ticker": pl.String, "sip_timestamp": pl.Datetime("ns", "UTC")}).lazy(),
                             backend, lazy)
        if tickers:
            lf = lf.filter(pl.col("ticker").is_in(tickers))
        return self._out(lf, backend, lazy)

    def trades(self, tickers, start: Any = None, end: Any = None, backend: Backend = "polars", lazy: bool = False):
        """Tick-level trades (fetch them first with `subspace backfill history --dataset trades`)."""
        return self._ticks("trades", tickers, start, end, backend, lazy)

    def quotes(self, tickers, start: Any = None, end: Any = None, backend: Backend = "polars", lazy: bool = False):
        """Tick-level NBBO quotes (fetch them first with `subspace backfill history --dataset quotes`)."""
        return self._ticks("quotes", tickers, start, end, backend, lazy)

    # ------------------------------------------------------------------ REST datasets
    def _reference_frame(self, name: str) -> pl.DataFrame:
        lf = self._dataset_lazy(name)
        return lf.collect() if lf is not None else pl.DataFrame()

    def _dataset_lazy(self, name: str) -> pl.LazyFrame | None:
        spec = REST_DATASETS[name]
        base = self.layout.rest_dir(spec.path)
        files = sorted(base.glob("year=*/part.parquet")) if spec.mode == "timeseries" else [base / "part.parquet"]
        return self._scan(files)

    def dataset(self, name: str, tickers=None, start: Any = None, end: Any = None,
                backend: Backend = "polars", lazy: bool = False):
        """Any REST dataset by name (see `Store.datasets()`), optionally filtered."""
        if name not in REST_DATASETS:
            raise ValueError(f"unknown dataset '{name}'; choose from {sorted(REST_DATASETS)}")
        spec = REST_DATASETS[name]
        lf = self._dataset_lazy(name)
        if lf is None:
            raise FileNotFoundError(f"'{name}' has not been downloaded yet. Run: subspace backfill rest {name}")
        tickers = self._tickers(tickers)
        names = lf.collect_schema().names()
        if tickers:
            if "ticker" in names:
                lf = lf.filter(pl.col("ticker").is_in(tickers))
            elif "tickers" in names:
                lf = lf.filter(pl.col("tickers").list.eval(pl.element().is_in(tickers)).list.any())
        if (start or end) and spec.date_field and spec.date_field in names:
            start_d, end_d = self._range(start, end)
            col = pl.col(spec.date_field)
            if isinstance(lf.collect_schema()[spec.date_field], pl.Datetime):
                col = col.dt.date()
            lf = lf.filter(col.is_between(start_d, end_d))
        return self._out(lf, backend, lazy)

    def tickers(self, active: bool | None = None, types: Iterable[str] | None = None, backend: Backend = "polars"):
        """Reference list of tickers (active and delisted)."""
        lf = self._dataset_lazy("tickers")
        if lf is None:
            raise FileNotFoundError("tickers not downloaded yet. Run: subspace backfill rest tickers")
        if active is not None:
            lf = lf.filter(pl.col("active") == active)
        if types:
            lf = lf.filter(pl.col("type").is_in(list(types)))
        return self._out(lf, backend, False)

    def splits(self, tickers=None, backend: Backend = "polars"):
        return self.dataset("splits", tickers, backend=backend)

    def dividends(self, tickers=None, backend: Backend = "polars"):
        return self.dataset("dividends", tickers, backend=backend)

    def financials(self, statement: Literal["income", "balance", "cash_flow"] = "income", tickers=None,
                   start: Any = None, end: Any = None, backend: Backend = "polars"):
        """Financial statements; start/end filter on filing_date (point-in-time safe)."""
        name = {"income": "income_statements", "balance": "balance_sheets",
                "cash_flow": "cash_flow_statements"}[statement]
        return self.dataset(name, tickers, start, end, backend=backend)

    def short_interest(self, tickers=None, start: Any = None, end: Any = None, backend: Backend = "polars"):
        return self.dataset("short_interest", tickers, start, end, backend=backend)

    def short_volume(self, tickers=None, start: Any = None, end: Any = None, backend: Backend = "polars"):
        return self.dataset("short_volume", tickers, start, end, backend=backend)

    def news(self, tickers=None, start: Any = None, end: Any = None, backend: Backend = "polars"):
        return self.dataset("news", tickers, start, end, backend=backend)

    # ------------------------------------------------------------------ universe and calendar
    def universe(self, on: Any, types: Iterable[str] | None = None, backend: Backend = "polars"):
        """Tickers that traded on a given day (from day bars, so delisted names are included),
        joined with reference info. `types` filters by ticker type, e.g. ['CS', 'ETF', 'ADRC']."""
        day = to_date(on)
        lf = self._scan([self.layout.bars_day_file(day.year)])
        if lf is None:
            raise FileNotFoundError(f"no day bars for {day.year}. Run: subspace backfill history --dataset bars_day")
        traded = lf.filter(pl.col("date") == day).select("ticker", "close", "volume")
        ref = self._dataset_lazy("tickers")
        if ref is not None:
            ref_cols = ["ticker", "name", "type", "primary_exchange", "active", "composite_figi", "cik"]
            names = ref.collect_schema().names()
            ref = (ref.select([c for c in ref_cols if c in names])
                   .sort("active", descending=True).unique("ticker", keep="first"))
            traded = traded.join(ref, on="ticker", how="left")
            if types:
                traded = traded.filter(pl.col("type").is_in(list(types)))
        elif types:
            raise FileNotFoundError("filtering by type needs reference tickers. Run: subspace backfill rest tickers")
        return self._out(traded.sort("ticker"), backend, False)

    def trading_days(self, start: Any, end: Any = None) -> list[date]:
        start_d, end_d = self._range(start, end)
        return trading_days(start_d, end_d)

    def sessions(self, start: Any, end: Any = None, backend: Backend = "polars"):
        """date, market_open, market_close (UTC) per NYSE session, incl. early closes."""
        start_d, end_d = self._range(start, end)
        return self._out(session_table(start_d, end_d).lazy(), backend, False)

    # ------------------------------------------------------------------ live and raw access
    def live(self, kind: str = "bars_1m", tickers=None, start: Any = None, end: Any = None,
             backend: Backend = "polars", lazy: bool = False):
        """Data captured by the live stream (provisional until replaced by official files)."""
        if kind not in LIVE_KINDS:
            raise ValueError(f"kind must be one of {LIVE_KINDS}")
        start_d, end_d = self._range(start, end if end is not None else start, default_days=0)
        files = []
        for day_dir in sorted(self.layout.live_dir(kind).glob("date=*")):
            day = day_dir.name.split("=", 1)[1]
            if start_d.isoformat() <= day <= end_d.isoformat():
                files += live_files(day_dir)
        if not files:
            return self._out(pl.DataFrame(schema={"ticker": pl.String}).lazy(), backend, lazy)
        lf = pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore")
        tickers = self._tickers(tickers)
        if tickers:
            lf = lf.filter(pl.col("ticker").is_in(tickers))
        return self._out(lf, backend, lazy)

    def scan(self, dataset: str) -> pl.LazyFrame:
        """Lazy polars frame over every stored file of a dataset (bars_day, bars_minute, trades,
        quotes, or any REST dataset name), for custom large queries."""
        if dataset == "bars_day":
            files = sorted((self.root / "bars" / "day").glob("year=*/part.parquet"))
        elif dataset == "bars_minute":
            files = sorted((self.root / "bars" / "minute").glob("date=*/part.parquet"))
        elif dataset in ("trades", "quotes"):
            files = sorted((self.root / "ticks" / dataset).glob("date=*/part.parquet"))
        elif dataset in REST_DATASETS:
            lf = self._dataset_lazy(dataset)
            if lf is None:
                raise FileNotFoundError(f"'{dataset}' has no data yet")
            return lf
        else:
            raise ValueError(f"unknown dataset '{dataset}'")
        if not files:
            raise FileNotFoundError(f"'{dataset}' has no data yet")
        return pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore")

    def coverage(self, backend: Backend = "polars"):
        """What is stored: one row per dataset with first/last partition, rows and bytes."""
        df = pl.DataFrame(self.catalog.summary(), schema={
            "dataset": pl.String, "partitions": pl.Int64, "first": pl.String, "last": pl.String,
            "rows": pl.Int64, "bytes": pl.Int64, "updated_at": pl.String})
        return df.to_pandas() if backend == "pandas" else df

    @staticmethod
    def datasets() -> dict[str, str]:
        """Names and descriptions of everything this package can store."""
        from ._datasets.flat import FLAT_DATASETS

        out = {k: v.description for k, v in FLAT_DATASETS.items()}
        out.update({k: v.description for k, v in REST_DATASETS.items()})
        out.update({f"live:{k}": "live stream capture" for k in LIVE_KINDS})
        return out


def live_files(day_dir: Path) -> list[Path]:
    """Parquet files in a live day folder, skipping part files already merged into an hourly file."""
    files = sorted(day_dir.glob("*.parquet"))
    merged_hours = {f.stem.split("-")[1] for f in files if f.name.startswith("hour-")}
    return [f for f in files if f.name.startswith("hour-") or f.stem.split("-")[1] not in merged_hours]
