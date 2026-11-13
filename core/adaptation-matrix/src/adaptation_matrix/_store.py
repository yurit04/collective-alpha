"""AdaptationMatrix: reads of the cost store, the day schedule, and the run."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any, Literal

import polars as pl

from ._config import Settings, load_settings
from ._layout import Layout
from ._run import MEASURE_VERSION, Runner, RunResult
from ._schedule import FIRST_TICK_DAY, schedule

Backend = Literal["polars", "pandas"]
BPS = 1e4


def _day(d: Any) -> date | None:
    """None, a date, a datetime (its date) or an ISO string -> date. Every package has this same
    copy (tests/test_shared_helpers.py checks they agree)."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


class AdaptationMatrix:
    """Transaction costs measured from Massive trades and NBBO quotes (see CLAUDE.md)."""

    def __init__(self, root: Any = None, settings: Settings | None = None, store: Any = None,
                 ingestor: Any = None, vinculum: Any = None):
        settings = settings or load_settings()
        if root is not None:
            from dataclasses import replace
            from pathlib import Path
            settings = replace(settings, data_root=Path(root).expanduser())
        self.settings = settings
        self.layout = Layout(settings.data_root)
        self.runner = Runner(settings, store=store, ingestor=ingestor, vinculum=vinculum)

    # ------------------------------------------------------------------ reads
    def _scan(self, table: str, tickers, start, end, by: str) -> pl.LazyFrame | None:
        files = self.layout.files(table)
        start_d, end_d = _day(start), _day(end)
        files = [f for f in files if (start_d is None or f.parent.name[5:] >= start_d.isoformat())
                 and (end_d is None or f.parent.name[5:] <= end_d.isoformat())]
        if not files:
            return None
        lf = pl.scan_parquet(files, missing_columns="insert", extra_columns="ignore") if len(files) > 1 \
            else pl.scan_parquet(files[0])
        if tickers is not None:
            names = [tickers] if isinstance(tickers, str) else list(tickers)
            col = "security_id" if by == "security" else "ticker"
            if by == "ticker":
                names = [n.upper() for n in names]
            lf = lf.filter(pl.col(col).is_in(names))
        return lf

    def _out(self, lf: pl.LazyFrame | None, table: str, backend: Backend, lazy: bool):
        if lf is None:
            lf = pl.LazyFrame(schema={"date": pl.Date, "ticker": pl.String, "security_id": pl.String})
        if lazy:
            return lf
        df = lf.collect()
        return df.to_pandas() if backend == "pandas" else df

    def daily(self, tickers: str | Iterable[str] | None = None, start: Any = None, end: Any = None,
              by: Literal["ticker", "security"] = "ticker", backend: Backend = "polars", lazy: bool = False):
        """One row per measured day and ticker: quoted, effective and realized half-spreads, price
        impact (bps), venue and odd-lot shares, auctions, quote snapshots, quality columns."""
        return self._out(self._scan("daily", tickers, start, end, by), "daily", backend, lazy)

    def minute(self, tickers: str | Iterable[str] | None = None, start: Any = None, end: Any = None,
               by: Literal["ticker", "security"] = "ticker", backend: Backend = "polars", lazy: bool = False):
        """One row per day, ticker and regular-session minute (0 = the first): time-weighted quotes
        with their weight `q_ns`, the quote at the minute's end, signed dollar volume, and
        trade-based measures with their weight `mdv`."""
        return self._out(self._scan("minute", tickers, start, end, by), "minute", backend, lazy)

    def buckets(self, tickers: str | Iterable[str] | None = None, start: Any = None, end: Any = None,
                minutes: int = 30, by: Literal["ticker", "security"] = "ticker", backend: Backend = "polars"):
        """Minute rows aggregated to `minutes`-long buckets of the session (bucket 0 starts at the
        open), with the same weights, so buckets average exactly to days."""
        lf = self._scan("minute", tickers, start, end, by)
        if lf is None:
            return self._out(None, "minute", backend, False)
        qw = pl.col("q_ns").cast(pl.Float64).fill_null(0.0)
        tw = pl.col("mdv").fill_null(0.0)

        def wmean(col: str, w: pl.Expr) -> pl.Expr:
            wn = w.filter(pl.col(col).is_not_null()).sum()
            return pl.when(wn > 0).then((pl.col(col) * w).sum() / wn).alias(col)

        trade_cols = [c for c in lf.collect_schema().names() if c.startswith(("eff_", "rs_", "pi_"))]
        out = (lf.with_columns((pl.col("minute") // minutes).cast(pl.Int16).alias("bucket"))
               .group_by("date", "ticker", "security_id", "bucket")
               .agg(pl.col("q_ns").sum(), wmean("qhs_bps", qw), wmean("qspread", qw), wmean("depth_usd", qw),
                    pl.col("n_trades").sum(), pl.col("volume").sum(), pl.col("dollar_volume").sum(),
                    pl.col("buy_dv").sum(), pl.col("sell_dv").sum(), pl.col("trf_dv").sum(), pl.col("mdv").sum(),
                    *[wmean(c, tw) for c in trade_cols])
               .sort("date", "ticker", "bucket"))
        return self._out(out, "minute", backend, False)

    def trades(self, tickers: str | Iterable[str] | None = None, start: Any = None, end: Any = None,
               by: Literal["ticker", "security"] = "ticker", backend: Backend = "polars", lazy: bool = False):
        """Matched trades of the kept names: t (UTC, the matching time), price, size, exchange,
        conditions, kind (0 continuous, 1 opening auction, 2 closing, 3 reopening), the NBBO in
        force (bid, ask), sign, midpoints 1 and 5 minutes on, quote_age_us."""
        return self._out(self._scan("trades", tickers, start, end, by), "trades", backend, lazy)

    def days(self) -> pl.DataFrame:
        """One row per day in state.json: status, set, version, rows, bytes, timings."""
        state = self.layout.read_state().get("days", {})
        rows = []
        for d, rec in sorted(state.items()):
            rows.append({"day": date.fromisoformat(d), "status": rec.get("status"), "set": rec.get("set"),
                         "current": rec.get("version") == MEASURE_VERSION, "interpolated": rec.get("interpolated"),
                         "daily_rows": (rec.get("rows") or {}).get("daily"),
                         "trades_rows": (rec.get("rows") or {}).get("trades"),
                         "gb": round(sum((rec.get("bytes") or {}).values()) / 1e9, 3),
                         "download_gb": rec.get("download_gb"), "download_mb_s": rec.get("download_mb_s"),
                         "download_s": rec.get("download_s"), "convert_s": rec.get("convert_s"),
                         "measure_s": rec.get("measure_s"), "error": rec.get("error")})
        schema = {"day": pl.Date, "status": pl.String, "set": pl.String, "current": pl.Boolean,
                  "interpolated": pl.Boolean, "daily_rows": pl.Int64, "trades_rows": pl.Int64, "gb": pl.Float64,
                  "download_gb": pl.Float64, "download_mb_s": pl.Float64, "download_s": pl.Float64,
                  "convert_s": pl.Float64, "measure_s": pl.Float64, "error": pl.String}
        return pl.DataFrame(rows, schema=schema)

    def status(self) -> dict[str, Any]:
        d = self.days()
        done = d.filter(pl.col("status") == "done")
        return {
            "root": str(self.layout.root),
            "days_done": done.height,
            "by_set": dict(done.group_by("set").len().iter_rows()) if done.height else {},
            "failed": d.filter(pl.col("status") == "failed")["day"].to_list(),
            "first": done["day"].min() if done.height else None,
            "last": done["day"].max() if done.height else None,
            "store_gb": round(self.layout.store_bytes() / 1e9, 2) if self.layout.root.exists() else 0.0,
            "max_store_gb": self.settings.max_store_gb,
        }

    # ------------------------------------------------------------------ schedule and run
    def schedule(self, end: Any = None, recent_months: int | None = None) -> pl.DataFrame:
        """day, set, priority, done: the days a full run measures, in run order (see _schedule)."""
        store = self.runner.store
        end_d = _day(end) or (date.today() - timedelta(days=1))
        sessions = store.sessions(FIRST_TICK_DAY, end_d)
        sched = schedule(sessions, end_d, recent_months if recent_months is not None else self.settings.recent_months)
        done = {d for d in self.days().filter((pl.col("status") == "done") & pl.col("current"))["day"].to_list()}
        return sched.with_columns(pl.col("day").is_in(list(done)).alias("done"))

    def estimate(self, sched: pl.DataFrame, mb_per_s: float = 15.0) -> pl.DataFrame:
        """Download sizes of the days not done yet, per set and year, from Massive's file list
        (listing only), and the hours they take at `mb_per_s`."""
        todo = sched.filter(~pl.col("done"))
        if todo.height == 0:
            return pl.DataFrame(schema={"set": pl.String, "year": pl.Int32, "days": pl.UInt32, "gb": pl.Float64,
                                        "hours": pl.Float64})
        ing = self.runner.ingestor
        wanted = set(todo["day"].to_list())
        sizes: dict[date, int] = {}
        for year in sorted({d.year for d in wanted}):
            for kind in ("trades", "quotes"):
                for d, n in ing.tick_files(kind, date(year, 1, 1), date(year, 12, 31)):
                    if d in wanted:
                        sizes[d] = sizes.get(d, 0) + n
        df = todo.with_columns(pl.col("day").replace_strict(sizes, default=None, return_dtype=pl.Int64).alias("bytes"))
        return (df.group_by("set", pl.col("day").dt.year().alias("year"))
                .agg(pl.len().alias("days"), (pl.col("bytes").sum() / 1e9).round(1).alias("gb"))
                .with_columns((pl.col("gb") * 1e3 / mb_per_s / 3600).round(1).alias("hours"))
                .sort("set", "year"))

    def run(self, end: Any = None, sets: Iterable[str] | None = None, limit: int | None = None,
            days: Iterable[Any] | None = None, on_day=None) -> RunResult:
        """Measure scheduled days not done yet (or exactly `days`), in schedule order. Long: about
        15 minutes per recent day; ask before starting a full run."""
        if days is not None:
            todo = [(_day(d), "manual") for d in days]
        else:
            sched = self.schedule(end)
            if sets is not None:
                sched = sched.filter(pl.col("set").is_in(list(sets)))
            todo = list(sched.select("day", "set").iter_rows())
        return self.runner.run(todo, limit=limit, on_day=on_day)

    def measure_stored(self, day: Any) -> dict[str, Any]:
        """Measure a day whose full trades and quotes are already in massive-subspace-stream's store
        (`subspace backfill history --dataset trades|quotes` without --symbols). Nothing is
        downloaded; a ticker-filtered stored day gives measures for those tickers only."""
        d = _day(day)
        store = self.runner.store
        open_ns, close_ns = self.runner._session(d)
        lo = pl.lit(open_ns - 6 * 3600 * 10**9).cast(pl.Datetime("ns", "UTC"))
        hi = pl.lit(close_ns + 6 * 3600 * 10**9).cast(pl.Datetime("ns", "UTC"))
        window = pl.col("sip_timestamp").is_between(lo, hi)
        trades, quotes = store.scan("trades").filter(window), store.scan("quotes").filter(window)
        self.layout.ensure()
        with self.layout.run_lock(f"measure stored {d}"):
            return self.runner.measure_day(d, trades, quotes, "stored")
