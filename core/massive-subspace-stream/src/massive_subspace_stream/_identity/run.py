"""Run the identity build: fetch what is missing, segment, link, map corporate actions, write."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import polars as pl

from .._catalog import Catalog
from .._layout import Layout
from .._sessions import trading_days
from .._storage import upsert_parquet, write_parquet
from .actions import (
    check_splits_against_prices,
    mapped_dividends,
    mapped_splits,
    read_stock_dividends,
)
from .build import (
    BarIndex,
    anchors,
    attributes_table,
    episodes,
    link_and_assign,
    map_to_security,
    master_table,
    securities_table,
    segment_all,
    segment_attributes,
    split_ratios,
)
from .fetch import LOOKUP_SCHEMA, SNAPSHOT_SCHEMA, fetch_lookups, fetch_snapshot, month_ends

log = logging.getLogger("massive_subspace_stream.identity")

SNAPSHOT_DATASET = "ticker_snapshots"
FIRST_SNAPSHOT = date(2003, 9, 10)
OVERRIDES_SCHEMA = {"action": pl.String, "ticker": pl.String, "date": pl.Date, "other_ticker": pl.String,
                    "other_date": pl.Date, "split_from": pl.Float64, "split_to": pl.Float64, "note": pl.String}
OUTPUTS = ("security_master", "securities", "security_attributes", "splits", "dividends")


def load_overrides(path: Path | None = None) -> pl.DataFrame:
    if path is None:
        with resources.as_file(resources.files(__package__) / "overrides.csv") as p:
            return pl.read_csv(p, schema=OVERRIDES_SCHEMA)
    return pl.read_csv(path, schema=OVERRIDES_SCHEMA)


def snapshot_dates(end: date) -> list[date]:
    """Last trading day of every completed month up to `end`."""
    ends = month_ends(trading_days(FIRST_SNAPSHOT, end + timedelta(days=40)))
    return [d for d in ends if d <= end]


def ensure_snapshots(layout: Layout, catalog: Catalog, client, end: date, workers: int = 4) -> int:
    """Download month-end snapshots that are not stored yet. Returns how many were fetched."""
    have = set(catalog.partition_keys(SNAPSHOT_DATASET))
    todo = [d for d in snapshot_dates(end) if d.isoformat() not in have]
    if not todo:
        return 0
    log.info("snapshots: %d month-end(s) to fetch (%s .. %s)", len(todo), todo[0], todo[-1])
    by_year: dict[int, list[date]] = {}
    for d in todo:
        by_year.setdefault(d.year, []).append(d)
    for year, days in sorted(by_year.items()):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            frames = list(pool.map(lambda d: fetch_snapshot(client, d), days))
        df = pl.concat(frames, how="vertical_relaxed") if frames else pl.DataFrame(schema=SNAPSHOT_SCHEMA)
        path = layout.snapshot_file(year)
        _, nbytes = upsert_parquet(path, df, ["as_of", "ticker"], ["as_of", "ticker"])
        catalog.mark_many(SNAPSHOT_DATASET, [(d.isoformat(), f.height, 0) for d, f in zip(days, frames)],
                          source="/v3/reference/tickers?date=")
        log.info("snapshots %d: %d month(s), %s rows", year, len(days), f"{df.height:,}")
    return len(todo)


def _read(paths: list[Path], schema: dict | None = None) -> pl.DataFrame:
    paths = [p for p in paths if p.exists()]
    if not paths:
        return pl.DataFrame(schema=schema) if schema else pl.DataFrame()
    return pl.read_parquet(paths) if len(paths) > 1 else pl.read_parquet(paths[0])


def load_inputs(layout: Layout) -> dict[str, pl.DataFrame]:
    day_files = sorted((layout.root / "bars" / "day").glob("year=*/part.parquet"))
    if not day_files:
        raise FileNotFoundError("no daily bars yet. Run: subspace backfill history --dataset bars_day")
    bars = pl.scan_parquet(day_files).select("ticker", "date", "open", "close").collect()
    return {
        "bars": bars,
        "snapshots": _read(sorted((layout.identity_dir / "snapshots").glob("year=*/part.parquet")), SNAPSHOT_SCHEMA),
        "lookups": _read([layout.identity_file("lookups")], LOOKUP_SCHEMA),
        "splits": _read([layout.rest_file("corporate_actions/splits")]),
        "dividends": _read([layout.rest_file("corporate_actions/dividends")]),
    }


def build_identity(layout: Layout, catalog: Catalog, client=None, fetch: bool = True,
                   lookup_workers: int = 8, max_rounds: int = 12,
                   overrides: pl.DataFrame | None = None, lookups_from: date | None = None) -> dict[str, Any]:
    """Build (or rebuild) the security master. With `client` and fetch=True, missing snapshots
    and dated lookups are downloaded first; without, it builds from what is stored.
    `lookups_from`: the earliest date the plan can look up (older ones are not asked for)."""
    started = datetime.now(UTC)
    layout.ensure()
    data = load_inputs(layout)
    end = data["bars"]["date"].max()
    fetched_snapshots = 0
    if client is not None and fetch:
        fetched_snapshots = ensure_snapshots(layout, catalog, client, end)
        if fetched_snapshots:
            data["snapshots"] = _read(sorted((layout.identity_dir / "snapshots").glob("year=*/part.parquet")),
                                      SNAPSHOT_SCHEMA)
    overrides = load_overrides() if overrides is None else overrides
    forced: dict[str, list[date]] = {}
    for t, d in overrides.filter(pl.col("action") == "break").select("ticker", "date").iter_rows():
        forced.setdefault(t, []).append(d)

    bars = data["bars"]
    eps = episodes(bars)
    index = BarIndex.from_bars(bars)
    ratios = split_ratios(data["splits"]) if data["splits"].height else {}
    lookups = data["lookups"]
    lookups_fetched = 0
    for round_no in range(1, max_rounds + 1):
        anc = anchors(data["snapshots"], lookups, eps)
        seg = segment_all(index, eps, anc, lookups, ratios, forced)
        needed = [(t, d) for t, d in seg.needed if lookups_from is None or d >= lookups_from]
        if len(needed) < len(seg.needed):
            log.info("identity round %d: %d lookup(s) older than %s are outside the plan; not fetched",
                     round_no, len(seg.needed) - len(needed), lookups_from)
        if not needed or client is None or not fetch:
            break
        log.info("identity round %d: %d dated lookup(s) needed", round_no, len(needed))
        new = fetch_lookups(client, needed, lookup_workers)
        if new.height == 0:
            break
        lookups_fetched += new.height
        lookups = pl.concat([lookups, new], how="vertical_relaxed").unique(["ticker", "as_of"], keep="last")
        write_parquet(lookups.sort("ticker", "as_of"), layout.identity_file("lookups"))

    anc = anchors(data["snapshots"], lookups, eps)
    final = segment_all(index, eps, anc, lookups, ratios, forced, final=True)
    segs = segment_attributes(final.segments, anc)
    segs, link_stats = link_and_assign(segs, index, ratios, overrides)

    master = master_table(segs, index.end_day)
    securities = securities_table(master)
    attrs = attributes_table(data["snapshots"], master)
    closes = map_to_security(bars.lazy().select("ticker", "date", "close"), master)
    splits = mapped_splits(data["splits"], master, securities, overrides)
    splits = read_stock_dividends(splits, closes)
    n_splits = splits.height
    splits = check_splits_against_prices(splits, closes)
    dividends = mapped_dividends(data["dividends"], master, securities, closes) if data["dividends"].height \
        else pl.DataFrame()

    outputs = {"security_master": master, "securities": securities, "security_attributes": attrs,
               "splits": splits, "dividends": dividends}
    total = 0
    for name, df in outputs.items():
        total += write_parquet(df, layout.identity_file(name))
    summary = {
        "built_at": started.isoformat(timespec="seconds"),
        "data_end": end.isoformat(),
        "episodes": eps.height,
        "segments": master.height,
        "securities": securities.height,
        "snapshots_fetched": fetched_snapshots,
        "lookups_fetched": lookups_fetched,
        "lookups_needed_left": len(final.needed),
        "boundaries_from_prices": final.unresolved_boundaries,
        "links": link_stats,
        "splits_dropped_by_price": n_splits - splits.height,
        "seconds": round((datetime.now(UTC) - started).total_seconds(), 1),
    }
    catalog.mark("identity", "all", master.height, total, source="build")
    catalog.set_state("identity", "last_build", json.dumps(summary))
    return summary
