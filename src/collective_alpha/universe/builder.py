"""Build and persist the security master from the curated store."""

from __future__ import annotations

import datetime as dt
import logging

import polars as pl

from collective_alpha.config import Settings, get_settings
from collective_alpha.storage import layout
from collective_alpha.storage.parquet import write_parquet_atomic
from collective_alpha.universe.security_master import (
    assign_identities,
    check_master,
    securities_from_master,
    trading_episodes,
)

log = logging.getLogger(__name__)


def _latest_snapshot(s: Settings, table: str) -> pl.DataFrame:
    d = layout.curated_table_dir(s, table)
    parts = sorted(p for p in d.glob("asof=*") if p.is_dir())
    if not parts:
        raise RuntimeError(f"no curated snapshots for {table}")
    return pl.read_parquet(parts[-1] / "data.parquet")


def _all_snapshots(s: Settings, table: str) -> pl.DataFrame:
    d = layout.curated_table_dir(s, table)
    files = sorted(d.glob("asof=*/data.parquet"))
    if not files:
        raise RuntimeError(f"no curated snapshots for {table}; run `ca sync tickers-pit`")
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")


def reuse_candidates(episodes: pl.DataFrame, ticker_events: pl.DataFrame | None) -> set[str]:
    """Tickers whose identity at episode edges is worth verifying with a dated lookup:
    those with more than one trading episode, and old symbols named in ticker-change events."""
    multi = set(episodes.group_by("ticker").len().filter(pl.col("len") > 1)["ticker"].to_list())
    old: set[str] = set()
    if ticker_events is not None and "ticker_change.ticker" in ticker_events.columns:
        ev = ticker_events.filter(pl.col("ticker_change.ticker") != pl.col("ticker"))
        old = set(ev["ticker_change.ticker"].drop_nulls().to_list())
    present = set(episodes["ticker"].to_list())
    return (multi | old) & present


def build_security_master(
    provider,
    asof: dt.date | None = None,
    gap_days: int = 30,
) -> dict[str, int]:
    s: Settings = provider.s
    asof = asof or dt.date.today()

    bars = pl.scan_parquet(str(s.curated_dir / "day_aggs" / "**" / "*.parquet"), hive_partitioning=True)
    episodes = trading_episodes(bars, gap_days=gap_days)
    log.info("episodes: %d over %d tickers", episodes.height, episodes["ticker"].n_unique())

    dates_df = bars.select("ticker", "date").unique().sort(["ticker", "date"]).collect()
    bar_dates = {t: g["date"].to_list() for t, g in dates_df.group_by("ticker", maintain_order=True)}
    bar_dates = {k[0] if isinstance(k, tuple) else k: v for k, v in bar_dates.items()}

    snapshots = _all_snapshots(s, "tickers_pit")
    log.info("pit snapshots: %d rows over %d dates", snapshots.height, snapshots["asof"].n_unique())

    try:
        events = _latest_snapshot(s, "ticker_events")
    except RuntimeError:
        events = None
    cands = reuse_candidates(episodes, events)
    log.info("reuse candidates: %d tickers", len(cands))

    calls = {"n": 0}

    def lookup(ticker: str, d: dt.date):
        calls["n"] += 1
        return provider.ticker_details_pit(ticker, d)

    master = assign_identities(episodes, bar_dates, snapshots, lookup, cands, workers=s.max_workers)
    log.info("dated lookups used: %d", calls["n"])

    current = _latest_snapshot(s, "tickers")
    securities = securities_from_master(master, current)

    master = master.with_columns(asof=pl.lit(asof))
    securities = securities.with_columns(asof=pl.lit(asof))
    write_parquet_atomic(master, layout.curated_snapshot_path(s, "security_master", asof))
    write_parquet_atomic(securities, layout.curated_snapshot_path(s, "securities", asof))

    report = check_master(master, episodes)
    report["lookups"] = calls["n"]
    return report


def load_master(settings: Settings | None = None) -> pl.DataFrame:
    return _latest_snapshot(settings or get_settings(), "security_master")


def load_securities(settings: Settings | None = None) -> pl.DataFrame:
    return _latest_snapshot(settings or get_settings(), "securities")
