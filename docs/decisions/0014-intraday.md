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
- **VWAP needs volume-weighted typical prices**, and `vwap_dev` is null until the day's first
  print (0 / 0 would read as NaN).
- **`days()` reads only the named securities** (by security, so ids and renamed tickers both
  work) unless a size universe is asked for, which needs the whole day (0.1 s).
