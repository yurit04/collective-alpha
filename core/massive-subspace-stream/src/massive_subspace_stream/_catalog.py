"""SQLite record of what has been downloaded, so every job is resumable and gaps are visible."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS partitions (
    dataset    TEXT NOT NULL,
    partition  TEXT NOT NULL,          -- usually an ISO date, or 'all'
    rows       INTEGER NOT NULL,
    bytes      INTEGER NOT NULL,
    detail     TEXT,                   -- JSON, e.g. {"tickers": ["AAPL"]} for partial tick days
    source     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset, partition)
);
CREATE TABLE IF NOT EXISTS state (
    dataset    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset, key)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Catalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con, con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=60)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # Partitions
    def mark(
        self,
        dataset: str,
        partition: str,
        rows: int,
        nbytes: int,
        detail: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> None:
        with closing(self._connect()) as con, con:
            con.execute(
                "INSERT OR REPLACE INTO partitions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (dataset, partition, int(rows), int(nbytes), json.dumps(detail) if detail else None, source, _now()),
            )

    def mark_many(self, dataset: str, items: list[tuple[str, int, int]], source: str | None = None) -> None:
        now = _now()
        with closing(self._connect()) as con, con:
            con.executemany(
                "INSERT OR REPLACE INTO partitions VALUES (?, ?, ?, ?, NULL, ?, ?)",
                [(dataset, p, int(r), int(b), source, now) for p, r, b in items],
            )

    def get(self, dataset: str, partition: str) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM partitions WHERE dataset = ? AND partition = ?", (dataset, partition)
            ).fetchone()
        return _row(row) if row else None

    def partitions(self, dataset: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM partitions WHERE dataset = ?", [dataset]
        if start:
            sql, args = sql + " AND partition >= ?", [*args, start]
        if end:
            sql, args = sql + " AND partition <= ?", [*args, end]
        with closing(self._connect()) as con:
            return [_row(r) for r in con.execute(sql + " ORDER BY partition", args)]

    def partition_keys(self, dataset: str, start: str | None = None, end: str | None = None) -> list[str]:
        return [p["partition"] for p in self.partitions(dataset, start, end)]

    def remove(self, dataset: str, partition: str) -> None:
        with closing(self._connect()) as con, con:
            con.execute("DELETE FROM partitions WHERE dataset = ? AND partition = ?", (dataset, partition))

    def summary(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            rows = con.execute(
                """SELECT dataset, COUNT(*) AS partitions, MIN(partition) AS first, MAX(partition) AS last,
                          SUM(rows) AS rows, SUM(bytes) AS bytes, MAX(updated_at) AS updated_at
                   FROM partitions GROUP BY dataset ORDER BY dataset"""
            ).fetchall()
        return [dict(r) for r in rows]

    # Free-form state (cursors, last refresh times)
    def set_state(self, dataset: str, key: str, value: str) -> None:
        with closing(self._connect()) as con, con:
            con.execute("INSERT OR REPLACE INTO state VALUES (?, ?, ?, ?)", (dataset, key, str(value), _now()))

    def get_state(self, dataset: str, key: str) -> str | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT value FROM state WHERE dataset = ? AND key = ?", (dataset, key)).fetchone()
        return row["value"] if row else None

    def all_state(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM state ORDER BY dataset, key")]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["detail"] = json.loads(out["detail"]) if out.get("detail") else None
    return out
