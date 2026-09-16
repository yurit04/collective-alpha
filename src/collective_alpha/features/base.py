"""Feature framework.

A *feature group* is a function ``build(ctx) -> DataFrame(security_id, date, <feature columns>)``.
Every value at (security_id, date) uses information available at that session's close; nothing
later. Groups are materialised under curated/features/<group>/year=YYYY and joined on load.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from functools import cached_property
from pathlib import Path

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.config import Settings, get_settings
from collective_alpha.storage import layout
from collective_alpha.storage.parquet import write_parquet_atomic

log = logging.getLogger(__name__)

Builder = Callable[["FeatureContext"], pl.DataFrame]


class FeatureContext:
    """Lazy, cached access to every curated input a feature group may need."""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def _latest(self, table: str) -> pl.DataFrame:
        d = layout.curated_table_dir(self.s, table)
        parts = sorted(p for p in d.glob("asof=*") if p.is_dir())
        if not parts:
            raise RuntimeError(f"no curated snapshot for {table}")
        return pl.read_parquet(parts[-1] / "data.parquet")

    @cached_property
    def panel(self) -> pl.DataFrame:
        from collective_alpha.panel.build import load_panel

        return load_panel(self.s).sort(["security_id", "date"])

    @cached_property
    def sessions(self) -> list[dt.date]:
        return trading_days(self.panel["date"].min(), self.panel["date"].max())

    @cached_property
    def intraday(self) -> pl.DataFrame:
        from collective_alpha.intraday.build import load_intraday

        return load_intraday(self.s)

    @cached_property
    def master(self) -> pl.DataFrame:
        return self._latest("security_master")

    @cached_property
    def securities(self) -> pl.DataFrame:
        return self._latest("securities")

    @cached_property
    def attributes(self) -> pl.DataFrame:
        from collective_alpha.universe.attributes import load_security_attributes

        return load_security_attributes(self.s)

    @cached_property
    def ticker_details(self) -> pl.DataFrame:
        return self._latest("ticker_details")

    @cached_property
    def sec_facts(self) -> pl.DataFrame:
        return self._latest("sec_facts")

    @cached_property
    def shares(self) -> pl.DataFrame:
        from collective_alpha.universe.marketcap import shares_series

        return shares_series(self.sec_facts, self.ticker_details)

    def _monthly(self, table: str) -> pl.DataFrame:
        d = layout.curated_table_dir(self.s, table)
        files = sorted(d.glob("year=*/month=*/data.parquet"))
        # monthly files can differ in columns (e.g. news `insights` appears mid-2024)
        return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")

    @cached_property
    def short_interest(self) -> pl.DataFrame:
        return self._monthly("short_interest")

    @cached_property
    def short_volume(self) -> pl.DataFrame:
        return self._monthly("short_volume")

    @cached_property
    def news(self) -> pl.DataFrame:
        return self._monthly("news")

    @cached_property
    def security_cik(self) -> pl.DataFrame:
        """(security_id, date, cik) for every panel row, point-in-time from attributes."""
        from collective_alpha.universe.attributes import attributes_asof

        keys = self.panel.select("security_id", "date")
        a = attributes_asof(self.attributes.select("security_id", "asof", "cik"), keys)
        return a.select("security_id", "date", "cik")


# ---------------------------------------------------------------- registry


def registry() -> dict[str, Builder]:
    from collective_alpha.features import fundamentals, intraday, news, price, short, size

    return {
        "price": price.build,
        "size": size.build,
        "fund": fundamentals.build,
        "short": short.build,
        "news": news.build,
        "intra": intraday.build,
    }


# ---------------------------------------------------------------- persistence


def group_dir(settings: Settings, group: str) -> Path:
    return layout.curated_table_dir(settings, "features") / group


def write_group(settings: Settings, group: str, df: pl.DataFrame) -> int:
    d = group_dir(settings, group)
    for stale in d.glob("year=*/data.parquet"):
        stale.unlink()
        if not any(stale.parent.iterdir()):
            stale.parent.rmdir()
    n = 0
    for (year,), part in df.with_columns(year=pl.col("date").dt.year()).group_by("year"):
        n += write_parquet_atomic(part.drop("year").sort(["date", "security_id"]), d / f"year={year}" / "data.parquet")
    return n


def build_groups(groups: list[str] | None = None, settings: Settings | None = None) -> dict[str, dict]:
    s = settings or get_settings()
    ctx = FeatureContext(s)
    reg = registry()
    out = {}
    for g in groups or list(reg):
        log.info("building feature group %s", g)
        df = reg[g](ctx)
        n = write_group(s, g, df)
        feats = [c for c in df.columns if c not in ("security_id", "date")]
        out[g] = {"rows": n, "features": feats}
    return out


def load_features(
    groups: list[str] | None = None,
    settings: Settings | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    universe: str | None = None,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    """Join feature groups on (security_id, date); optionally restrict to a universe's members."""
    s = settings or get_settings()
    groups = groups or [g for g in registry() if group_dir(s, g).exists()]
    lf: pl.LazyFrame | None = None
    for g in groups:
        d = group_dir(s, g)
        if not d.exists():
            raise RuntimeError(f"feature group {g} not built (run `ca features build {g}`)")
        g_lf = pl.scan_parquet(str(d / "year=*" / "data.parquet"), hive_partitioning=True).drop("year")
        if start:
            g_lf = g_lf.filter(pl.col("date") >= start)
        if end:
            g_lf = g_lf.filter(pl.col("date") <= end)
        lf = g_lf if lf is None else lf.join(g_lf, on=["security_id", "date"], how="full", coalesce=True)
    assert lf is not None
    if universe:
        from collective_alpha.universe.universes import load_universe

        u = load_universe(s, universe).select("security_id", "date").lazy()
        lf = lf.join(u, on=["security_id", "date"], how="inner")
    if columns:
        lf = lf.select(["security_id", "date", *[c for c in columns if c not in ("security_id", "date")]])
    return lf.collect()


def list_features(settings: Settings | None = None) -> dict[str, list[str]]:
    s = settings or get_settings()
    out = {}
    for g in registry():
        d = group_dir(s, g)
        files = sorted(d.glob("year=*/data.parquet"))
        if files:
            out[g] = [c for c in pl.read_parquet_schema(files[-1]) if c not in ("security_id", "date")]
    return out
