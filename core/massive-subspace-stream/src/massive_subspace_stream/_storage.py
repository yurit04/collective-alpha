"""Safe Parquet writes: write to a temp file in the same folder, then rename.

A crash mid-write never leaves a half-written file where readers look.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import polars as pl

COMPRESSION = "zstd"
COMPRESSION_LEVEL = 3
ROW_GROUP_SIZE = 128_000


def tmp_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def write_parquet(df: pl.DataFrame, path: Path, row_group_size: int = ROW_GROUP_SIZE) -> int:
    """Atomically write `df` to `path`. Returns the file size in bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)
    try:
        df.write_parquet(
            tmp,
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
            statistics=True,
            row_group_size=row_group_size,
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path.stat().st_size


def read_parquet_if_exists(path: Path) -> pl.DataFrame | None:
    return pl.read_parquet(path) if path.exists() else None


def upsert_parquet(path: Path, new: pl.DataFrame, keys: list[str], sort: list[str] | None = None) -> tuple[int, int]:
    """Merge `new` into the file at `path`, keeping the newest row per `keys`.

    Returns (rows in file, file size in bytes).
    """
    existing = read_parquet_if_exists(path)
    combined = new if existing is None else pl.concat([existing, new], how="diagonal_relaxed")
    if keys:
        combined = combined.unique(subset=keys, keep="last", maintain_order=True)
    if sort:
        combined = combined.sort([c for c in sort if c in combined.columns], nulls_last=True)
    return combined.height, write_parquet(combined, path)


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
