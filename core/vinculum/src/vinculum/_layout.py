"""On-disk layout of the vinculum data root (default ~/market_data/vinculum). Everything here
is rebuilt from the Massive and EDGAR stores by `vinculum build`; nothing is downloaded.

cik_map.parquet              CIK per security and validity window, with how it was decided
share_counts.parquet         SEC share counts per security, in today's units of the security
split_attribution.parquet    which of a security's splits apply to the SEC's counts
basis.parquet                basis ratio per security (traded units per SEC unit)
public_float.parquet         public float per company and 10-K, as a fraction of market cap
successors.parquet           same company under a new security or CIK (holding-company reorganizations)
market_cap/year=YYYY/part.parquet   one row per equity security and trading day
build.json                   when it was built, from which inputs, check summary
_logs/
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class Layout:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    def file(self, name: str) -> Path:
        return self.root / f"{name}.parquet"

    @property
    def market_cap_dir(self) -> Path:
        return self.root / "market_cap"

    def market_cap_file(self, year: int, base: Path | None = None) -> Path:
        return (base or self.market_cap_dir) / f"year={year}" / "part.parquet"

    def market_cap_files(self) -> list[Path]:
        return sorted(self.market_cap_dir.glob("year=*/part.parquet"))

    @property
    def build_file(self) -> Path:
        return self.root / "build.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "_logs"

    def ensure(self) -> None:
        for p in (self.root, self.market_cap_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)

    def read_build(self) -> dict[str, Any]:
        try:
            return json.loads(self.build_file.read_text())
        except FileNotFoundError:
            return {}

    def write_build(self, info: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.build_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(info, indent=1, sort_keys=True, default=str))
        os.replace(tmp, self.build_file)


def write_atomic(df, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        df.write_parquet(tmp, compression="zstd", compression_level=3, statistics=True, row_group_size=256_000)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path.stat().st_size
