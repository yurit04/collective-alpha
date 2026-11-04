# 0011: Materialized EDGAR series

Date: 2026-11-04. Status: accepted.

## Problem
`EdgarStore.series` built every point-in-time series from the raw facts on each call: about
2 s per item for the 9,400 companies of a top-1000 universe over ten years, three quarters of
it in `periodize` (reading the 99.5 million facts themselves takes 1.7 s). Every fundamental
panel in neural-processor, every `Vinculum.fundamentals` call and every `RiskModel.build`
repeated that work: a value panel took 18.6 s, profitability 13.4 s, growth 11.9 s, against
0.8 s for a panel that needs no EDGAR items. The planned performance work on the universe
ranking and the rebalance loop's market snapshot was measured first and would have saved
about two seconds together, so it was dropped in favour of this.

## Decision
`cortical backfill` and `cortical sync` end by building `series.parquet` (every item in
`CONCEPTS`, first-filed policy, all companies) and `shares.parquet` from the facts, with
`Ingestor.series()`; `cortical series` does it by hand. `EdgarStore.series` and `.shares` read
those tables when they are current and build from the facts otherwise.

- **Currency, not age.** `state.json` carries `facts_version`, a counter incremented by
  everything that writes facts (`bulk`, `refresh_ciks`; a counter rather than a timestamp,
  since two writes can fall in one second), and `series_facts_version`, the version the
  tables were built from. The tables are used only when the two are equal, so a partial
  refresh (a sync that fetched today's filers but failed before rebuilding, or a notebook
  calling `refresh_ciks`) can never serve stale series: reads fall back to the facts, which is
  the same result, slower. `series_status()` and `cortical status` show which.
- **Only the default policy.** `policy="latest"` is a research setting and is always built on
  the fly; materializing both would double the tables for little use.
- **One table, sorted by item and CIK.** Parquet row-group statistics make `item in items`
  and `cik in ciks` selective, so reads stay fast without one file per item.
- **Same code path.** The tables are `EdgarStore.build_tables()`, the same `build_series` and
  `build_shares` calls a read would make, so there is nothing to keep in step; the test
  compares the two paths for every item.
- **Deterministic ties.** Comparing the two paths on real data showed about 0.1% of quarters
  differing: several `sort` calls before a `unique` in `_pit.py` had incomplete keys, and
  polars sorts are not stable, so which of two filings' rows survived a tie depended on how
  many companies were in the frame. Every such sort now ends with `filed`, `start` and the
  value, which also makes the first-filed rule decide ties as intended.

## Consequences
- Fundamental panels drop from tens of seconds to a few seconds; `Vinculum.fundamentals`,
  neural-processor panels and `RiskModel.build` speed up with no change to their results.
- `backfill` and `sync` take about two minutes longer. The macOS launchd job and
  `tools/nightly_sync.sh` need no change.
- Two more derived files under the EDGAR root (a few hundred MB); `vinculum build` still
  depends on the facts only, through `series()`, so a stale table only costs time.
