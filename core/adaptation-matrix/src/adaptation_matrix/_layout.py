"""On-disk layout of the cost store (default ~/market_data/adaptation_matrix).

daily/date=YYYY-MM-DD/part.parquet    one row per ticker: spreads, impact, auctions, quality
minute/date=YYYY-MM-DD/part.parquet   one row per ticker and session minute: quotes and signed flow
trades/date=YYYY-MM-DD/part.parquet   trades matched to the NBBO, for the kept names
state.json                            per day: set, version, era, rows, sizes, timings
_work/                                downloads and converted tick files of the days in progress
_run/run.lock                         one run at a time
_logs/
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

TABLES = ("daily", "minute", "trades")


class BusyError(RuntimeError):
    """Another run holds the cost store's lock; nothing was done."""


class Layout:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    def file(self, table: str, day: date) -> Path:
        return self.root / table / f"date={day.isoformat()}" / "part.parquet"

    def files(self, table: str) -> list[Path]:
        return sorted((self.root / table).glob("date=*/part.parquet"))

    @property
    def work_dir(self) -> Path:
        return self.root / "_work"

    @property
    def logs_dir(self) -> Path:
        return self.root / "_logs"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    def ensure(self) -> None:
        for p in (self.root, self.work_dir, self.logs_dir, self.root / "_run", *(self.root / t for t in TABLES)):
            p.mkdir(parents=True, exist_ok=True)

    def read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}

    def write_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True, default=str))
        os.replace(tmp, self.state_file)

    def store_bytes(self) -> int:
        """Bytes of the stored tables (not the work folder)."""
        return sum(p.stat().st_size for t in TABLES for p in self.files(t))

    def free_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free

    @contextmanager
    def run_lock(self, job: str) -> Iterator[None]:
        (self.root / "_run").mkdir(parents=True, exist_ok=True)
        fh = open(self.root / "_run" / "run.lock", "a+")  # noqa: SIM115 - held across the yield
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            holder = fh.read().strip() or "another run"
            fh.close()
            raise BusyError(f"the cost store is busy ({holder}); try again when it has finished") from None
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{job}, pid {os.getpid()}\n")
            fh.flush()
            yield
        finally:
            fh.seek(0)
            fh.truncate()
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
