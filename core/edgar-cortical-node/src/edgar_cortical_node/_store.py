"""Read-side API over the EDGAR data root, by CIK."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl

from ._concepts import CONCEPTS
from ._config import Settings, load_settings
from ._layout import BUCKETS, Layout, bucket_of
from ._parse import COMPANY_SCHEMA, FACT_SCHEMA, pad_cik
from ._pit import SERIES_SCHEMA, STALE_DAYS, build_series, build_shares, latest_as_of

Backend = Literal["polars", "pandas"]
Policy = Literal["first", "latest"]


def _day(d: Any) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if hasattr(d, "date"):  # pandas Timestamp
        return d.date()
    return date.fromisoformat(str(d)[:10])


class EdgarStore:
    """Point-in-time SEC fundamentals.

    Example:
        store = EdgarStore()
        store.series("revenue", ciks=[320193])            # quarters + TTM, first-filed values
        store.shares([320193, 1326801])                   # shares outstanding history
        store.as_of("2020-06-30", ciks=[320193])          # what was knowable that day
    """

    def __init__(self, root: Path | str | None = None, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.layout = Layout(root or self.settings.data_root)
        self.root = self.layout.root

    def __repr__(self) -> str:
        return f"EdgarStore(root='{self.root}')"

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _ciks(ciks: Any) -> list[str] | None:
        if ciks is None:
            return None
        if isinstance(ciks, (str, int)):
            ciks = [ciks]
        return sorted({pad_cik(c) for c in ciks})

    @staticmethod
    def _out(df: pl.DataFrame, backend: Backend):
        if backend == "pandas":
            return df.to_pandas()
        if backend != "polars":
            raise ValueError("backend must be 'polars' or 'pandas'")
        return df

    def scan(self, ciks: Any = None) -> pl.LazyFrame:
        """Lazy frame over stored facts (only the buckets holding `ciks`, if given)."""
        ciks = self._ciks(ciks)
        buckets = sorted({bucket_of(c) for c in ciks}) if ciks else range(BUCKETS)
        files = [self.layout.bucket_file(b) for b in buckets]
        files = [f for f in files if f.exists()]
        if not files:
            if not self.layout.facts_dir.exists():
                raise FileNotFoundError("no EDGAR facts stored yet. Run: cortical backfill")
            return pl.DataFrame(schema=FACT_SCHEMA).lazy()
        lf = pl.scan_parquet(files)
        return lf.filter(pl.col("cik").is_in(ciks)) if ciks else lf

    # ------------------------------------------------------------------ raw facts
    def facts(self, ciks: Any = None, tags: Iterable[str] | str | None = None, start: Any = None,
              end: Any = None, filed_before: Any = None, backend: Backend = "polars", lazy: bool = False):
        """Raw facts, every filing kept. `tags` are 'taxonomy:Tag' strings or item names
        from CONCEPTS; `start`/`end` bound the period end; `filed_before` keeps facts filed
        strictly before a day (i.e. usable on it)."""
        lf = self.scan(ciks)
        if tags is not None:
            tags = [tags] if isinstance(tags, str) else list(tags)
            full = []
            for t in tags:
                full += list(CONCEPTS[t].tags) if t in CONCEPTS else [t]
            lf = lf.filter(pl.concat_str(["taxonomy", "tag"], separator=":").is_in(full))
        if start is not None:
            lf = lf.filter(pl.col("end") >= _day(start))
        if end is not None:
            lf = lf.filter(pl.col("end") <= _day(end))
        if filed_before is not None:
            lf = lf.filter(pl.col("filed") < _day(filed_before))
        if lazy:
            return lf
        return self._out(lf.collect(), backend)

    def companies(self, backend: Backend = "polars"):
        """cik, name, number of facts, last filing date."""
        path = self.layout.companies_file
        df = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=COMPANY_SCHEMA)
        return self._out(df, backend)

    @staticmethod
    def concepts() -> dict[str, dict]:
        return {k: {"kind": c.kind, "unit": c.unit, "tags": list(c.tags), "description": c.description}
                for k, c in CONCEPTS.items()}

    # ------------------------------------------------------------------ point-in-time series
    def series(self, items: str | Iterable[str], ciks: Any = None, policy: Policy = "first",
               backend: Backend = "polars"):
        """Point-in-time series for items in CONCEPTS (long format).

        Flows: one row per fiscal quarter (reported, or derived from year-to-date figures and
        annual minus nine months) with `ttm` (trailing twelve months). Instants: one row per
        balance-sheet date. Per-share items: reported quarters and years.
        Every row has `filed` and `available_from` (the day after filing).
        """
        items = [items] if isinstance(items, str) else list(items)
        unknown = [i for i in items if i not in CONCEPTS]
        if unknown:
            raise ValueError(f"unknown item(s) {unknown}; choose from {sorted(CONCEPTS)}")
        tags = sorted({t for i in items for t in CONCEPTS[i].tags})
        facts = self.facts(ciks, tags=tags)
        frames = [build_series(facts, i, CONCEPTS[i], policy) for i in items]
        df = pl.concat(frames) if frames else pl.DataFrame(schema=SERIES_SCHEMA)
        return self._out(df, backend)

    def shares(self, ciks: Any = None, policy: Policy = "first", backend: Backend = "polars"):
        """Shares outstanding over time: cover page (dei) > balance sheet > weighted average basic,
        one concept per company (the one in most filings), values >20x off the latest dropped.
        Multi-class companies have no cover-page total in company facts and use the weighted
        average of all classes. As reported: not split-adjusted."""
        from ._concepts import SHARE_TAGS

        facts = self.facts(ciks, tags=list(SHARE_TAGS))
        return self._out(build_shares(facts, policy), backend)

    def as_of(self, on: Any, ciks: Any = None, items: Iterable[str] | None = None, policy: Policy = "first",
              stale_days: int = STALE_DAYS, backend: Backend = "polars"):
        """What was knowable on `on`: the newest usable value per company and item (flows also
        give `ttm`), plus `shares`. Values whose period ended more than `stale_days` before are
        left out."""
        day = _day(on)
        items = list(items) if items is not None else list(CONCEPTS)
        s = self.series(items, ciks, policy)
        # TTM must itself be usable on the day.
        s = s.with_columns(pl.when(pl.col("ttm_available_from") <= day).then(pl.col("ttm")).otherwise(None).alias("ttm"))
        latest = latest_as_of(s, day, ["cik", "item"], stale_days)
        sh = latest_as_of(self.shares(ciks, policy), day, ["cik"], stale_days)
        sh_rows = sh.select("cik", pl.lit("shares").alias("item"), pl.lit("shares").alias("unit"),
                            pl.lit("instant").alias("period"), pl.lit(None, pl.Date).alias("start"), "end",
                            pl.col("shares").alias("value"), "filed", "available_from", "tag",
                            pl.lit("reported").alias("source"), pl.lit(None, pl.Float64).alias("ttm"),
                            pl.lit(None, pl.Date).alias("ttm_available_from"))
        out = pl.concat([latest.select(list(SERIES_SCHEMA)), sh_rows.select(list(SERIES_SCHEMA))])
        return self._out(out.drop("ttm_available_from").sort("cik", "item"), backend)

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        state = self.layout.read_state()
        size = sum(p.stat().st_size for p in self.layout.facts_dir.rglob("*.parquet")) \
            if self.layout.facts_dir.exists() else 0
        comps = self.companies()
        return {**state, "companies": comps.height, "facts_bytes": size, "root": str(self.root)}
