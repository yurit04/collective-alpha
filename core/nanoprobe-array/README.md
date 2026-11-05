# nanoprobe-array

Intraday bars and features from the stored Massive minute bars: a regular-session clock, bars
of any size with an explicit rule for empty bars, official auction prices, per-bar features,
and a store of daily intraday summaries for every security since 2003. It is the data layer for
intraday research; the intraday backtester (`tactical-cube`) comes next. Design:
[docs/decisions/0014-intraday.md](../../docs/decisions/0014-intraday.md).

```python
from nanoprobe_array import Nanoprobe, FEATURES

p = Nanoprobe()                       # summaries under ~/market_data/nanoprobe_array (NANOPROBE_DATA_ROOT)

# a few names over a long span
b = p.bars(["SPY", "QQQ"], "2024-01-01", "2024-12-31", freq="5min", fill="grid",
           prev_close=True, features=["ret_open", "gap", "vwap_dev", "or_break", "vol_ratio"])

# the whole market or a size universe, one day at a time (a year of everything does not fit in memory)
for day, bars in p.days("2025-01-01", "2025-12-31", top=500, tickers=["SPY"], freq="15min",
                        features=list(FEATURES), prev_close=True):
    ...

p.clock("2025-11-28", freq="30min")   # the bars of a session (early closes included)
p.auctions("NVDA", "2024-06-07", "2024-06-11")   # official open/close, prev_close on today's basis
p.summaries(["SPY", "AAPL"], "2020-01-01")       # one row per security and day (build first)
```

CLI:

```bash
uv run nanoprobe build              # missing years and the current one (about a minute)
uv run nanoprobe build --full       # every year since 2003 (~27 min on 4 processes, 3.4 GB, 49.5M rows)
uv run nanoprobe status
uv run nanoprobe show AAPL 2026-10-30 --freq 30min
```

## Bars

- **Regular session only**, anchored at the open and labelled by their end: a bar covers
  `[bar_start, bar_end)`; the last bar ends at the close, so it is shorter when the size does not
  divide the session. Early closes come from the NYSE calendar.
- **Prices are raw within a day**, as traded. Across days use `prev_close` (the previous trading
  day's official close on today's share basis, splits and dividends taken out) or `auctions`.
- **`fill="grid"`** gives every security a row for every bar. An empty bar has `volume` 0,
  `stale` true, `vwap` null and the previous close as its prices, carried within the day only;
  before a security's first print of the day its prices are null. A halt is a run of stale bars.
- **`vwap`** is the volume-weighted typical price (high + low + close) / 3 of the bar's
  minutes: the flat files carry no VWAP.
- **The close auction is not in the minute bars.** The last minute's close is 0.5-2 bp from the
  official close for SPY and AAPL and 9-124 bp for small caps, and the auction is 15-40% of a
  day's volume. Price close fills with `auctions(...)["close_auction"]`, and never feed it to
  a decision taken that day. The opening auction is the first regular minute's open.

## Features

| name | meaning |
|---|---|
| `ret_bar` | close / previous bar's close - 1 (first bar: close / open - 1) |
| `ret_open` | close / the day's first open - 1 |
| `ret_prev` | close / previous official close - 1 (needs `prev_close`) |
| `gap` | first open / previous official close - 1 (needs `prev_close`) |
| `vwap_dev` | close / VWAP since the open - 1 |
| `rvol` | realized volatility since the open, sqrt of summed squared log bar returns |
| `range_pos` | position of the close in the day's range so far, 0 to 1 |
| `or_high`, `or_low`, `or_break` | opening range (bars ending within `or_minutes`, default 30) and +1 / -1 / 0 for a close above, below or inside it; null until the range is complete |
| `vol_ratio` | volume since the open / its average at the same bar over the previous `lookback` (20) days; needs `fill="grid"` |

Every value at a bar uses only that day's bars up to it, and `vol_ratio` only earlier days (a
test changes everything after a bar and checks nothing before it moves).

## Daily summaries

One row per security and trading day, for every security with regular-session minutes and a
daily bar (2.7M rows in 2025):

| column | meaning |
|---|---|
| `open`, `close`, `volume` | official (auctions included, all venues), raw |
| `prev_close` | previous trading day's close on this day's share basis |
| `minute_volume`, `minutes_traded`, `vwap`, `high`, `low` | regular-session minutes |
| `auction_share` | 1 - minute_volume / volume: the closing auction and other prints outside the minute bars |
| `ret_overnight`, `ret_intraday` | open / prev_close - 1, close / open - 1 (total returns) |
| `ret_first30` | open to the last print before 10:00 |
| `ret_last30` | last print before close - 30 min to the official close |
| `ret_close_auction` | last regular minute's close to the official close |
| `rv_5min`, `bars_5min` | realized volatility from 5-minute closes, and how many 5-minute bars traded |

`build()` redoes only years whose file is missing or behind the minute bars, so a nightly run
(`tools/nightly_sync.sh`) takes about a minute.
