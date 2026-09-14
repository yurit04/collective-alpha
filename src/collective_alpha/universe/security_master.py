"""Security master: map (ticker, date) -> stable security id.

Tickers are reused (FB: Meta until 2022-06-08, an ETF since 2025-06) and renamed (FB -> META),
so every point-in-time join must go through this table.

Inputs
------
* daily bars              -> *trading episodes*: runs of consecutive sessions per ticker, split on gaps
* tickers_pit snapshots   -> identity (FIGI / CIK / name / type) of a ticker *as of* monthly dates
* dated details lookup    -> identity on an arbitrary date, used to pin the boundary when the
                             identity changes inside one episode, or when an episode has no snapshot

Identity key precedence: composite_figi > share_class_figi > CIK:<cik>:<ticker> > SYM:<ticker>:<first date>
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

log = logging.getLogger(__name__)

Lookup = Callable[[str, dt.date], dict | None]

IDENTITY_COLS = ["name", "type", "cik", "composite_figi", "share_class_figi", "primary_exchange"]

# Exchange test symbols (ZVZZT, ZTEST, NTEST.A ...) never have a reference record.
TEST_TICKER_RE = re.compile(r"^(Z[A-Z]{2,5}|[A-Z]{1,2}TEST(\.[A-Z])?|CBO|CBX|IBO|ATEST[A-Z.]*)$")


@dataclass(frozen=True)
class Identity:
    security_id: str
    source: str  # figi | share_class_figi | cik | symbol | none
    name: str | None = None
    type: str | None = None
    cik: str | None = None
    composite_figi: str | None = None
    share_class_figi: str | None = None
    primary_exchange: str | None = None

    @property
    def known(self) -> bool:
        return self.source != "none"


def identity_from_record(rec: dict | None, ticker: str, start: dt.date) -> Identity:
    if not rec:
        return Identity(f"SYM:{ticker}:{start:%Y-%m-%d}", "none")
    attrs = {c: rec.get(c) for c in IDENTITY_COLS}
    if rec.get("composite_figi"):
        return Identity(rec["composite_figi"], "figi", **attrs)
    if rec.get("share_class_figi"):
        return Identity(rec["share_class_figi"], "share_class_figi", **attrs)
    if rec.get("cik"):
        return Identity(f"CIK:{rec['cik']}:{ticker}", "cik", **attrs)
    return Identity(f"SYM:{ticker}:{start:%Y-%m-%d}", "symbol", **attrs)


# ---------------------------------------------------------------- episodes


def trading_episodes(bars: pl.LazyFrame, gap_days: int = 30) -> pl.DataFrame:
    """One row per (ticker, episode): start, end, n_days. A gap of more than `gap_days` calendar
    days without a bar closes an episode."""
    df = (
        bars.select("ticker", "date")
        .unique()
        .sort(["ticker", "date"])
        .with_columns(prev=pl.col("date").shift(1).over("ticker"))
        .with_columns(
            brk=(pl.col("prev").is_null() | ((pl.col("date") - pl.col("prev")).dt.total_days() > gap_days)).cast(
                pl.Int32
            )
        )
        .with_columns(ep=pl.col("brk").cum_sum().over("ticker"))
        .group_by(["ticker", "ep"])
        .agg(start=pl.col("date").min(), end=pl.col("date").max(), n_days=pl.len())
        .sort(["ticker", "start"])
        .collect()
    )
    return df


# ---------------------------------------------------------------- assignment


def _snapshot_identities(snapshots: pl.DataFrame) -> dict[str, list[tuple[dt.date, Identity]]]:
    """ticker -> [(asof, Identity)] sorted by asof."""
    out: dict[str, list[tuple[dt.date, Identity]]] = {}
    cols = ["ticker", "asof", *[c for c in IDENTITY_COLS if c in snapshots.columns]]
    for row in snapshots.select(cols).sort(["ticker", "asof"]).iter_rows(named=True):
        ident = identity_from_record(row, row["ticker"], row["asof"])
        out.setdefault(row["ticker"], []).append((row["asof"], ident))
    return out


def _bisect_boundary(
    ticker: str,
    dates: list[dt.date],
    lo_idx: int,
    hi_idx: int,
    left: Identity,
    lookup: Lookup,
) -> int:
    """dates[lo_idx] has identity `left`, dates[hi_idx] has a different identity.
    Return the index of the first date whose identity differs from `left`."""
    while hi_idx - lo_idx > 1:
        mid = (lo_idx + hi_idx) // 2
        ident = identity_from_record(lookup(ticker, dates[mid]), ticker, dates[mid])
        if ident.security_id == left.security_id:
            lo_idx = mid
        else:
            hi_idx = mid
    return hi_idx


def assign_identities(
    episodes: pl.DataFrame,
    bar_dates: dict[str, list[dt.date]],
    snapshots: pl.DataFrame,
    lookup: Lookup,
    reuse_candidates: set[str] | None = None,
    workers: int = 1,
) -> pl.DataFrame:
    """Split each trading episode into segments with a single security identity.

    bar_dates: ticker -> sorted list of session dates with a bar (used for bisection).
    reuse_candidates: tickers for which the identity at episode start/end is verified with a
    lookup even when snapshots exist inside the episode (symbol reuse without a trading gap).
    """
    snap = _snapshot_identities(snapshots)
    reuse_candidates = reuse_candidates or set()
    eps = episodes.to_dicts()
    if workers > 1 and len(eps) > 50:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as ex:
            chunks = list(ex.map(lambda e: _assign_episode(e, bar_dates, snap, lookup, reuse_candidates), eps))
        rows = [r for chunk in chunks for r in chunk]
    else:
        rows = [r for e in eps for r in _assign_episode(e, bar_dates, snap, lookup, reuse_candidates)]
    return pl.DataFrame(rows).sort(["ticker", "valid_from"])


def _assign_episode(
    ep: dict,
    bar_dates: dict[str, list[dt.date]],
    snap: dict[str, list[tuple[dt.date, Identity]]],
    lookup: Lookup,
    reuse_candidates: set[str],
) -> list[dict]:
    rows: list[dict] = []

    def emit(ticker: str, ident: Identity, start: dt.date, end: dt.date, how: str) -> None:
        rows.append(
            {
                "ticker": ticker,
                "security_id": ident.security_id,
                "valid_from": start,
                "valid_to": end,
                "id_source": ident.source,
                "method": how,
                "is_test": (not ident.known) and bool(TEST_TICKER_RE.match(ticker)),
                **{c: getattr(ident, c) for c in IDENTITY_COLS},
            }
        )

    t, start, end = ep["ticker"], ep["start"], ep["end"]
    dates = [d for d in bar_dates.get(t, []) if start <= d <= end]
    if not dates:
        dates = [start, end]
    inside = [(a, i) for a, i in snap.get(t, []) if start <= a <= end]

    if not inside:
        ident = identity_from_record(lookup(t, start), t, start)
        if ident.known:
            # a single lookup at the start; verify the end only for plausible reuse
            if t in reuse_candidates and len(dates) > 1:
                ident_end = identity_from_record(lookup(t, end), t, end)
                if ident_end.security_id != ident.security_id:
                    k = _bisect_boundary(t, dates, 0, len(dates) - 1, ident, lookup)
                    emit(t, ident, start, dates[k - 1], "lookup")
                    emit(t, ident_end, dates[k], end, "bisect")
                    return rows
            emit(t, ident, start, end, "lookup")
        else:
            # try the end too (episode may have started before the vendor's record)
            ident_end = identity_from_record(lookup(t, end), t, end) if end != start else ident
            if ident_end.known:
                emit(t, ident_end, start, end, "lookup_end")
            else:
                emit(t, ident, start, end, "unresolved")
        return rows

    # anchors: (date, identity) points we trust, in date order
    anchors: list[tuple[dt.date, Identity]] = list(inside)
    if t in reuse_candidates:
        if anchors[0][0] != start:
            anchors.insert(0, (start, identity_from_record(lookup(t, start), t, start)))
        if anchors[-1][0] != end:
            anchors.append((end, identity_from_record(lookup(t, end), t, end)))
    # drop unknown anchors (lookup 404) unless nothing else is known
    known = [a for a in anchors if a[1].known]
    anchors = known or anchors[:1]

    seg_start = start
    cur_date, cur = anchors[0]
    for a_date, a_ident in anchors[1:]:
        if a_ident.security_id == cur.security_id:
            cur_date = a_date
            continue
        # identity changed somewhere in (cur_date, a_date]: bisect over the sessions in between
        lo = min(_index_at_or_after(dates, cur_date), len(dates) - 1)
        hi = min(_index_at_or_after(dates, a_date), len(dates) - 1)
        k = _bisect_boundary(t, dates, lo, hi, cur, lookup) if hi > lo else hi
        if k > 0:
            emit(t, cur, seg_start, dates[k - 1], "snapshot")
        seg_start = dates[k]
        cur_date, cur = a_date, a_ident
    emit(t, cur, seg_start, end, "snapshot")
    return rows


def _index_at_or_after(dates: list[dt.date], d: dt.date) -> int:
    import bisect

    return bisect.bisect_left(dates, d)


# ---------------------------------------------------------------- securities table


def securities_from_master(master: pl.DataFrame, current_tickers: pl.DataFrame | None = None) -> pl.DataFrame:
    """One row per security id: latest attributes, first/last trade, tickers used."""
    latest = (
        master.sort(["security_id", "valid_to"])
        .group_by("security_id")
        .agg(
            [pl.col(c).drop_nulls().last() for c in IDENTITY_COLS]
            + [
                pl.col("ticker").last().alias("ticker"),
                pl.col("ticker").unique().sort().alias("tickers"),
                pl.col("valid_from").min().alias("first_trade"),
                pl.col("valid_to").max().alias("last_trade"),
                pl.col("id_source").first(),
                pl.col("is_test").any(),
            ]
        )
    )
    if current_tickers is not None and "delisted_utc" in current_tickers.columns:
        cur = current_tickers.select(
            "ticker",
            pl.col("active").alias("ref_active"),
            pl.col("delisted_utc").str.slice(0, 10).str.to_date(strict=False).alias("delisted_date"),
        )
        latest = latest.join(cur, on="ticker", how="left")
    return latest.sort("security_id")


# ---------------------------------------------------------------- mapping helper


def map_to_security(df: pl.DataFrame, master: pl.DataFrame, date_col: str = "date") -> pl.DataFrame:
    """Attach `security_id` to any frame with (ticker, date). Rows outside every validity window get null."""
    m = master.select("ticker", "valid_from", "valid_to", "security_id").sort(["ticker", "valid_from"])
    joined = (
        df.sort(["ticker", date_col])
        .join_asof(m, left_on=date_col, right_on="valid_from", by="ticker", strategy="backward", check_sortedness=False)
        .with_columns(
            security_id=pl.when(pl.col(date_col) <= pl.col("valid_to")).then(pl.col("security_id")).otherwise(None)
        )
        .drop(["valid_from", "valid_to"])
    )
    return joined


def check_master(master: pl.DataFrame, episodes: pl.DataFrame) -> dict[str, int]:
    """Consistency report."""
    overlaps = (
        master.sort(["ticker", "valid_from"])
        .with_columns(prev_to=pl.col("valid_to").shift(1).over("ticker"))
        .filter(pl.col("prev_to").is_not_null() & (pl.col("valid_from") <= pl.col("prev_to")))
        .height
    )
    covered = master.group_by("ticker").agg(pl.len().alias("segments"))
    return {
        "segments": master.height,
        "tickers": master["ticker"].n_unique(),
        "securities": master["security_id"].n_unique(),
        "episodes": episodes.height,
        "overlapping_segments": overlaps,
        "unresolved": master.filter(pl.col("id_source") == "none").height,
        "test_symbols": master.filter(pl.col("is_test")).height,
        "tickers_with_multiple_securities": covered.join(
            master.group_by("ticker").agg(pl.col("security_id").n_unique().alias("n")), on="ticker"
        )
        .filter(pl.col("n") > 1)
        .height,
        "securities_with_multiple_tickers": master.group_by("security_id")
        .agg(pl.col("ticker").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
        .height,
    }
