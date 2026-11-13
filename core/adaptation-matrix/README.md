# adaptation-matrix

Transaction costs measured from Massive trades and quotes, kept after the ticks are gone.

Trades and quotes are about 14 GB a day (compressed, 2026) and only the Massive Advanced plan
has them. This package downloads a schedule of days, measures each one, keeps compact tables and
deletes the raw files: a few hundred GB of results from several TB of downloads. The tables
calibrate the cost models of the daily backtester (unimatrix-zero), the optimizer (borg-queen),
the intraday backtester (tactical-cube) and execution (transwarp-conduit). Design:
[decision 0016](../../docs/decisions/0016-adaptation-matrix.md); the cost model fitted on it:
[decision 0017](../../docs/decisions/0017-cost-calibration.md).

## What is measured

All in basis points of the midpoint in force, as half-spreads (a round trip costs twice):

| Measure | Meaning |
|---|---|
| quoted half-spread `qhs_bps` | (ask − bid) / 2·mid, weighted by the time each quote was in force |
| effective half-spread `eff_bps` | \|price − mid\| / mid per trade, weighted by dollar volume: what traders paid |
| realized half-spread `rs_5m_bps` | sign · (price − mid 5 minutes later) / mid: what the liquidity provider kept |
| price impact `pi_5m_bps` | sign · (mid 5 minutes later − mid) / mid: how far the price moved with the trade |
| `close_vs_mid_bps` | official close vs the midpoint just before the close: the closing auction against the quote |

Effective = realized + impact. The trade sign is Lee-Ready: above the mid a buy, below a sell, at
the mid the tick test.

Tables: `daily` (per day and ticker, also by venue: lit exchanges vs off-exchange TRF),
`minute` (per session minute: quotes, signed dollar flow and trade measures, with their weights,
so 30-minute buckets and days are exact averages of minutes) and `trades` (every trade of the
largest 1,000 companies and 100 ETFs with the NBBO in force and the midpoints 1 and 5 minutes
later).

## Days

`adapt plan` lists them and what downloading them takes:

- **recent**: every session of the last 6 months;
- **sample**: one full session a month back to 2003-09 (the first Tuesday-Thursday on or after
  the 10th), for costs across eras;
- **event**: stress days, quarterly expiries and Russell reconstitutions of the last 3 years.

The last 3 months go first, then the sample (newest first), then the rest, so a run cut short
(by the end of the subscription) has the most useful days.

```bash
uv run adapt plan --mb-per-s 15        # 423 days, ~2.6 TB to download, ~48 h at 15 MB/s
uv run adapt run --limit 5             # the first five days (about 15 minutes each)
uv run adapt run                       # everything not done yet; stop and restart any time
uv run adapt run --sets sample         # one set
uv run adapt run --day 2026-11-10      # exact days
uv run adapt measure-stored 2026-11-10 # a day already in the Massive store (no download)
uv run adapt status
uv run adapt show AAPL
```

A run downloads the next day while measuring the current one, records each finished day in
`state.json` and skips finished days on the next run. It stops before the store passes
`max_store_gb` (480) or free disk falls below `disk_margin_gb` (50). It never takes the Massive
store's writer lock, so the nightly sync keeps running.

```python
from adaptation_matrix import AdaptationMatrix

m = AdaptationMatrix()
m.daily("AAPL", "2026-06-01", "2026-11-10")              # one row per measured day
m.buckets(["SPY", "AAPL"], "2026-11-10", minutes=30)     # spreads and impact by time of day
m.trades("MSFT", "2026-11-10")                           # matched trades of a kept name
```

## The cost model fitted on it

```bash
uv run adapt calibrate                 # fit, validate on the last 6 months, print; ~10 s
uv run adapt calibrate --refresh       # rebuild the inputs first (~3 minutes)
```

```python
cal = AdaptationMatrix().calibrate()
cal.spread                             # effective half-spread: ADV and volatility slopes, level by year
cal.validation                         # held out: fitted and former rule against measured, by ADV
cal.close, cal.impact, cal.tod_pairs()
```

The spread formula uses only what daily bars give (21-day dollar volume, 63-day volatility, the
year), so backtests can use it after trades and quotes are gone. Its numbers ship as
unimatrix-zero's `SPREAD_FIT` (the default `CostModel` spread) and tactical-cube's time-of-day
multipliers:

| ADV ($ a day, 2% vol, 2025) | $10M | $100M | $1B | $10B |
|---|---|---|---|---|
| fitted effective half-spread | 7.1 bp | 3.1 bp | 1.3 bp | 0.6 bp |
| former volume rule | 9.5 bp | 3.0 bp | 0.95 bp | 0.5 bp |

Out of sample (May-November 2026) the fit is within 5-15% of measured costs in every ADV bucket
(dollar-weighted); the former rule undercharged names over $100M a day by 37-62%. Index ETFs are
not covered: SPY's effective half-spread is 0.12 bp, flat through the day. Details and plots:
`projects/subspace-probe/notebooks/adaptation-matrix/01_calibration.ipynb`.

## Settings

`[adaptation_matrix]` in `collective.toml` (or `ADAPTATION_DATA_ROOT`): `data_root`
(`~/market_data/adaptation_matrix`), `max_store_gb`, `disk_margin_gb`, `keep_top`, `keep_etfs`,
`recent_months`, `chunk_quotes` (quote rows measured at once; 25M uses about 9 GB of memory).

## Measured on the first days

| | 2026-11-10 | 2005-06-15 |
|---|---|---|
| tickers | 13,181 | 7,862 |
| trades / quotes | 129M / 622M | 7M / 18M |
| download (compressed) | 14.2 GB at 11-20 MB/s | 0.2 GB |
| convert / measure | 7 min / 2.6 min (8.9 GB memory) | 10 s / 8 s |
| stored | 1.15 GB (trades 0.83, minute 0.32) | 0.1 GB |

Median effective half-spread on 2026-11-10 by size: large caps 1.2 bp, mid 2.4, small 6.4,
micro 55.

## Limits

- Quotes are the NBBO at the top of the book; there is no depth beyond it and no order IDs, so
  impact is measured from the tape, not from metaorders. Your own fills remain the final check.
- Before 2007 (Reg NMS) the NBBO includes slow manual quotes, and some early ETF quotes are not a
  full NBBO (SPY in 2005: 28% of trades outside the quote). `outside_share` flags such days.
- The 2000s have whole-second quote timestamps; trades are matched on interpolated time.
