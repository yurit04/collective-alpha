"""Raw vendor files -> typed curated Parquet."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import polars as pl

from collective_alpha.data.massive.datasets import FLATFILE_DATASETS, FlatFileDataset
from collective_alpha.storage.parquet import write_parquet_atomic

log = logging.getLogger(__name__)

NY = "America/New_York"


def read_flatfile(path: Path, spec: FlatFileDataset) -> pl.DataFrame:
    df = pl.read_csv(path, schema_overrides=spec.schema, infer_schema_length=10_000)
    missing = [c for c in spec.schema if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name}: missing expected columns {missing}; got {df.columns}")
    return df


def convert_bars(path: Path, spec: FlatFileDataset, day: dt.date, dest: Path) -> int:
    """Aggregates: add `ts` (UTC datetime), `ts_ny` (exchange-local naive), `date`; sort; write."""
    df = read_flatfile(path, spec)
    ts_utc = pl.from_epoch(pl.col(spec.ts_column), time_unit="ns").dt.replace_time_zone("UTC")
    df = (
        df.with_columns(
            ts=ts_utc,
            ts_ny=ts_utc.dt.convert_time_zone(NY).dt.replace_time_zone(None),
            date=pl.lit(day),
        )
        .drop(spec.ts_column)
        .sort([("ts" if c == spec.ts_column else c) for c in spec.sort_by])
    )
    if spec.table == "day_aggs":
        bad = df.filter(pl.col("ts").dt.date() != day).height
        if bad:
            log.warning("%s: %d rows whose window_start is not %s", path.name, bad, day)
    df = df.select(
        ["ticker", "date", "ts", "ts_ny", "open", "high", "low", "close", "volume", "transactions"]
        + [
            c
            for c in df.columns
            if c not in {"ticker", "date", "ts", "ts_ny", "open", "high", "low", "close", "volume", "transactions"}
        ]
    )
    return write_parquet_atomic(df, dest)


def convert_ticks(path: Path, spec: FlatFileDataset, day: dt.date, dest: Path) -> int:
    df = read_flatfile(path, spec)
    ts_utc = pl.from_epoch(pl.col(spec.ts_column), time_unit="ns").dt.replace_time_zone("UTC")
    df = df.with_columns(ts=ts_utc, date=pl.lit(day)).sort(list(spec.sort_by))
    return write_parquet_atomic(df, dest)


def convert_flatfile(path: Path, dataset: str, day: dt.date, dest: Path) -> int:
    spec = FLATFILE_DATASETS[dataset]
    if spec.table in ("day_aggs", "minute_aggs"):
        return convert_bars(path, spec, day, dest)
    return convert_ticks(path, spec, day, dest)


# ---------------------------------------------------------------- REST json -> parquet


def _json_safe(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        import json

        return json.dumps(v, separators=(",", ":"))
    return v


def records_to_frame(records: list[dict[str, Any]], asof: dt.date | None = None) -> pl.DataFrame:
    """Flatten nested dicts one level (a.b), serialise deeper structures to JSON strings."""
    rows: list[dict[str, Any]] = []
    for r in records:
        flat: dict[str, Any] = {}
        for k, v in r.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    flat[f"{k}.{k2}"] = _json_safe(v2)
            else:
                flat[k] = _json_safe(v)
        rows.append(flat)
    if not rows:
        return pl.DataFrame()
    df = pl.from_dicts(rows, infer_schema_length=None)
    if asof is not None:
        df = df.with_columns(asof=pl.lit(asof))
    return df
