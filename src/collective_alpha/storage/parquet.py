"""Atomic Parquet writes with zstd compression."""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl


def write_parquet_atomic(df: pl.DataFrame, path: Path, compression: str = "zstd") -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        df.write_parquet(tmp, compression=compression, statistics=True)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return df.height
