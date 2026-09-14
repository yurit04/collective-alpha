"""SQLite ledger of everything fetched and converted. Makes every sync idempotent and resumable."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    key           TEXT PRIMARY KEY,   -- s3 key or rest/<table>/<partition>
    source        TEXT NOT NULL,      -- 'flatfile' | 'rest'
    dataset       TEXT NOT NULL,
    partition     TEXT,               -- date / asof / month
    size_bytes    INTEGER,
    etag          TEXT,
    downloaded_at TEXT,
    converted_at  TEXT,
    curated_path  TEXT,
    row_count     INTEGER,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS files_dataset_idx ON files(dataset, partition);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    detail      TEXT
);
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class Manifest:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # files ---------------------------------------------------------------
    def get(self, key: str) -> sqlite3.Row | None:
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute("SELECT * FROM files WHERE key=?", (key,)).fetchone()
        self._conn.row_factory = None
        return row

    def is_downloaded(self, key: str, size: int | None = None, etag: str | None = None) -> bool:
        row = self.get(key)
        if row is None or row["downloaded_at"] is None:
            return False
        if size is not None and row["size_bytes"] != size:
            return False
        if etag is not None and row["etag"] and row["etag"] != etag:
            return False
        return True

    def is_converted(self, key: str) -> bool:
        row = self.get(key)
        return row is not None and row["converted_at"] is not None

    def mark_downloaded(
        self, key: str, source: str, dataset: str, partition: str | None, size: int, etag: str | None
    ) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO files(key, source, dataset, partition, size_bytes, etag, downloaded_at, error)
                   VALUES (?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(key) DO UPDATE SET size_bytes=excluded.size_bytes, etag=excluded.etag,
                     downloaded_at=excluded.downloaded_at, converted_at=NULL, curated_path=NULL, error=NULL""",
                (key, source, dataset, partition, size, etag, _now()),
            )

    def mark_converted(self, key: str, curated_path: Path, row_count: int) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE files SET converted_at=?, curated_path=?, row_count=?, error=NULL WHERE key=?",
                (_now(), str(curated_path), row_count, key),
            )

    def mark_error(self, key: str, source: str, dataset: str, partition: str | None, err: str) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO files(key, source, dataset, partition, error) VALUES (?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET error=excluded.error""",
                (key, source, dataset, partition, err[:2000]),
            )

    def pending_conversion(self, dataset: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT key FROM files WHERE dataset=? AND downloaded_at IS NOT NULL AND converted_at IS NULL ORDER BY key",
            (dataset,),
        ).fetchall()
        return [r[0] for r in rows]

    def summary(self) -> list[tuple]:
        return self._conn.execute(
            """SELECT source, dataset, COUNT(*) AS files,
                      SUM(downloaded_at IS NOT NULL) AS downloaded,
                      SUM(converted_at IS NOT NULL) AS converted,
                      SUM(error IS NOT NULL) AS errors,
                      MIN(partition) AS first_partition, MAX(partition) AS last_partition,
                      ROUND(SUM(COALESCE(size_bytes,0))/1e9, 2) AS raw_gb,
                      SUM(COALESCE(row_count,0)) AS rows
               FROM files GROUP BY source, dataset ORDER BY source, dataset"""
        ).fetchall()

    def errors(self, limit: int = 50) -> list[tuple]:
        return self._conn.execute(
            "SELECT key, error FROM files WHERE error IS NOT NULL ORDER BY key LIMIT ?", (limit,)
        ).fetchall()

    # runs ----------------------------------------------------------------
    def start_run(self, command: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO runs(command, started_at) VALUES (?,?)", (command, _now()))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, detail: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?",
                (_now(), status, detail[:4000], run_id),
            )

    def close(self) -> None:
        self._conn.close()
