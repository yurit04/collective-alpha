"""Downloads: short interest per settlement date, short sale volume per day.

Every business day in a range is tried once; finished days are recorded in state.json and
skipped next time. A day with no data is recorded as empty only once FINRA would certainly
have published it (short interest: 3 business days after its publication date; daily files:
3 business days later), so a day asked for too early is tried again. Recent days are fetched
again on each run to pick up FINRA's revisions.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from ._calendar import add_business_days, business_days, si_published, today_et
from ._client import FinraClient, FinraError
from ._config import Settings
from ._layout import Layout, write_atomic
from ._parse import (
    CNMS_START,
    SI_SCHEMA,
    SV_SCHEMA,
    combine_facilities,
    facilities,
    parse_daily_file,
    short_interest_frame,
)

log = logging.getLogger("finra_sensor_array")

SI_START = date(2017, 12, 1)   # the consolidated (exchange-listed + OTC) data begins 2017-12-29
SV_START = date(2009, 8, 3)    # first Reg SHO daily files
SETTLE_DAYS = 3                # business days after publication before a day counts as empty
SI_REFRESH_DAYS = 30           # re-fetch settlement dates published within this many days
SV_REFRESH_DAYS = 3            # re-fetch the last few daily files (FINRA re-posts some)


@dataclass
class JobResult:
    dataset: str
    done: int = 0
    empty: int = 0
    skipped: int = 0
    rows: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    def line(self) -> str:
        parts = [f"{self.dataset}: {self.done} with data", f"{self.empty} without", f"{self.skipped} already stored"]
        if self.rows:
            parts.append(f"{self.rows:,} rows")
        if self.failed:
            parts.append(f"{len(self.failed)} FAILED")
        if self.note:
            parts.append(self.note)
        return ", ".join(parts)


class Ingestor:
    def __init__(self, settings: Settings, client: Any = None, today: Callable[[], date] = today_et):
        self.settings = settings
        self.layout = Layout(settings.data_root)
        self.layout.ensure()
        self._client = client
        self._today = today

    @property
    def client(self) -> FinraClient:
        if self._client is None:
            self._client = FinraClient(self.settings.user_agent, self.settings.requests_per_second)
        return self._client

    # ------------------------------------------------------------------ shared
    def _plan(self, dataset: str, start: date, end: date, refresh_days: int,
              published: Callable[[date], date]) -> tuple[list[date], int]:
        state = self.layout.read_state().get(dataset, {})
        have, empty = set(state.get("days", {})), set(state.get("empty", []))
        today = self._today()
        todo, skipped = [], 0
        for d in business_days(start, min(end, today)):
            key = d.isoformat()
            stale = key in have and published(d) < today - timedelta(days=refresh_days)
            if key in empty or stale:
                skipped += 1
            else:
                todo.append(d)
        return todo, skipped

    def _run(self, dataset: str, days: list[date], fetch: Callable[[date], pl.DataFrame | None],
             published: Callable[[date], date], schema: dict, keys: list[str], date_col: str,
             result: JobResult) -> None:
        """Fetch days in parallel, one year at a time; write each year file once."""
        by_year: dict[int, list[date]] = defaultdict(list)
        for d in days:
            by_year[d.year].append(d)
        total, n = len(days), 0
        for year in sorted(by_year):
            frames: list[pl.DataFrame] = []
            found: dict[str, int] = {}
            gone: list[str] = []
            with ThreadPoolExecutor(max_workers=max(1, self.settings.workers)) as pool:
                futures = {pool.submit(fetch, d): d for d in by_year[year]}
                for fut in as_completed(futures):
                    d, n = futures[fut], n + 1
                    try:
                        df = fut.result()
                    except (FinraError, ValueError, pl.exceptions.PolarsError) as exc:
                        result.failed[d.isoformat()] = str(exc)
                        log.error("%s %s failed: %s", dataset, d, exc)
                        continue
                    if df is None or df.is_empty():
                        if add_business_days(published(d), SETTLE_DAYS) < self._today():
                            gone.append(d.isoformat())
                            result.empty += 1
                        continue
                    frames.append(df)
                    found[d.isoformat()] = df.height
                    result.done += 1
                    result.rows += df.height
                    if n % 250 == 0:
                        log.info("%s: %d/%d days", dataset, n, total)
            if frames:
                self._write_year(dataset, year, pl.concat(frames), schema, keys, date_col)
                log.info("%s %d: %d day(s) with data", dataset, year, len(found))
            self._record(dataset, found, gone)

    def _write_year(self, dataset: str, year: int, new: pl.DataFrame, schema: dict, keys: list[str],
                    date_col: str) -> None:
        path = self.layout.year_file(dataset, year)
        if path.exists():
            old = pl.read_parquet(path)
            new = pl.concat([old.filter(~pl.col(date_col).is_in(new[date_col].unique().implode())), new],
                            how="diagonal_relaxed")
        write_atomic(new.select(list(schema)).cast(schema).sort(keys), path)

    def _record(self, dataset: str, found: dict[str, int], gone: list[str]) -> None:
        state = self.layout.read_state()
        s = state.setdefault(dataset, {"days": {}, "empty": []})
        s["days"].update(found)
        s["empty"] = sorted(set(s["empty"]) | set(gone))
        self.layout.write_state(state)

    # ------------------------------------------------------------------ short interest
    def short_interest(self, start: Any = None, end: Any = None) -> JobResult:
        """Short interest for every settlement date in [start, end] (default: all since 2017-12)."""
        start, end = _day(start) or SI_START, _day(end) or self._today()
        result = JobResult("short_interest")
        days, result.skipped = self._plan("short_interest", start, end, SI_REFRESH_DAYS, si_published)
        log.info("short_interest: trying %d business day(s) between %s and %s", len(days), start, end)

        def fetch(d: date) -> pl.DataFrame:
            return short_interest_frame(d, self.client.short_interest(d.isoformat()))

        self._run("short_interest", days, fetch, si_published, SI_SCHEMA,
                  ["settlement_date", "symbol", "market"], "settlement_date", result)
        return result

    # ------------------------------------------------------------------ short volume
    def short_volume(self, start: Any = None, end: Any = None) -> JobResult:
        """Daily off-exchange short sale volume for every business day in [start, end]
        (default: all since 2009-08)."""
        start, end = _day(start) or SV_START, _day(end) or self._today()
        result = JobResult("short_volume")
        same_day = lambda d: d  # noqa: E731  (posted by 6 pm ET on the trade date)
        days, result.skipped = self._plan("short_volume", start, end, SV_REFRESH_DAYS, same_day)
        log.info("short_volume: trying %d business day(s) between %s and %s", len(days), start, end)

        def fetch(d: date) -> pl.DataFrame | None:
            texts = [self.client.daily_file(f, d) for f in facilities(d)]
            frames = [parse_daily_file(t) for t in texts if t]
            if not frames:
                return None
            return combine_facilities(frames, "CNMS" if d >= CNMS_START else "TRF+ADF")

        self._run("short_volume", days, fetch, same_day, SV_SCHEMA, ["date", "symbol"], "date", result)
        return result

    def sync(self) -> list[JobResult]:
        """Daily catch-up: both datasets from a little before their last stored day."""
        out = []
        for name, job in (("short_interest", self.short_interest), ("short_volume", self.short_volume)):
            days = sorted(self.layout.read_state().get(name, {}).get("days", {}))
            if not days:
                out.append(JobResult(name, note=f"skipped: never backfilled (run `sensor backfill {name.replace('_', '-')}`)"))
                continue
            out.append(job(start=date.fromisoformat(days[-1]) - timedelta(days=45)))
        return out


def _day(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
