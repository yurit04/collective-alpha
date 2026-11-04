"""Append-only record of order sheets, submitted orders, fills and events under the data root.
Never edited by hand; nothing in it is ever deleted by this package."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from ._broker import FILLS, ORDERS

SUBMITTED = {**ORDERS, "sheet_id": pl.String, "account": pl.String, "submitted_at": pl.Datetime("us", "UTC")}


class Journal:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.sheets = self.root / "sheets"
        self.orders_path = self.root / "orders.parquet"
        self.fills_path = self.root / "fills.parquet"
        self.events_path = self.root / "events.jsonl"

    def _write(self, df: pl.DataFrame, path: Path) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(path)

    def event(self, kind: str, **detail) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a") as f:
            f.write(json.dumps({"time": datetime.now(UTC).isoformat(), "event": kind, **detail}, default=str) + "\n")

    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines() if line.strip()]

    def submitted(self) -> pl.DataFrame:
        return pl.read_parquet(self.orders_path) if self.orders_path.exists() else pl.DataFrame(schema=SUBMITTED)

    def record_orders(self, placed: pl.DataFrame, sheet_id: str, account: str) -> None:
        rows = placed.with_columns(pl.lit(sheet_id).alias("sheet_id"), pl.lit(account).alias("account"),
                                   pl.lit(datetime.now(UTC)).cast(SUBMITTED["submitted_at"]).alias("submitted_at"))
        self._write(pl.concat([self.submitted(), rows.select(list(SUBMITTED)).cast(SUBMITTED)]), self.orders_path)

    def fills(self) -> pl.DataFrame:
        return pl.read_parquet(self.fills_path) if self.fills_path.exists() else pl.DataFrame(schema=FILLS)

    def record_fills(self, fills: pl.DataFrame) -> int:
        """Add executions not seen before (by exec_id). Returns how many were new."""
        old = self.fills()
        new = fills.cast(FILLS).filter(~pl.col("exec_id").is_in(old["exec_id"].to_list()))
        if new.height:
            self._write(pl.concat([old, new.select(list(FILLS))]).sort("time"), self.fills_path)
        return new.height

    def traded_con_ids(self) -> set[int]:
        """Contracts this package has ever ordered: their positions are managed."""
        return set(self.submitted()["con_id"].drop_nulls().to_list())
