"""Nanoprobe: intraday reads over the Massive store and the daily summaries store."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Literal

import polars as pl

from ._bars import grid, resample
from ._clock import clock, parse_freq
from ._daily import SUMMARY_COLUMNS, daily_prices, summarize
from ._features import FEATURES, _check, features

log = logging.getLogger("nanoprobe_array")

ENV_ROOT = "NANOPROBE_DATA_ROOT"
WHOLE_MARKET_MAX_DAYS = 5   # bars() for every security at once; longer spans go through days()
CHUNK_DAYS = 20             # days() with named securities reads this many days of minutes at once


def default_root() -> Path:
    return Path(os.environ.get(ENV_ROOT) or "~/market_data/nanoprobe_array").expanduser()


def _day(d: Any) -> date | None:
    if d is None or isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return date.fromisoformat(str(d)[:10])


def _ids(tickers: str | Iterable[str] | None) -> list[str] | None:
    if tickers is None:
        return None
    if isinstance(tickers, str):
        return [t.strip() for t in tickers.split(",") if t.strip()]
    return list(tickers)


def _out(df: pl.DataFrame, backend: str):
    return df.to_pandas() if backend == "pandas" else df


class Nanoprobe:
    """Intraday bars, features, auction prices and daily intraday summaries.

    root      summaries store (default `NANOPROBE_DATA_ROOT` or ~/market_data/nanoprobe_array)
    store     massive_subspace_stream.Store (default: the configured one)
    vinculum  vinculum.Vinculum, only for `top`/`size` universes
    """

    def __init__(self, root: Path | str | None = None, store=None, vinculum=None):
        self.root = Path(root).expanduser() if root else default_root()
        self._store, self._vinculum = store, vinculum

    @property
    def store(self):
        if self._store is None:
            from massive_subspace_stream import Store

            self._store = Store()
        return self._store

    @property
    def vinculum(self):
        if self._vinculum is None:
            from vinculum import Vinculum

            self._vinculum = Vinculum(store=self.store)
        return self._vinculum

    # ------------------------------------------------------------------ clock and prices
    def clock(self, start: Any, end: Any = None, freq: str | int = "5min", backend: str = "polars"):
        """Bars of each regular session: date, bar, bar_start, bar_end, minutes_from_open,
        minutes_to_close, last (early closes included)."""
        start = _day(start)
        return _out(clock(self.store.sessions(start, _day(end) or start), freq), backend)

    def _daily(self, tickers: list[str] | None, start: date, end: date, by: str) -> pl.DataFrame:
        raw = self.store.bars(tickers, start=start - timedelta(days=14), end=end, adjust="all", by=by)
        if raw.height == 0:
            return pl.DataFrame(schema={"date": pl.Date, "security_id": pl.String, "prev_close": pl.Float64})
        return daily_prices(raw).filter(pl.col("date").is_between(start, end))

    def auctions(self, tickers: str | Iterable[str] | None, start: Any, end: Any = None,
                 by: Literal["ticker", "security"] = "ticker", backend: str = "polars"):
        """Official prices per security and day: open_auction, close_auction (raw, auctions
        included), high, low, volume (all venues), prev_close (previous trading day's close on
        this day's share basis) and prev_date.

        The close is known only after the session: use it to price close fills or to evaluate,
        never as an input to a decision taken during that day.
        """
        start = _day(start)
        d = self._daily(_ids(tickers), start, _day(end) or start, by)
        return _out(d.rename({"open": "open_auction", "close": "close_auction"}), backend)

    # ------------------------------------------------------------------ bars
    def _prepare(self, minutes: pl.DataFrame, sessions: pl.DataFrame, freq: int, fill: str,
                 daily: pl.DataFrame | None, ids: list[str] | None = None) -> pl.DataFrame:
        if fill not in ("none", "grid"):
            raise ValueError("fill must be 'none' or 'grid'")
        b = resample(minutes, sessions, freq)
        if fill == "grid":
            b = grid(b, sessions, freq, security_ids=ids)
        if daily is not None:
            b = b.join(daily.select("date", "security_id", "prev_close"), on=["date", "security_id"], how="left")
        return b

    def bars(self, tickers: str | Iterable[str], start: Any, end: Any = None, freq: str | int = "5min",
             by: Literal["ticker", "security"] = "ticker", fill: Literal["none", "grid"] = "none",
             prev_close: bool = False, features: Iterable[str] | str | None = None,
             or_minutes: int = 30, lookback: int = 20, include_live: bool = False, backend: str = "polars"):
        """Regular-session bars of `freq` for the given tickers (or, with by='security', security
        ids / tickers followed across renames).

        Columns: date, security_id, ticker, bar, bar_start, bar_end, minutes_from_open,
        minutes_to_close, last, open, high, low, close, volume, vwap, dollar_volume,
        transactions, minutes, last_trade, stale [, prev_close] [, features].
        Prices are raw within each day (as traded); compare days through prev_close or
        `auctions`. fill='grid' adds every empty bar (see `grid`). `features` adds per-bar
        features (FEATURES); vol_ratio needs fill='grid' and is null for the first days.
        For every security at once use `days()`: a year of the whole market does not fit in memory.
        """
        ids = _ids(tickers)
        if not ids:
            raise ValueError("bars() needs tickers; for the whole market iterate with days()")
        start = _day(start)
        end = _day(end) or start
        n = parse_freq(freq)
        names = _check(features) if features is not None else []
        if "vol_ratio" in names and fill != "grid":
            raise ValueError("vol_ratio compares bars across days: use fill='grid'")
        minutes = self.store.bars(ids, start=start, end=end, freq="1min", session="regular", by=by,
                                  include_live=include_live)
        sessions = self.store.sessions(start, end)
        daily = self._daily(ids, start, end, by) if prev_close or any(FEATURES[x].needs == "prev_close" for x in names) else None
        b = self._prepare(minutes, sessions, n, fill, daily)
        if names:
            b = self._features(b, names, or_minutes, lookback, None)
        return _out(b, backend)

    @staticmethod
    def _features(b, names, or_minutes, lookback, history):
        return features(b, names, or_minutes=or_minutes, lookback=lookback, history=history)

    def universe(self, on: Any, top: int | None = None, size: str | None = None,
                 types: Iterable[str] = ("CS", "ADRC")) -> pl.DataFrame:
        """vinculum's size universe for `on`, ranked at the previous month end (known before the
        open): date, security_id, ticker, rank, size_bucket ... as vinculum returns it."""
        return self.vinculum.universe(_day(on), top=top, size=size, types=tuple(types))

    def days(self, start: Any, end: Any = None, tickers: str | Iterable[str] | None = None,
             top: int | None = None, size: str | None = None, freq: str | int = "5min",
             fill: Literal["none", "grid"] = "grid", prev_close: bool = False,
             features: Iterable[str] | str | None = None, or_minutes: int = 30, lookback: int = 20,
             include_live: bool = False) -> Iterator[tuple[date, pl.DataFrame]]:
        """Yield (day, bars) one trading day at a time: the way to work over many securities.

        Securities: `tickers` (security ids or tickers as of that day), vinculum's `top` N /
        `size` bucket for that day, both (union, e.g. top=500 plus SPY), or every security that
        traded when none is given. With vol_ratio the `lookback` days before `start` are read first
        (not yielded). Memory stays at about one day (~140 MB for the whole market).
        """
        start = _day(start)
        end = _day(end) or start
        n = parse_freq(freq)
        names = _check(features) if features is not None else []
        if "vol_ratio" in names and fill != "grid":
            raise ValueError("vol_ratio compares bars across days: use fill='grid'")
        ids = _ids(tickers)
        need_prev = prev_close or any(FEATURES[x].needs == "prev_close" for x in names)
        warm = lookback if "vol_ratio" in names else 0
        cal = self.store.trading_days(start - timedelta(days=int(warm * 1.6) + 7), end)
        first = cal.index(next(d for d in cal if d >= start)) if any(d >= start for d in cal) else len(cal)
        cal = cal[max(0, first - warm):]
        only_ids = ids if not (top or size) else None     # read just these (tickers that day or ids)
        if not cal:
            return
        sessions = self.store.sessions(cal[0], cal[-1])
        if only_ids:
            yield from self._days_named(only_ids, cal, start, end, sessions, n, fill, need_prev, names,
                                        or_minutes, lookback, warm, include_live)
            return
        history: list[pl.DataFrame] = []
        daily = None
        for d in cal:
            raw = self.store.bars(None, start=d, end=d, freq="1min", session="regular", include_live=include_live)
            if top or size:
                keep = set(self.universe(d, top=top, size=size)["security_id"].to_list())
                if ids:
                    keep |= set(raw.filter(pl.col("ticker").is_in(ids) | pl.col("security_id").is_in(ids))
                                ["security_id"].to_list())
                raw = raw.filter(pl.col("security_id").is_in(list(keep)))
            if need_prev:
                if daily is None or daily.height == 0 or d > daily["date"].max():
                    daily = self._daily(None, d, min(end, d + timedelta(days=90)), "ticker")
                day_daily = daily.filter(pl.col("date") == d)
            else:
                day_daily = None
            b = self._prepare(raw, sessions.filter(pl.col("date") == d), n, fill, day_daily)
            if names:
                hist = pl.concat(history) if history else None
                b = self._features(b, names, or_minutes, lookback, hist)
                if warm:
                    history.append(b.select("security_id", "date", "bar", "volume"))
                    history = history[-lookback:]
            if d >= start:
                yield d, b

    def _days_named(self, ids, cal, start, end, sessions, n, fill, need_prev, names, or_minutes, lookback,
                    warm, include_live) -> Iterator[tuple[date, pl.DataFrame]]:
        """days() for a few named securities: CHUNK_DAYS days are read and prepared in one pass
        (every step works per security and day, so the result is the same as day by day)."""
        history: pl.DataFrame | None = None
        daily = None
        for i in range(0, len(cal), CHUNK_DAYS):
            days_c = cal[i:i + CHUNK_DAYS]
            raw = self.store.bars(ids, start=days_c[0], end=days_c[-1], freq="1min", session="regular",
                                  by="security", include_live=include_live)
            day_daily = None
            if need_prev:
                if daily is None or daily.height == 0 or days_c[-1] > daily["date"].max():
                    daily = self._daily(ids, days_c[0], min(end, days_c[0] + timedelta(days=365)), "security")
                day_daily = daily.filter(pl.col("date").is_between(days_c[0], days_c[-1]))
            b = self._prepare(raw, sessions.filter(pl.col("date").is_between(days_c[0], days_c[-1])), n, fill,
                              day_daily)
            if names:
                b = self._features(b, names, or_minutes, lookback, history)
                if warm:
                    h = b.select("security_id", "date", "bar", "volume")
                    history = h if history is None else pl.concat([history, h])
                    keep = sorted(history["date"].unique().to_list())[-lookback:]
                    history = history.filter(pl.col("date").is_in(keep))
            parts = {k[0]: g for k, g in b.partition_by("date", as_dict=True).items()}
            for d in days_c:
                if d >= start:
                    yield d, parts.get(d, b.clear())

    # ------------------------------------------------------------------ summaries store
    def _summary_files(self) -> list[Path]:
        return sorted((self.root / "summaries").glob("year=*.parquet"))

    def summaries(self, tickers: str | Iterable[str] | None = None, start: Any = None, end: Any = None,
                  columns: list[str] | None = None, lazy: bool = False, backend: str = "polars"):
        """Daily intraday summaries (one row per security and day; see SUMMARY_COLUMNS and
        README). `tickers` filters by ticker or security_id. Build with `build()` first."""
        files = self._summary_files()
        if not files:
            raise FileNotFoundError("no intraday summaries yet. Run: nanoprobe build")
        lf = pl.scan_parquet(files)
        start, end = _day(start), _day(end)
        if start:
            lf = lf.filter(pl.col("date") >= start)
        if end:
            lf = lf.filter(pl.col("date") <= end)
        ids = _ids(tickers)
        if ids:
            lf = lf.filter(pl.col("ticker").is_in(ids) | pl.col("security_id").is_in(ids))
        if columns:
            lf = lf.select(columns)
        if lazy:
            return lf
        return _out(lf.collect(), backend)

    def _manifest(self) -> dict:
        p = self.root / "build.json"
        return json.loads(p.read_text()) if p.exists() else {"years": {}}

    def _minute_span(self) -> tuple[date, date]:
        cov = self.store.coverage().filter(pl.col("dataset") == "bars_minute")
        if cov.height == 0:
            raise FileNotFoundError("no minute bars stored. See massive-subspace-stream's README")
        return date.fromisoformat(cov["first"][0]), date.fromisoformat(cov["last"][0])

    def build(self, years: Iterable[int] | None = None, workers: int = 4, full: bool = False) -> dict:
        """Build the daily summaries, one file per year.

        years    the years to (re)build; default: every year whose file is missing or older than
                 the stored minute bars (so a nightly run redoes only the current year), or all
                 years with full=True.
        workers  processes, one year each (spawned: forked polars can deadlock).
        """
        t0 = time.time()
        first, last = self._minute_span()
        manifest = self._manifest()
        built = manifest.get("years", {})
        if years is None:
            todo = []
            for y in range(first.year, last.year + 1):
                info = built.get(str(y))
                want = min(last, date(y, 12, 31)).isoformat()
                if full or info is None or info.get("last_date", "") < want or \
                        not (self.root / "summaries" / f"year={y}.parquet").exists():
                    todo.append(y)
        else:
            todo = sorted(set(int(y) for y in years))
        (self.root / "summaries").mkdir(parents=True, exist_ok=True)
        results = []
        if workers > 1 and len(todo) > 1:
            store_root = str(self.store.root)
            with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as ex:
                futures = [ex.submit(_build_year_worker, y, str(self.root), store_root) for y in todo]
                for f in futures:
                    results.append(f.result())
                    log.info("year %s: %s rows, %s days, %ss", *[results[-1][k] for k in ("year", "rows", "days", "seconds")])
        else:
            for y in todo:
                results.append(build_year(y, self.root, self.store))
                log.info("year %s: %s rows, %s days, %ss", *[results[-1][k] for k in ("year", "rows", "days", "seconds")])
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for r in results:
            if r["days"]:
                built[str(r["year"])] = {"rows": r["rows"], "days": r["days"], "first_date": r["first_date"],
                                         "last_date": r["last_date"], "built_at": now}
        manifest = {"years": dict(sorted(built.items())), "built_at": now, "columns": SUMMARY_COLUMNS}
        tmp = self.root / "build.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=1))
        tmp.replace(self.root / "build.json")
        return {"years": [r["year"] for r in results], "rows": sum(r["rows"] for r in results),
                "days": sum(r["days"] for r in results), "seconds": round(time.time() - t0, 1)}

    def status(self) -> dict:
        """What the summaries store holds and whether it is behind the minute bars."""
        m = self._manifest()
        years = m.get("years", {})
        out = {"root": str(self.root), "built_at": m.get("built_at"), "years": len(years),
               "rows": sum(v["rows"] for v in years.values()),
               "first_date": min((v["first_date"] for v in years.values()), default=None),
               "last_date": max((v["last_date"] for v in years.values()), default=None)}
        try:
            out["minute_bars_last"] = self._minute_span()[1].isoformat()
            out["behind"] = out["last_date"] is None or out["last_date"] < out["minute_bars_last"]
        except FileNotFoundError:
            out["minute_bars_last"], out["behind"] = None, None
        return out


def build_year(year: int, root: Path | str, store) -> dict:
    """Summaries for one year, written atomically to <root>/summaries/year=<year>.parquet."""
    t0 = time.time()
    start, end = date(year, 1, 1), date(year, 12, 31)
    days = store.trading_days(start, end)
    raw = store.bars(None, start=start - timedelta(days=14), end=end, adjust="all")
    daily = daily_prices(raw).filter(pl.col("date").is_between(start, end)) if raw.height else None
    frames = []
    for d in days:
        minutes = store.bars(None, start=d, end=d, freq="1min", session="regular")
        if minutes.height == 0 or daily is None:
            continue
        frames.append(summarize(minutes, store.sessions(d, d), daily.filter(pl.col("date") == d)))
    out = pl.concat(frames) if frames else pl.DataFrame()
    path = Path(root) / "summaries" / f"year={year}.parquet"
    if out.height:
        tmp = path.with_suffix(".tmp")
        out.write_parquet(tmp, compression="zstd", statistics=True)
        tmp.replace(path)
    return {"year": year, "rows": out.height, "days": out["date"].n_unique() if out.height else 0,
            "first_date": out["date"].min().isoformat() if out.height else None,
            "last_date": out["date"].max().isoformat() if out.height else None,
            "seconds": round(time.time() - t0, 1)}


def _build_year_worker(year: int, root: str, store_root: str) -> dict:
    from massive_subspace_stream import Store

    return build_year(year, root, Store(root=store_root))
