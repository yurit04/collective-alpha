"""Path conventions under data_root.

raw/flatfiles/<s3 key>                          immutable vendor files (csv.gz)
raw/rest/<table>/<partition>.json.gz             immutable REST responses (all pages concatenated)
curated/<table>/<hive partitions>/<file>.parquet typed Parquet, what research code reads
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from collective_alpha.config import Settings

FLATFILE_PREFIX = "us_stocks_sip"


def flatfile_key(dataset: str, day: dt.date) -> str:
    return f"{FLATFILE_PREFIX}/{dataset}/{day:%Y}/{day:%m}/{day:%Y-%m-%d}.csv.gz"


def date_from_flatfile_key(key: str) -> dt.date:
    return dt.date.fromisoformat(Path(key).name.removesuffix(".csv.gz"))


def dataset_from_flatfile_key(key: str) -> str:
    return key.split("/")[1]


def raw_flatfile_path(s: Settings, key: str) -> Path:
    return s.raw_dir / "flatfiles" / key


def raw_rest_path(s: Settings, table: str, partition: str) -> Path:
    return s.raw_dir / "rest" / table / f"{partition}.json.gz"


def curated_table_dir(s: Settings, table: str) -> Path:
    return s.curated_dir / table


def curated_bars_path(s: Settings, dataset: str, day: dt.date) -> Path:
    """day_aggs -> curated/day_aggs/year=YYYY/YYYY-MM-DD.parquet
    minute_aggs -> curated/minute_aggs/year=YYYY/month=MM/YYYY-MM-DD.parquet"""
    table = dataset.removesuffix("_v1")
    base = curated_table_dir(s, table) / f"year={day:%Y}"
    if table != "day_aggs":
        base = base / f"month={day:%m}"
    return base / f"{day:%Y-%m-%d}.parquet"


def curated_snapshot_path(s: Settings, table: str, asof: dt.date) -> Path:
    return curated_table_dir(s, table) / f"asof={asof:%Y-%m-%d}" / "data.parquet"


def curated_month_path(s: Settings, table: str, month: dt.date) -> Path:
    return curated_table_dir(s, table) / f"year={month:%Y}" / f"month={month:%m}" / "data.parquet"
