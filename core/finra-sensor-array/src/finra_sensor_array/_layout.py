"""On-disk layout of the FINRA data root (default ~/market_data/finra).

short_interest/year=YYYY/part.parquet   one row per settlement date and symbol
short_volume/year=YYYY/part.parquet     one row per trading day and symbol
state.json                              settlement dates and days fetched (or known empty)
_logs/
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DATASETS = ("short_interest", "short_volume")


class Layout:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    def year_file(self, dataset: str, year: int) -> Path:
        return self.root / dataset / f"year={year}" / "part.parquet"

    def files(self, dataset: str) -> list[Path]:
        return sorted((self.root / dataset).glob("year=*/part.parquet"))

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "_logs"

    def ensure(self) -> None:
        for p in (self.root, self.logs_dir, *(self.root / d for d in DATASETS)):
            p.mkdir(parents=True, exist_ok=True)

    def read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}

    @contextmanager
    def state_lock(self) -> Iterator[None]:
        """Held around read-modify-write of state.json, so concurrent jobs (both backfills at
        once) never drop each other's progress."""
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".state.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def write_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
        os.replace(tmp, self.state_file)


def write_atomic(df, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        df.write_parquet(tmp, compression="zstd", compression_level=3, statistics=True, row_group_size=256_000)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path.stat().st_size
