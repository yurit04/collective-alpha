"""On-disk layout of the data root. Every path the package writes comes from here."""

from __future__ import annotations

from datetime import date
from pathlib import Path


class Layout:
    """Paths under the data root (default `~/market_data/massive`).

    bars/day/year=YYYY/part.parquet
    bars/minute/date=YYYY-MM-DD/part.parquet
    ticks/{trades,quotes}/date=YYYY-MM-DD/part.parquet
    <rest dataset path>/part.parquet            (snapshot datasets)
    <rest dataset path>/year=YYYY/part.parquet  (time-series datasets)
    live/{bars_1m,bars_1s,trades,quotes,luld}/date=YYYY-MM-DD/*.parquet
    identity/snapshots/year=YYYY/part.parquet  (month-end point-in-time reference)
    identity/{lookups,security_master,securities,security_attributes,splits,dividends}.parquet
    raw/flatfiles/<s3 key>                      (downloads, deleted after conversion by default)
    _catalog/catalog.sqlite  _logs/  _run/
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser()

    # Flat-file datasets
    def bars_day_file(self, year: int) -> Path:
        return self.root / "bars" / "day" / f"year={year}" / "part.parquet"

    def bars_minute_file(self, day: date) -> Path:
        return self.root / "bars" / "minute" / f"date={day.isoformat()}" / "part.parquet"

    def ticks_file(self, kind: str, day: date) -> Path:
        return self.root / "ticks" / kind / f"date={day.isoformat()}" / "part.parquet"

    def raw_file(self, key: str) -> Path:
        return self.root / "raw" / "flatfiles" / key

    # REST datasets
    def rest_file(self, rel_path: str, year: int | None = None) -> Path:
        base = self.root / rel_path
        return (base / f"year={year}" / "part.parquet") if year is not None else (base / "part.parquet")

    def rest_dir(self, rel_path: str) -> Path:
        return self.root / rel_path

    # Live stream
    def live_dir(self, kind: str, day: date | None = None) -> Path:
        base = self.root / "live" / kind
        return base / f"date={day.isoformat()}" if day else base

    # Security master (identity layer)
    @property
    def identity_dir(self) -> Path:
        return self.root / "identity"

    def snapshot_file(self, year: int) -> Path:
        return self.identity_dir / "snapshots" / f"year={year}" / "part.parquet"

    def identity_file(self, name: str) -> Path:
        """lookups, security_master, securities, security_attributes, splits, dividends."""
        return self.identity_dir / f"{name}.parquet"

    # Bookkeeping
    @property
    def catalog_path(self) -> Path:
        return self.root / "_catalog" / "catalog.sqlite"

    @property
    def logs_dir(self) -> Path:
        return self.root / "_logs"

    @property
    def run_dir(self) -> Path:
        return self.root / "_run"

    def ensure(self) -> None:
        for path in (self.root, self.catalog_path.parent, self.logs_dir, self.run_dir):
            path.mkdir(parents=True, exist_ok=True)
