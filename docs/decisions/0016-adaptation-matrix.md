# 0016 · adaptation-matrix: transaction costs from trades and quotes (2026-11-11)

## Context
Every cost in the repo is an assumption: unimatrix-zero's half-spread is 30 bp × (ADV in $M)^-0.5,
tactical-cube multiplies it by 3/2/1.5 in the first half hour, impact is square-root with a
coefficient of 1. The decision 0014 studies break even at 0.4-1.6 bp per unit traded, inside the
range those assumptions disagree on. Trades and NBBO quotes measure costs directly, but only the
Massive Advanced plan has them, and the owner is moving to Starter. What is not measured and
stored before the switch cannot be measured afterwards. Budget: up to 500 GB on disk.

## What a test day showed (2026-11-10 and 2005-06-15)
| Check | Result | Consequence |
|---|---|---|
| Size | full history 3.8 TB trades + 15 TB quotes; 2026 day 3.1 + 11.1 GB | sample days, reduce each, delete raw |
| Download | 10-30 MB/s whatever the parallelism or boto settings | ~48 h for the plan; one day downloads while the previous is measured |
| Conversion | quotes 324 s, trades 84 s, < 5 GB memory | not the bottleneck |
| Sort order | files sorted by ticker, then time (2005 trades: 98 breaks) | measure in contiguous ticker blocks |
| Quotes are NBBO | lit trades: 54-75% at the bid or ask, 4-6% outside | effective spreads against these quotes are meaningful |
| TRF reporting delay | 47% of trades; SIP stamp 8-265 ms after execution; 13-18% "outside" on SIP time | TRF trades match on `participant_timestamp` |
| 2005 timestamps | quotes 100% whole seconds | Holden-Jacobsen interpolated time |
| SPY 2005 | 28% of trades outside its quote, any time shift | early ETF quotes are not a full NBBO; flag with `outside_share` |
| Auctions | 2005 has no flagged auction prints | compare with the day bar's official open/close |

## Decision
A new core package, `adaptation-matrix` (depends on massive-subspace-stream and vinculum), CLI
`adapt`, store `~/market_data/adaptation_matrix`.

1. **Days.** Every session of the last 6 months; one full mid-month session (Tue-Thu, on or after
   the 10th) a month back to 2003-09; stress days, expiries and Russell reconstitutions of the
   last 3 years. 423 days, 2.6 TB to download. The last 3 months first, then the sample newest
   first, so a run cut short keeps the most useful days.
2. **Reduce, then delete.** Per day: `daily` (every ticker), `minute` (every ticker and session
   minute, with weights), `trades` (matched trades of vinculum's top 1,000 plus the 100 most
   traded ETFs; vinculum has no market caps before about 2009, so the day's dollar volume tops
   the list up). 1.15 GB for a 2026 day, so about 150 GB for the recent window.
3. **Definitions** (Holden and Jacobsen 2014; Lee and Ready 1991): half-spreads in bps of the
   prevailing mid; quoted time-weighted, effective dollar-weighted; realized and impact at 1 and
   5 minutes (capped at the close); sign by Lee-Ready with the tick test. Condition codes:
   auction prints apart; average-price, cross, derivatively priced, Form T, out of sequence,
   contingent and similar prints excluded; odd lots and sweeps kept.
4. **Independent of the nightly run.** Ticks come through a new public
   `Ingestor.download_ticks` into the package's own `_work/`, never the Massive store, and take
   no Massive writer lock. A run is resumable per day and stops at `max_store_gb` (480) or
   `disk_margin_gb` (50).
5. **An archive.** `collective roots` gives it a new kind, `archive`; `tools/reset.sh` keeps it
   unless `--include-archive` (like the trading journal), because a Starter plan cannot rebuild it.

## The run (2026-11-11 21:21 to 2026-11-13 07:58)
All 423 scheduled days plus the 2005-06-15 test day, before the switch to Starter on 2026-11-13.
Download 9.5-36.5 MB/s (median 35); converting and measuring a recent day (about 10 minutes) set
the pace. One day hit a transient S3 403 and succeeded on retry. Store: 290 GB (trades 187,
minute 87, daily 1.3).

## Phase 2 (after the run): calibration
- Daily spread model for unimatrix-zero's `CostModel` (and so borg-queen's optimizer): effective
  half-spread fitted on inputs that stay available on Starter (dollar volume, price with the
  one-cent tick floor, volatility, year), compared with the current volume rule and Abdi-Ranaldo
  by size bucket.
- The close: `close_vs_mid_bps` by size decides whether close fills pay a half-spread
  (unimatrix-zero charges one, tactical-cube none).
- Time-of-day profiles from `buckets()` replace tactical-cube's 3/2/1.5 multipliers.
- Impact: price impact and signed-flow regressions by size against `impact_coef`.
- The decision 0014 studies re-run with measured costs.

## Why not
- **Raw ticks for a few names over many days:** ticker filtering still downloads the whole file,
  and the cost model needs the cross-section (every size) more than depth in a few names.
- **Store raw quotes:** 10 GB a day; the minute table and matched trades keep what the
  measures need at a tenth of the size.
- **Measure inside massive-subspace-stream:** its job is storing Massive data; a multi-day job
  holding its writer lock would block the nightly sync.
- **Put it in tactical-cube (0014 phase 3):** unimatrix-zero needs the results and cannot depend
  on tactical-cube; a data producer below both keeps the graph acyclic.
