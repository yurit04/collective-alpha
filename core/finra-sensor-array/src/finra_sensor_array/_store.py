"""FinraStore: reads of the stored short interest and short sale volume."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl

from ._config import Settings, load_settings
from ._layout import Layout
from ._parse import SI_SCHEMA, SV_SCHEMA, finra_symbols

Backend = Literal["polars", "pandas"]
_SCHEMAS = {"short_interest": SI_SCHEMA, "short_volume": SV_SCHEMA}


def _day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class FinraStore:
    """Stored FINRA data, keyed by FINRA's own symbols (not identifiers: a symbol can be reused
    by another company; map to securities through the security master, as vinculum does).

    `tickers` may be FINRA symbols or Massive-style tickers: BRK.B finds short interest's BRKB
    and daily volume's BRK/B.
    """

    def __init__(self, root: Path | str | None = None, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.layout = Layout(root or self.settings.data_root)

    def __repr__(self) -> str:
        return f"FinraStore(root={str(self.layout.root)!r})"

    def scan(self, dataset: Literal["short_interest", "short_volume"]) -> pl.LazyFrame:
        if dataset not in _SCHEMAS:
            raise ValueError(f"unknown dataset {dataset!r}; choose from {list(_SCHEMAS)}")
        files = self.layout.files(dataset)
        if not files:
            raise FileNotFoundError(f"no {dataset} stored yet. Run: sensor backfill {dataset.replace('_', '-')}")
        return pl.scan_parquet(files, schema=_SCHEMAS[dataset])

    def _query(self, dataset: str, date_col: str, tickers, start, end, lazy: bool, backend: Backend):
        lf = self.scan(dataset)
        s, e = _day(start), _day(end)
        if s:
            lf = lf.filter(pl.col(date_col) >= s)
        if e:
            lf = lf.filter(pl.col(date_col) <= e)
        names = _list(tickers)
        if names:
            wanted = set(names) | {n.upper() for n in names} | set(finra_symbols(names, dataset))
            lf = lf.filter(pl.col("symbol").is_in(sorted(wanted)))
        lf = lf.sort(date_col, "symbol")
        if lazy:
            return lf
        df = lf.collect()
        return df.to_pandas() if backend == "pandas" else df

    def short_interest(self, tickers=None, start: Any = None, end: Any = None,
                       date_by: Literal["settlement", "published"] = "settlement", lazy: bool = False,
                       backend: Backend = "polars"):
        """Short interest twice a month (exchange-listed and OTC, from 2017-12-29): settlement_date,
        published_date (when FINRA released it: use this for anything point in time), symbol,
        issue_name, market, short_interest, previous_short_interest, change, change_pct,
        avg_daily_volume, days_to_cover, split_flag, revision_flag, fetched_at.
        start/end filter on settlement_date, or published_date with date_by="published"."""
        if date_by not in ("settlement", "published"):
            raise ValueError("date_by must be 'settlement' or 'published'")
        return self._query("short_interest", f"{date_by}_date", tickers, start, end, lazy, backend)

    def short_volume(self, tickers=None, start: Any = None, end: Any = None, lazy: bool = False,
                     backend: Backend = "polars"):
        """Daily short sale volume of off-exchange trades (reported to the TRFs, the ADF and, for
        OTC stocks, the ORF), from 2009-08-03: date, symbol, short_volume, short_exempt_volume
        (null before 2011), total_volume (off-exchange), markets, source (CNMS, TRF+ADF summed
        before 2018-08, or ORF for OTC stocks).
        Posted by 6 pm ET on the trade date: usable from the next trading day."""
        return self._query("short_volume", "date", tickers, start, end, lazy, backend)

    def status(self) -> dict[str, Any]:
        """Per dataset: first and last date with data, days, rows, bytes."""
        state = self.layout.read_state()
        out: dict[str, Any] = {"root": str(self.layout.root)}
        for name in _SCHEMAS:
            days = state.get(name, {}).get("days", {})
            files = self.layout.files(name)
            out[name] = {"days": len(days), "first": min(days) if days else None, "last": max(days) if days else None,
                         "rows": sum(days.values()), "bytes": sum(f.stat().st_size for f in files)}
        return out


def _list(x: str | Iterable[str] | None) -> list[str] | None:
    if x is None:
        return None
    if isinstance(x, str):
        x = x.split(",")
    return [t.strip() for t in x if t.strip()]  # case kept: Massive writes preferreds as ABRpD
