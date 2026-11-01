# 0002: Security master (stable identity for every traded security)

Date: 2026-10-31. Status: accepted.

## Problem
Massive data is keyed by ticker, and tickers are not identifiers:
- **Renamed:** Meta traded as `FB` until 2022-06-08, then as `META`.
- **Reused:** `FB` has been a ProShares ETF since 2025-06-26; `META` was the Roundhill Metaverse
  ETF in 2021-22; `TWTR` was Tweeter Home Entertainment before Twitter.
- **Corporate actions are filed under the company's current ticker**, sometimes twice: United
  Technologies' 2004-2020 dividends appear under both `UTX` and `RTX`.

Joining or adjusting by ticker therefore mixes companies (the ETF's dividends were applied to
Facebook's prices) and splits one company's history in two.

## Decision
Build a security master inside `massive-subspace-stream`, from Massive data only:
1. Monthly point-in-time reference snapshots (`/v3/reference/tickers?date=`), last trading day
   of each month since 2003-09.
2. Trading episodes from daily bars (a gap of more than 30 days starts a new episode).
3. Identity changes inside an episode: consecutive anchors whose FIGIs (or, without FIGIs,
   CIKs) disagree. The exact first day is found by binary search with dated single-ticker
   lookups (`/v3/reference/tickers/{ticker}?date=`), cached on disk.
4. A CIK change without FIGI evidence only counts as a new security if the price jumps
   across the boundary (holding-company reorganizations like Alphabet in 2015 keep the CIK
   history continuous in price).
5. Segments are linked into one security by: equal FIGI (non-overlapping windows), the same
   ticker across a gap with matching ids, a **CIK hand-off** (ticker A stops, ticker B with the
   same CIK starts within a week, price continuous, no other candidate, no conflicting FIGIs, not
   a fund: fund trusts share one CIK across hundreds of ETFs), or a manual override.
6. `security_id` = the earliest composite FIGI of the security, else share-class FIGI, else
   `CIK:<cik>:<ticker>`, else `SYM:<ticker>:<first trade date>`.
7. Corporate actions are mapped to securities (Massive's current-ticker convention first, then
   the ticker's holder on that date, 7-day grace), de-duplicated per security, and dividend
   factors are recomputed per security from cash amounts and the previous close.

It lives in this package because `Store` itself needs it (bar adjustment, universes). A
separate package could not fix `Store` without a dependency cycle.

## Why not simpler
- **Current reference table only:** it knows today's holder of each symbol; 1,858 tickers in
  the bars have no record at all.
- **FIGI only:** 24% of rows carry a FIGI in 2003 and 33% in 2008, and a FIGI can change at a
  rename (UTX vs RTX).
- **Massive ticker events:** incomplete and sometimes wrong (GOOGL's events say it became GOOG).
- **Dated lookups only:** they have holes (`GOOGL` is "not found" in 2016), so they confirm and
  locate changes but cannot be the only source.

## Manual overrides
`_identity/overrides.csv` (in the repo, one documented row per case) can force a break, force a
link, or add a split. First entry: on 2014-04-03 Google's class A moved from `GOOG` to `GOOGL`
and `GOOG` became class C; Massive has no record of the class C distribution.

## Additional rules found on real data
- **When-issued and temporary tickers** (`UTXw`, `IACIV`, reverse-split `...D` lines) carry the
  parent's FIGI and trade alongside it. A security can't trade under two tickers at once, so the
  shorter-lived one is split off with id `<parent id>:<ticker>`.
- **Same ticker across a gap** links only on a shared FIGI, or a shared CIK for non-funds.

## First build (2026-10-31)
278 snapshots and 42,404 dated lookups (9 minutes; a rebuild from the cache takes 15 seconds).
50.2M bar-days, 100% mapped; 48,808 ticker windows became 41,455 securities. All regression
cases pass; a random sample of CIK hand-off links were all real renames. Dividend factors match
Massive's to within 0.2% for 99% of dividends.

## Known limits
- Spin-offs are not adjustment events (as before): adjusted prices drop on the spin-off date.
- Non-USD dividends (0.5%, mostly CAD) get no factor; Massive has none either.
- Ambiguous corporate actions (several possible securities) are counted, not guessed silently.
- Attributes like `type` are mostly empty in old snapshots; the security's latest known value
  fills the gap, which is a mild look-ahead for classification only.

## Addendum (2026-11-01): split checks
Found while building vinculum (0004): 185 of 6,234 testable mapped splits (3%) are not reflected
in the security's own price (actions filed under the wrong company, or wrong); they are now
dropped, and kept only when the close moves by about the split ratio or cannot be tested.
Massive records some stock dividends as 1 -> N (TotalEnergies 1 -> 91.6); with N > 10 they are
read as (N-1) -> N. Both change `adjust=` results for the securities concerned.
