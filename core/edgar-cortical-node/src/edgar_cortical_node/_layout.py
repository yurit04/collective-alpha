"""On-disk layout of the EDGAR data root (default ~/market_data/edgar).

facts/bucket=NN/part.parquet   every numeric XBRL fact, CIKs split into BUCKETS by cik % BUCKETS
companies.parquet              cik, name, facts, last_filed, fetched_at
profiles.parquet               company profiles (SIC, fiscal year end, tickers, ...) from submissions
names.parquet                  company name history: cik, name, valid_from, valid_to
state.json                     bulk file date, how far the daily sync got, CIKs to retry
raw/companyfacts.zip           last bulk download (kept for re-parsing)
raw/submissions.zip            last profiles bulk download
_logs/
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BUCKETS = 64


def bucket_of(cik: str | int) -> int:
    return int(cik) % BUCKETS


class Layout:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    @property
    def facts_dir(self) -> Path:
        return self.root / "facts"

    def bucket_file(self, bucket: int, base: Path | None = None) -> Path:
        return (base or self.facts_dir) / f"bucket={bucket:02d}" / "part.parquet"

    @property
    def companies_file(self) -> Path:
        return self.root / "companies.parquet"

    @property
    def profiles_file(self) -> Path:
        return self.root / "profiles.parquet"

    @property
    def names_file(self) -> Path:
        return self.root / "names.parquet"

    @property
    def submissions_zip(self) -> Path:
        return self.root / "raw" / "submissions.zip"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def bulk_zip(self) -> Path:
        return self.root / "raw" / "companyfacts.zip"

    @property
    def logs_dir(self) -> Path:
        return self.root / "_logs"

    def ensure(self) -> None:
        for p in (self.root, self.facts_dir, self.bulk_zip.parent, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)

    def read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}

    def write_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True, default=str))
        os.replace(tmp, self.state_file)
