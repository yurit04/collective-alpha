"""Build the intraday daily table from minute flat files, one session at a time.

Output: curated/intraday/year=YYYY/YYYY-MM-DD.parquet, one row per (security_id, date), keyed
through the security master like every other curated table. Incremental: a session whose file
already exists is skipped unless `force`.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.config import Settings, get_settings
from collective_alpha.intraday.aggregate import aggregate_session
from collective_alpha.intraday.session import session_bounds
from collective_alpha.storage import layout
from collective_alpha.storage.parquet import write_parquet_atomic
from collective_alpha.universe.security_master import map_to_security

log = logging.getLogger(__name__)

TABLE = "intraday"


def intraday_dir(s: Settings) -> Path:
    return layout.curated_table_dir(s, TABLE)


def session_path(s: Settings, d: dt.date) -> Path:
    return intraday_dir(s) / f"year={d:%Y}" / f"{d:%Y-%m-%d}.parquet"


def minute_path(s: Settings, d: dt.date) -> Path:
    return layout.curated_bars_path(s, "minute_aggs_v1", d)


def earliest_minute_session(s: Settings) -> dt.date | None:
    files = sorted((layout.curated_table_dir(s, "minute_aggs")).glob("year=*/month=*/*.parquet"))
    return dt.date.fromisoformat(files[0].stem) if files else None


def build_session(s: Settings, d: dt.date, master: pl.DataFrame, force: bool = False) -> dict:
    """Aggregate one session and write its Parquet file. Returns a small status dict."""
    out_path = session_path(s, d)
    if out_path.exists() and not force:
        return {"date": d, "status": "skipped"}
    b = session_bounds(d)
    if b is None:
        return {"date": d, "status": "not_a_session"}
    src = minute_path(s, d)
    if not src.exists():
        return {"date": d, "status": "no_minute_file"}
    bars = pl.read_parquet(src, columns=["ticker", "ts_ny", "open", "high", "low", "close", "volume", "transactions"])
    agg = aggregate_session(bars, b)
    if agg.height == 0:
        return {"date": d, "status": "empty"}
    mapped = map_to_security(agg, master).filter(pl.col("security_id").is_not_null())
    # concurrent when-issued lines resolve to one security: keep the more active line
    mapped = (
        mapped.sort(["security_id", "volume_reg"], descending=[False, True])
        .unique(subset=["security_id", "date"], keep="first", maintain_order=True)
        .select(["security_id", *[c for c in agg.columns if c != "date"], "date"])
        .sort("security_id")
    )
    n = write_parquet_atomic(mapped, out_path)
    return {"date": d, "status": "built", "rows": n, "tickers": agg.height}


def build(
    settings: Settings | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    force: bool = False,
    workers: int | None = None,
) -> dict:
    s = settings or get_settings()
    from collective_alpha.universe.builder import load_master

    master = load_master(s)
    # Default to whatever minute data exists, not a rolling window: a rolling default silently
    # leaves the oldest sessions on an older schema when the table is rebuilt.
    start = (
        start or earliest_minute_session(s) or (dt.date.today().replace(year=dt.date.today().year - s.history_years))
    )
    end = end or dt.date.today()
    days = trading_days(start, end)
    workers = workers or s.max_workers
    counts: dict[str, int] = {}
    rows = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(build_session, s, d, master, force): d for d in days}
        for f in as_completed(futs):
            d = futs[f]
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                log.error("%s failed: %s", d, e)
                counts["error"] = counts.get("error", 0) + 1
                continue
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            rows += int(r.get("rows", 0))
            done += 1
            if done % 100 == 0:
                log.info("intraday: %d/%d sessions", done, len(days))
    return {"sessions": len(days), "rows_written": rows, **counts}


def load_intraday(
    settings: Settings | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    columns: list[str] | None = None,
    universe: str | None = None,
) -> pl.DataFrame:
    s = settings or get_settings()
    lf = pl.scan_parquet(str(intraday_dir(s) / "year=*" / "*.parquet"), hive_partitioning=True)
    if "year" in lf.collect_schema().names():
        lf = lf.drop("year")
    if start:
        lf = lf.filter(pl.col("date") >= start)
    if end:
        lf = lf.filter(pl.col("date") <= end)
    if universe:
        from collective_alpha.universe.universes import load_universe

        u = load_universe(universe, s).select("security_id", "date").lazy()
        lf = lf.join(u, on=["security_id", "date"], how="inner")
    if columns:
        lf = lf.select(["security_id", "date", *[c for c in columns if c not in ("security_id", "date")]])
    return lf.collect()


def coverage(settings: Settings | None = None) -> pl.DataFrame:
    """Sessions present versus expected, for `ca intraday check`."""
    s = settings or get_settings()
    files = sorted(intraday_dir(s).glob("year=*/*.parquet"))
    have = {dt.date.fromisoformat(p.stem) for p in files}
    if not have:
        return pl.DataFrame()
    expected = trading_days(min(have), max(have))
    missing = [d for d in expected if d not in have]
    return pl.DataFrame(
        {
            "first": [min(have)],
            "last": [max(have)],
            "sessions": [len(have)],
            "expected": [len(expected)],
            "missing": [len(missing)],
            "missing_sample": [", ".join(str(d) for d in missing[:5])],
        }
    )
