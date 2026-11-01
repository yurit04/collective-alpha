"""Download point-in-time reference data for the security master.

Snapshots: the full ticker list as of the last trading day of each month
(`/v3/reference/tickers?date=`), stored per year.
Lookups: one ticker as of one day (`/v3/reference/tickers/{ticker}?date=`), cached so a
rebuild never asks twice. "Not found" answers are cached too (`found = false`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from .._sources.rest import RestError

log = logging.getLogger("massive_subspace_stream.identity")

# Fields kept from Massive's ticker records, in storage order.
RECORD_FIELDS: dict[str, Any] = {
    "ticker": pl.String,
    "name": pl.String,
    "type": pl.String,
    "primary_exchange": pl.String,
    "cik": pl.String,
    "composite_figi": pl.String,
    "share_class_figi": pl.String,
    "active": pl.Boolean,
    "list_date": pl.String,
    "delisted_utc": pl.String,
}
SNAPSHOT_SCHEMA = {"as_of": pl.Date, **RECORD_FIELDS}
LOOKUP_SCHEMA = {"as_of": pl.Date, "found": pl.Boolean, **RECORD_FIELDS,
                 "fetched_at": pl.Datetime("us", "UTC")}


def month_ends(days: Iterable[date]) -> list[date]:
    """Last trading day of each month, from a sorted list of trading days."""
    out: dict[tuple[int, int], date] = {}
    for d in days:
        out[(d.year, d.month)] = d
    return sorted(out.values())


def _record(raw: dict[str, Any]) -> dict[str, Any]:
    rec = {k: raw.get(k) for k in RECORD_FIELDS}
    for k in ("cik", "composite_figi", "share_class_figi", "type", "primary_exchange", "name"):
        if rec[k] == "":
            rec[k] = None
    if rec["active"] is not None:
        rec["active"] = bool(rec["active"])
    for k in ("list_date", "delisted_utc"):
        if rec[k] is not None:
            rec[k] = str(rec[k])
    return rec


def fetch_snapshot(client, as_of: date) -> pl.DataFrame:
    """Every stock ticker Massive lists as of `as_of` (about 8-13k rows, 9-14 pages)."""
    rows = []
    for page in client.paginate("/v3/reference/tickers",
                                {"market": "stocks", "date": as_of.isoformat(), "limit": 1000}):
        rows += [{"as_of": as_of, **_record(r)} for r in page]
    df = pl.DataFrame(rows, schema=SNAPSHOT_SCHEMA, orient="row") if rows \
        else pl.DataFrame(schema=SNAPSHOT_SCHEMA)
    return df.unique(["as_of", "ticker"], keep="first").sort("ticker")


def fetch_lookup(client, ticker: str, as_of: date) -> dict[str, Any]:
    now = datetime.now(UTC)
    try:
        res = client.get(f"/v3/reference/tickers/{ticker}", {"date": as_of.isoformat()}).get("results")
    except RestError as exc:
        if exc.status == 404:
            res = None
        else:
            raise
    if not res:
        empty = dict.fromkeys(RECORD_FIELDS)
        return {"as_of": as_of, "found": False, **empty, "ticker": ticker, "fetched_at": now}
    rec = _record(res)
    rec["ticker"] = ticker  # keep the key we asked for
    return {"as_of": as_of, "found": True, **rec, "fetched_at": now}


def fetch_lookups(client, pairs: Iterable[tuple[str, date]], workers: int = 8) -> pl.DataFrame:
    """Dated lookups in parallel. Failures (other than 404) are logged and left out."""
    pairs = sorted(set(pairs))
    rows: list[dict[str, Any]] = []
    if not pairs:
        return pl.DataFrame(schema=LOOKUP_SCHEMA)
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_lookup, client, t, d): (t, d) for t, d in pairs}
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                rows.append(fut.result())
            except RestError as exc:
                failed += 1
                log.warning("lookup %s %s failed: %s", *futures[fut], exc)
            if n % 2000 == 0:
                log.info("lookups: %d/%d", n, len(pairs))
    if failed:
        log.warning("lookups: %d of %d failed (they will be retried next build)", failed, len(pairs))
    return pl.DataFrame(rows, schema=LOOKUP_SCHEMA, orient="row")
