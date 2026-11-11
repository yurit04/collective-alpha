"""Read-side API: query stored Massive data into polars or pandas DataFrames."""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from ._adjust import (
    adjust_bars,
    adjust_with_tables,
    dividend_factor_table_from_steps,
    split_factor_table,
)
from ._catalog import Catalog
from ._config import Settings, load_settings
from ._datasets.rest import REST_DATASETS
from ._dates import ET, to_date, today_et
from ._identity.build import map_to_security
from ._ingest.stream import TIME_COLUMN, read_status
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
        self.catalog = Catalog(self.layout.catalog_path, create=False)   # reading creates nothing
        self._identity_cache: dict[str, tuple[float, pl.DataFrame]] = {}

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

    # ------------------------------------------------------------------ security master
    def _identity(self, name: str) -> pl.DataFrame | None:
        """A security-master table (cached until its file changes), or None if not built."""
        path = self.layout.identity_file(name)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None
        hit = self._identity_cache.get(name)
        if hit is None or hit[0] != mtime:
            hit = (mtime, pl.read_parquet(path))
            self._identity_cache[name] = hit
        return hit[1]

    def _identity_or_empty(self, name: str) -> pl.DataFrame:
        df = self._identity(name)
        return df if df is not None else pl.DataFrame()

    def _require_identity(self, name: str) -> pl.DataFrame:
        df = self._identity(name)
        if df is None:
            raise FileNotFoundError("the security master is not built yet. Run: subspace identity build")
        return df

    @staticmethod
    def _resolve_ids(names: list[str], on: date, master: pl.DataFrame) -> list[str]:
        """Security ids for a mix of security ids and tickers (tickers as of `on`)."""
        known = set(master["security_id"].to_list())
        out = []
        for n in names:
            if n in known:
                out.append(n)
                continue
            rows = master.filter(pl.col("ticker") == n).sort("valid_from")
            if not rows.height:
                raise ValueError(f"unknown ticker or security id: {n}")
            hit = rows.filter(pl.col("valid_from") <= on).tail(1)
            out.append((hit if hit.height else rows.head(1))["security_id"][0])
        return list(dict.fromkeys(out))

    def resolve(self, tickers, on: Any = None, backend: Backend = "polars"):
        """Which security each ticker meant on a day (default: the latest stored day).

        Returns ticker, date, security_id, name, type (null security_id: not trading then).
        """
        master = self._require_identity("security_master")
        tickers = self._tickers(tickers) or []
        day = to_date(on) or self._require_identity("securities")["last_trade"].max()
        df = map_to_security(pl.DataFrame({"ticker": tickers, "date": [day] * len(tickers)},
                                          schema={"ticker": pl.String, "date": pl.Date}), master)
        secs = self._require_identity("securities").select("security_id", "name", "type")
        return self._out(df.join(secs, on="security_id", how="left").lazy(), backend, False)

    def securities(self, tickers=None, active: bool | None = None, backend: Backend = "polars"):
        """One row per security: latest attributes, every ticker it used, first/last trade."""
        df = self._require_identity("securities")
        tickers = self._tickers(tickers)
        if tickers:
            df = df.filter(pl.col("tickers").list.eval(pl.element().is_in(tickers)).list.any())
        if active is not None:
            df = df.filter(pl.col("active") == active)
        return self._out(df.lazy(), backend, False)

    def security_master(self, tickers=None, backend: Backend = "polars"):
        """One row per ticker and validity window: which security a ticker meant when."""
        df = self._require_identity("security_master")
        tickers = self._tickers(tickers)
        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))
        return self._out(df.lazy(), backend, False)

    def security_attributes(self, security_ids=None, backend: Backend = "polars"):
        """Point-in-time attributes per security at each month end (`as_of`): ticker, name, type,
        primary_exchange, cik, FIGIs, as Massive's reference data showed them that day."""
        df = self._require_identity("security_attributes")
        ids = self._tickers(security_ids)
        if ids:
            df = df.filter(pl.col("security_id").is_in(ids))
        return self._out(df.lazy(), backend, False)

    def security_actions(self, kind: Literal["splits", "dividends"] = "splits", security_ids=None,
                         backend: Backend = "polars"):
        """Splits or dividends mapped to securities (what `adjust=` uses): security_id, ticker,
        execution_date / ex_dividend_date, split_from, split_to / cash_amount, factor, ..."""
        if kind not in ("splits", "dividends"):
            raise ValueError("kind must be 'splits' or 'dividends'")
        df = self._require_identity(kind)
        ids = self._tickers(security_ids)
        if ids:
            df = df.filter(pl.col("security_id").is_in(ids))
        return self._out(df.lazy(), backend, False)

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
        by: Literal["ticker", "security"] = "ticker",
        backend: Backend = "polars",
        lazy: bool = False,
    ):
        """OHLCV bars.

        tickers   one ticker, a list, a comma-separated string, or None for all.
        start/end inclusive trading dates ('YYYY-MM-DD', date, datetime, Timestamp).
        freq      '1d' (daily) or '1min'. Other bar sizes belong in feature packages.
        adjust    None (raw, as traded), 'splits', or 'all' (splits + dividends = total return).
                  With a security master, factors follow the security, not the symbol.
        session   minute bars only: 'regular' (exchange hours, early closes respected),
                  'extended' (pre/post market), or 'all'.
        include_live  minute bars only: append live-stream bars for days not yet in the official files.
        by        'ticker': rows traded under these symbols (a reused symbol returns several
                  securities; tell them apart with `security_id`).
                  'security': each ticker (as of `end`) or security id names a security, and its
                  whole history is returned across renames, e.g. 'META' includes the FB years.

        With a security master (`subspace identity build`), every row has a `security_id`.
        """
        tickers = self._tickers(tickers)
        if by not in ("ticker", "security"):
            raise ValueError("by must be 'ticker' or 'security'")
        master = self._identity("security_master")
        wanted: list[str] | None = None
        if by == "security":
            if master is None:
                raise FileNotFoundError("by='security' needs the security master. Run: subspace identity build")
            if not tickers:
                raise ValueError("by='security' needs tickers or security ids")
            wanted = self._resolve_ids(tickers, to_date(end) or today_et(), master)
            tickers = master.filter(pl.col("security_id").is_in(wanted))["ticker"].unique().sort().to_list()
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
        if master is not None:
            lf = map_to_security(lf, master)
            if wanted is not None:
                lf = lf.filter(pl.col("security_id").is_in(wanted))
        if adjust and master is not None:
            if self._identity("splits") is None or (adjust == "all" and self._identity("dividends") is None):
                warnings.warn("the security master has no mapped splits/dividends yet, so adjust="
                              f"{adjust!r} leaves prices unadjusted. Run: subspace identity build", stacklevel=2)
            div_table = dividend_factor_table_from_steps(self._identity_or_empty("dividends")) \
                if adjust == "all" else None
            split_table = split_factor_table(self._identity_or_empty("splits"), key="security_id")
            lf = adjust_with_tables(lf, adjust, split_table, div_table, key="security_id")
        elif adjust:
            lf = adjust_bars(lf, adjust, self._reference_frame("splits"),
                             self._reference_frame("dividends") if adjust == "all" else None)
        lf = lf.sort("security_id", "timestamp") if by == "security" else lf.sort("ticker", "timestamp")
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
        files = sorted(base.glob("year=*/part.parquet")) if spec.mode in ("timeseries", "details") else [base / "part.parquet"]
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
        """Financial statements; start/end filter on filing_date.

        Massive serves one row per fiscal period, dated by the *latest* filing that reported it
        (often the next year's report, which repeats the period for comparison). So filtering on
        filing_date never looks ahead, but older periods appear about a year late. The store keeps
        every version it downloads (filing_date is part of the key), so exact point-in-time
        history builds up from the first download onward. See the subspace-probe notebook 04."""
        name = {"income": "income_statements", "balance": "balance_sheets",
                "cash_flow": "cash_flow_statements"}[statement]
        return self.dataset(name, tickers, start, end, backend=backend)

    def short_interest(self, tickers=None, start: Any = None, end: Any = None, backend: Backend = "polars"):
        return self.dataset("short_interest", tickers, start, end, backend=backend)

    def ticker_details(self, tickers=None, start: Any = None, end: Any = None, backend: Backend = "polars"):
        """Daily snapshots (`as_of`) of Massive's ticker details for active stocks and ADRs:
        market cap, share-class and weighted shares outstanding (ADS counts for ADRs), SIC.
        History starts at the first `ticker_details` refresh; Massive keeps no past counts."""
        return self.dataset("ticker_details", tickers, start, end, backend=backend)

    def short_volume(self, tickers=None, start: Any = None, end: Any = None, backend: Backend = "polars"):
        return self.dataset("short_volume", tickers, start, end, backend=backend)

    def news(self, tickers=None, start: Any = None, end: Any = None, backend: Backend = "polars"):
        return self.dataset("news", tickers, start, end, backend=backend)

    # ------------------------------------------------------------------ universe and calendar
    def universe(self, on: Any, types: Iterable[str] | None = None, backend: Backend = "polars"):
        """Tickers that traded on a given day (from day bars, so delisted names are included),
        joined with reference info. `types` filters by ticker type, e.g. ['CS', 'ETF', 'ADRC'].

        With a security master the info is point in time: the security behind each ticker that
        day and its attributes from the nearest month-end snapshot (the security's latest known
        value where the snapshot has none; old snapshots rarely carry `type`).
        """
        day = to_date(on)
        lf = self._scan([self.layout.bars_day_file(day.year)])
        if lf is None:
            raise FileNotFoundError(f"no day bars for {day.year}. Run: subspace backfill history --dataset bars_day")
        traded = lf.filter(pl.col("date") == day).select("ticker", "close", "volume")
        master = self._identity("security_master")
        if master is not None:
            return self._out(self._universe_pit(traded.collect(), day, master, types).lazy(), backend, False)
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

    def _universe_pit(self, traded: pl.DataFrame, day: date, master: pl.DataFrame,
                      types: Iterable[str] | None) -> pl.DataFrame:
        cols = ["name", "type", "primary_exchange", "cik", "composite_figi"]
        mapped = map_to_security(traded.with_columns(pl.lit(day).alias("date")), master).drop("date")
        secs = self._require_identity("securities").select(
            "security_id", "active", *[pl.col(c).alias(f"__latest_{c}") for c in cols])
        out = mapped.join(secs, on="security_id", how="left")
        attrs = self._identity("security_attributes")
        if attrs is not None and attrs.height:
            before = (attrs.filter(pl.col("as_of") <= day).sort("as_of").group_by("security_id")
                      .agg(*[pl.col(c).last().alias(f"__before_{c}") for c in cols]))
            after = (attrs.filter(pl.col("as_of") > day).sort("as_of").group_by("security_id")
                     .agg(*[pl.col(c).first().alias(f"__after_{c}") for c in cols]))
            out = out.join(before, on="security_id", how="left").join(after, on="security_id", how="left")
            out = out.with_columns(*[pl.coalesce(f"__before_{c}", f"__after_{c}", f"__latest_{c}").alias(c)
                                     for c in cols])
        else:
            out = out.with_columns(*[pl.col(f"__latest_{c}").alias(c) for c in cols])
        out = out.select("ticker", "close", "volume", "security_id", "name", "type", "primary_exchange",
                         "active", "composite_figi", "cik")
        if types:
            out = out.filter(pl.col("type").is_in(list(types)))
        return out.sort("ticker")

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

    def stream_status(self) -> dict[str, Any]:
        """Whether the live stream service is running, plus its last stats
        (subscriptions, connected, messages, rows_written, last_error)."""
        return read_status(self.layout)

    def follow(self, kind: str = "bars_1m", tickers=None, poll_seconds: float = 5.0,
               since: datetime | None = None, timeout: float | None = None,
               settle_seconds: float = 2.0, backend: Backend = "polars") -> Iterator:
        """Yield new live rows as the stream service writes them (one DataFrame per batch).

        Reads the files the stream service writes (it holds the only websocket connection),
        so new data shows up once per flush (`stream_flush_seconds`, default 30 s).

        since          only rows received after this UTC time (default: now, i.e. new rows only).
                       For today's history first, call `live(kind, start=today)`.
        timeout        stop after this many seconds (default: run until interrupted).
        settle_seconds a flush is read only once it is this old, so a flush is never half-read.

        Example:
            for batch in store.follow("bars_1m", ["SPY", "QQQ"], timeout=600):
                print(batch.select("ticker", "timestamp", "close"))
        """
        if kind not in LIVE_KINDS:
            raise ValueError(f"kind must be one of {LIVE_KINDS}")
        tickers = self._tickers(tickers)
        watermark = since or datetime.now(UTC)
        if watermark.tzinfo is None:
            raise ValueError("since must be timezone-aware (UTC)")
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            cutoff = datetime.now(UTC) - timedelta(seconds=settle_seconds)
            batch = self._live_between(kind, tickers, watermark, cutoff)
            if batch is not None and batch.height:
                watermark = batch["received_at"].max()
                yield batch.to_pandas() if backend == "pandas" else batch
            if deadline is not None and time.monotonic() >= deadline:
                return
            pause = poll_seconds if deadline is None else min(poll_seconds, max(0.0, deadline - time.monotonic()))
            time.sleep(pause)

    def _live_between(self, kind: str, tickers: list[str] | None, after: datetime,
                      until: datetime) -> pl.DataFrame | None:
        """Live rows with after < received_at <= until (day folders from the day before `after`)."""
        first_day = (after.astimezone(ET) - timedelta(days=1)).date().isoformat()
        files = []
        for day_dir in sorted(self.layout.live_dir(kind).glob("date=*")):
            if day_dir.name.split("=", 1)[1] >= first_day:
                files += live_files(day_dir)
        if not files:
            return None
        lf = pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore")
        lf = lf.filter(pl.col("received_at") > after, pl.col("received_at") <= until)
        if tickers:
            lf = lf.filter(pl.col("ticker").is_in(tickers))
        return lf.sort("received_at", TIME_COLUMN[kind], "ticker").collect()

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

    def hour(f: Path) -> str | None:   # hour-YYYYMMDDHH / part-YYYYMMDDHH-<pid>-<n>; anything else: None
        parts = f.stem.split("-")
        return parts[1] if len(parts) > 1 else None

    merged_hours = {hour(f) for f in files if f.name.startswith("hour-")}
    return [f for f in files if f.name.startswith("hour-") or hour(f) not in merged_hours]
