"""Cleaning rules for Massive (SIP) trades and NBBO quotes, and the timestamp each trade is matched on.

Trade sale conditions follow Massive's `conditions` reference (asset class stocks):
- auction prints: opening 17 (CTA "O") and 25 (UTP "O"); closing 19 (CTA "6") and 8 (UTP "6");
  reopening 18 and 28. Measured separately, never as continuous trades.
- excluded from continuous measures: prints that are not a trade at the prevailing quote, or are
  reported late or out of sequence (average price, cash, cross, derivatively priced, Form T,
  next day, price variation, prior reference price, seller, sold last, out of sequence,
  contingent, qualified contingent, official open/close reports, corrected close, ...).
- kept: no condition, automatic execution 3, bunched 4/5, intermarket sweep 14, split 34,
  yellow flag 36, odd lot 37, trade-through exempt 41, and the indicator codes 57-71
  (short sale restriction, financial status), which say nothing about the price.
Trades with `correction` != 0 are dropped.

Quotes: a quote is usable when bid > 0, ask > 0, ask >= bid and the spread is at most half the
midpoint. Unusable quotes are dropped, so the previous usable quote stays in force (Holden and
Jacobsen 2014).

Timestamps: lit trades and quotes are matched on the SIP timestamp (one clock). Off-exchange
trades (exchange 4, the FINRA TRFs) are reported to the SIP late (median 8-265 ms on
2026-11-10), so they are matched on `participant_timestamp`, the execution time. Days whose quote
timestamps are whole seconds (the 2000s) get Holden-Jacobsen interpolated times: the i-th of n
events of a ticker within a second is placed at (2i - 1) / 2n of that second, for quotes and for
whole-second trades.
"""

from __future__ import annotations

import polars as pl

TRF_EXCHANGE = 4
OPEN_AUCTION = (17, 25)
CLOSE_AUCTION = (8, 19)
REOPEN_AUCTION = (18, 28)
EXCLUDED = (1, 2, 6, 7, 9, 10, 11, 12, 13, 15, 16, 20, 21, 22, 23, 24, 27, 29, 30, 31, 32, 33, 35, 38,
            52, 53, 55)
ODD_LOT = 37
ISO = 14
MAX_RELATIVE_SPREAD = 0.5   # (ask - bid) / mid above this: not a usable quote
WHOLE_SECOND_ERA = 0.9      # share of whole-second quote timestamps that marks a day as second-stamped

SECOND = 1_000_000_000


def has_any(col: str, codes: tuple[int, ...]) -> pl.Expr:
    """True where the list column holds any of `codes` (null lists: False)."""
    return pl.col(col).list.eval(pl.element().is_in(list(codes))).list.any().fill_null(False)


def classify_trades(lf: pl.LazyFrame) -> pl.LazyFrame:
    """Add `kind`: 0 continuous, 1 opening auction, 2 closing auction, 3 reopening, -1 excluded."""
    bad = (has_any("conditions", EXCLUDED) | (pl.col("correction").fill_null(0) != 0)
           | (pl.col("price") <= 0) | (pl.col("size") <= 0))
    kind = (pl.when(has_any("conditions", OPEN_AUCTION)).then(1)
            .when(has_any("conditions", CLOSE_AUCTION)).then(2)
            .when(has_any("conditions", REOPEN_AUCTION)).then(3)
            .when(bad).then(-1)
            .otherwise(0))
    return lf.with_columns(kind.cast(pl.Int8).alias("kind"))


def usable_quote() -> pl.Expr:
    mid = (pl.col("bid") + pl.col("ask")) / 2
    return ((pl.col("bid") > 0) & (pl.col("ask") > 0) & (pl.col("ask") >= pl.col("bid"))
            & ((pl.col("ask") - pl.col("bid")) <= MAX_RELATIVE_SPREAD * mid))


def interpolate_seconds(lf: pl.LazyFrame, col: str = "ts", only_whole: bool = False) -> pl.LazyFrame:
    """Holden-Jacobsen interpolated time on an int64 ns column, per ticker and second, keeping
    row order. With only_whole, rows whose stamp has a sub-second part keep it (and are not
    counted)."""
    sec = pl.col(col) // SECOND
    whole = (pl.col(col) % SECOND) == 0
    lf = lf.with_columns(sec.alias("__sec"), whole.alias("__whole"))
    grp = ["ticker", "__sec", "__whole"]
    i = pl.int_range(1, pl.len() + 1).over(grp)
    n = pl.len().over(grp)
    interp = pl.col("__sec") * SECOND + ((2 * i - 1) * SECOND) // (2 * n)
    new = pl.when(pl.col("__whole")).then(interp).otherwise(pl.col(col)) if only_whole else interp
    return lf.with_columns(new.cast(pl.Int64).alias(col)).drop("__sec", "__whole")


def match_time() -> pl.Expr:
    """The time a trade is matched on: execution time for TRF prints, SIP time otherwise (ns)."""
    sip = pl.col("sip_timestamp").dt.epoch("ns")
    part = pl.col("participant_timestamp").dt.epoch("ns")
    return (pl.when((pl.col("exchange") == TRF_EXCHANGE) & part.is_not_null() & (part > 0))
            .then(part).otherwise(sip)).alias("t")
