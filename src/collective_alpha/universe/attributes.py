"""Point-in-time security attributes (type, exchange, cik, name) per monthly snapshot date,
keyed by security_id. Built from tickers_pit snapshots mapped through the security master."""

from __future__ import annotations

import polars as pl

from collective_alpha.config import Settings
from collective_alpha.storage import layout
from collective_alpha.storage.parquet import write_parquet_atomic
from collective_alpha.universe.security_master import map_to_security

ATTR_COLS = ["type", "primary_exchange", "cik", "name", "composite_figi", "share_class_figi"]


def build_security_attributes(settings: Settings, master: pl.DataFrame, snapshots: pl.DataFrame) -> pl.DataFrame:
    snap = snapshots.select(["ticker", "asof", *[c for c in ATTR_COLS if c in snapshots.columns]]).rename(
        {"asof": "date"}
    )
    mapped = map_to_security(snap, master).filter(pl.col("security_id").is_not_null())
    attrs = (
        mapped.rename({"date": "asof"})
        .sort(["security_id", "asof"])
        .unique(subset=["security_id", "asof"], keep="last")
        .select(["security_id", "asof", "ticker", *[c for c in ATTR_COLS if c in mapped.columns]])
    )
    write_parquet_atomic(attrs, attributes_path(settings))
    return attrs


def attributes_path(settings: Settings):
    return layout.curated_table_dir(settings, "security_attributes") / "data.parquet"


def load_security_attributes(settings: Settings) -> pl.DataFrame:
    return pl.read_parquet(attributes_path(settings))


def attributes_asof(attrs: pl.DataFrame, keys: pl.DataFrame, date_col: str = "date") -> pl.DataFrame:
    """For rows (security_id, date) attach the attributes from the latest snapshot on or before date."""
    a = attrs.sort(["security_id", "asof"])
    return keys.sort(["security_id", date_col]).join_asof(
        a, left_on=date_col, right_on="asof", by="security_id", strategy="backward", check_sortedness=False
    )
