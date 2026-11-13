# 0014 · Intraday research on minute bars (2026-11-04)

## Context
Every package after the data layer decides once a day at the close: neural-processor builds
daily and monthly panels, unimatrix-zero counts lag in trading days and fills at the close,
transwarp-conduit sends market-on-close orders once a day. The owner wants to research (and
later trade) strategies that decide within the day on minute bars. Massive minute bars are
already stored for the whole history (2003-09-10 onward, 5,825 days, 82 GB, one Parquet file
per day sorted by ticker).

## Plan
| Phase | What | Package |
|---|---|---|
| 0 | data checks (below) and this record | - |
| 1 | session clock, resampled bars with gap rules, intraday features, daily summaries store | `nanoprobe-array` (new) |
| 2 | intraday backtester: bar lag, fill models, participation, flat-by time, costs | `tactical-cube` (new) |
| 3 | spread and impact by time of day from a quotes/trades sample (download needs approval) | tactical-cube |
| 4 | intraday inputs for daily strategies, same-day close execution from late signals | neural-processor, unimatrix-zero |
| 5 | paper intraday execution off the stream service (own sign-off) | transwarp-conduit / unicomplex |

Defaults agreed with the owner: a few names or ETFs at many bars first, flat at the close by
default (overnight supported), 5-minute decisions (1-minute supported), live intraday trading
eventually, paper first.

The daily engine stays as it is. Intraday code goes into new packages, so the tested daily
invariants (lag in days, delisting rows, drift) are not mixed with bar-level ones. The intraday
engine will report daily P&L in unimatrix-zero's format, so both kinds of strategy compare
with the same analytics.

## Phase 0: what the data shows
Measured on the stored files and the stream service's live files.

| Check | Result | Consequence |
|---|---|---|
| Read speed | AAPL 1 year 0.2 s; 50 tickers 1 year 0.3 s; whole market 1 day 0.1 s | `Store.bars(freq="1min")` is fast enough; no new storage format |
| Memory | whole market, 1 month, regular session: 37M rows, 2.7 GB | whole-market work goes one day at a time |
| Opening auction | first regular minute's open = official open (0 bp on every name checked) | opening fills can be priced from minute bars |
| Closing auction | last minute's close vs official close: SPY 0.5 bp, AAPL 2 bp, small caps 9-124 bp; minutes carry 60-92% of daily volume | close fills use the daily bar's close; minute bars never stand in for the close auction |
| Gaps | small caps (ASTE, CRMT) trade in 136-163 of 390 minutes | resampled bars mark empty bars (`stale`); prices carry forward within the day only |
| Halts | SPY on 2020-03-09 has no bars 09:35-09:48 ET (the market-wide halt) | a halt is a run of empty bars; nothing fills on an empty bar |
| Early closes | regular session on 2025-11-28 ends 13:00 ET (210 minutes); 50 later bars are extended hours | the clock comes from `Store.sessions` |
| Splits | NVDA 2024-06-07 last close 1,208.65, next day 121.65 raw; `adjust="splits"` 120.865 | within a day raw prices; across days (gaps, overnight) adjusted daily bars |
| Live bars | regular session: written 3-31 s after the minute ends (p50 17 s, the 30 s flush); 99.75% of closes equal the official file | a 1-bar lag is the minimum live; at 5 minutes it leaves 4.5 minutes of slack |
| Identity | within a day, one ticker = one `security_id` and back | per-day frames are keyed by `security_id` |

Minute bars in the flat files have no VWAP; bar VWAP is the volume-weighted typical price
(high + low + close) / 3, an approximation.

## Phase 1: `nanoprobe-array`
Depends on massive-subspace-stream (bars, sessions, adjustment) and vinculum (size universes
for whole-market work). Details: core/nanoprobe-array/CLAUDE.md.

- **Bars are labelled by their end.** A bar covers [bar_start, bar_end); everything computed for
  it uses only minutes that start before bar_end. Bars are anchored at the session open, and
  the last bar of a session ends at the close (shorter if the frequency does not divide the
  session).
- **Grid fill is opt-in.** `fill="grid"` gives every security a row for every bar; an empty bar
  has volume 0, `stale` true and the previous close as its prices (within the day only;
  before the first trade the prices are null).
- **Auction prices are a separate table** (official open and close from daily bars), so the
  close cannot leak into an intraday frame by accident.
- **Features are per security per day**, from data up to each bar's end. The one feature that
  needs history (volume against its time-of-day profile) uses only earlier days.
- **Daily summaries** are built for every security and day and stored under
  `~/market_data/nanoprobe_array` (overnight, intraday, first and last half hour returns,
  closing-auction volume share, realized volatility from 5-minute returns, minutes traded).

### Built (2026-11-04)
- **Speed.** Reading and resampling a whole-market day takes 0.11 s; the top 500 plus SPY with
  every feature (including the 20-day `vol_ratio` warm-up) 0.4 s a day at 5 minutes, so 15 years
  is about 25 minutes in one process. Few-name `bars()` calls stay under a second a year.
- **Summaries store.** 2.7M rows in 2025 (215 MB); one year takes 67 s alone and about 2.5
  minutes per process with four running (4-5 for recent years); the full build (2003 onward,
  5,825 days, 49.5M rows, 3.4 GB) took 27 minutes.
  `build()` redoes only years behind the minute bars, and `tools/nightly_sync.sh` runs it after
  vinculum. There is no macOS launchd job yet (that waits for unicomplex phase 2's single
  scheduler) and no `collective status` check yet.
  *Update 2026-11-07:* unicomplex phase 2 runs `nanoprobe build` as a step of `collective run`
  on both macOS and Linux (decision 0012), and `collective status` checks the summaries
  (`nanoprobe/summaries`: through the last completed session).
- **VWAP needs volume-weighted typical prices**, and `vwap_dev` is null until the day's first
  print (0 / 0 would read as NaN).
- **`days()` reads only the named securities** (by security, so ids and renamed tickers both
  work) unless a size universe is asked for, which needs the whole day (0.1 s).

## Phase 2: `tactical-cube`
Depends on nanoprobe-array (bars, features, summaries), unimatrix-zero (CostModel, summary and
by_year) and vinculum (borrow rates). Details: core/tactical-cube/CLAUDE.md. Defaults agreed
with the owner: next-bar VWAP fills, fixed capital each day, IB per-share commission with an
AUM, studies in a new project `assimilation-trials`.

- **A strategy is a function of one day's bars.** `strategy(day, bars)` returns targets
  (`bar_end`, `security_id`, `weight`, optional `auction`). Each decision time is a whole book;
  names left out go to 0. A precomputed targets frame works too.
- **Fills `lag` bars after the decision** (at least 1), at the fill bar's VWAP by default. An
  order waits through empty bars and keeps working until filled, replaced or the day ends;
  `unfilled` records why. Closing-auction orders (decided at least 10 minutes before the
  close) fill at the official close, opening-auction orders at the next official open.
- **Participation cap only with an AUM** (10% of the bar's share volume). Without an AUM the
  backtest is per unit of capital and costs do not depend on size.
- **Positions are shares, capital is fixed each day.** Marks are bar closes, the official
  auctions and, across nights, `prev_close` on today's basis, so splits are neutral and
  dividends are paid. Daily returns are on the fixed capital; `nav` compounds them. The daily
  frame has unimatrix-zero's columns, so `summary` and `by_year` are reused as they are.
- **Costs per fill**: unimatrix-zero's half-spread times a time-of-day multiplier (3x in the
  first 5 minutes, 2x to 15, 1.5x to 30), no spread in the auctions, square-root impact on ADV
  and volatility known before the open (21 and 63 days, shifted a day), IB $0.0035 a share
  ($0.35 minimum), borrow only on shorts held overnight. Impact and the commission minimum are
  charged on the whole order (all its fills across bars), not per fill: per-fill square-root
  impact made an order split by the participation cap look cheaper than the same order sent at
  once (a $1M ETSY buy: 7.5 bp over bars vs 18.7 bp in the auction; 18.1 bp now). Phase 3
  replaces the multipliers with measured spreads.
- **Look-ahead check by default.** On the first two days with targets the strategy is re-run on
  bars cut at 25/50/75% of the session; a target up to the cut that changes raises
  `LookaheadError`.
- **Numpy inside a day.** Each day is a dense security x bar array; polars builds it and
  collects the output.

### Built (2026-11-04)
- **Exact against the daily engine.** Buying SPY, AAPL and NVDA in the closing auction every day
  of 2024 and holding, with no costs, gives the same daily returns as unimatrix-zero
  `Engine.returns` to 1.4e-16 (NVDA's split on 2024-06-10 included), once the weight actually
  held (auction price over decision price) is used.
- **Speed.** `Nanoprobe.days` now reads named securities in 20-day chunks and prepares them in one
  pass (features unchanged to 1e-13). SPY over 23 years at 5 minutes: 151 s at first, 41 s now.
  The top 500 runs about 30 s a year.
- **Summary volume was on today's split basis (fixed).** `daily_prices` undid the split and
  dividend factors on prices but not on volume, which `Store` divides by the split factor, so
  summary `volume` and `auction_share` were wrong before any later split: forward splits
  overstated volume (NVDA 2018: 40x, from its 2021 and 2024 splits), reverse splits
  understated it (TOPS 2018: 0.47 shares a day instead of ~500,000). ADV came out at $0.47 and square-root impact at 1,400%,
  which wiped out the top-500 reversal study. Volume is now raw shares; the summaries were
  rebuilt (25.7 minutes) and the phase 1 notebook re-run.
- **A size universe can hold names too thin to trade.** vinculum ranked TOPS 22nd in late 2018
  ($167B on a $0.87 close; fixed in vinculum, decision 0004 addendum 2026-11-05), and a few
  ADRs that trade mostly at home sit in the top 500 on under $2M a day. The reversal study
  leaves out names that traded under $1M between the open and its decision.

- **First results** (projects/assimilation-trials, $1M, 2004-2026): SPY intraday momentum
  +1.7%/yr gross, -3.8% net; SPY overnight +7.9% gross, +3.4% net (t 1.7), intraday +2.6% /
  -1.7%; opening range breakout on SPY and QQQ -2.0% gross; top-500 first-half-hour reversal
  (from 2010) +2.1% gross, -14.3% net. Break-even costs are 0.4-1.6 bp per unit traded (none
  for the breakout); the model charges 0.85-1.2 bp on SPY and 3.5 bp on the top 500, so phase
  3's measured costs decide whether any of it is tradable.

*Update 2026-11-11:* phase 3 moved to a new core package, `adaptation-matrix` (decision 0016):
the measures feed unimatrix-zero as well, which cannot depend on tactical-cube, and the data
had to be harvested before the Massive plan loses trades and quotes. Replacing tactical-cube's
time-of-day multipliers with the measured profiles is part of 0016's phase 2.
