# nanoprobe-array (core, depends on massive-subspace-stream, vinculum)

Intraday bars and features from Massive minute bars, and a store of daily intraday summaries.
User docs: README.md in this folder. Design: docs/decisions/0014-intraday.md.

## Public API (`from nanoprobe_array import ...`)

| Name | Use |
|---|---|
| `Nanoprobe(root=None, store=None, vinculum=None)` | reads and the summaries build; see below |
| `clock(sessions, freq)` | bars of each session: date, bar, bar_start, bar_end, minutes_from_open, minutes_to_close, last |
| `resample(minutes, sessions, freq)` | minute bars -> regular-session bars (only bars with prints) |
| `grid(bars, sessions, freq, security_ids=None)` | every bar for every security, empty bars `stale` with the close carried within the day |
| `features(bars, names, or_minutes=30, lookback=20, history=None)` | per-bar features (FEATURES) |
| `FEATURES` / `Feature` | name -> description, needs ("prev_close" / "history") |
| `daily_prices(adjusted)` | `Store.bars(adjust="all")` daily -> raw official prices + prev_close on today's basis |
| `summarize(minutes, sessions, daily)` | one summary row per security and day |
| `parse_freq`, `BAR_COLUMNS`, `SUMMARY_COLUMNS`, `ENV_ROOT` | helpers and schemas |

`Nanoprobe` methods (reads take `backend="polars"|"pandas"`):
- `bars(tickers, start, end, freq="5min", by, fill="none"|"grid", prev_close, features, or_minutes,
  lookback, include_live)`: needs tickers (whole market: `days`)
- `days(start, end, tickers, top, size, freq, fill="grid", prev_close, features, ...)` -> iterator of
  (date, bars); `top`/`size` = vinculum universe of that day (ranked the previous month end),
  `tickers` added to it; nothing = every security that traded
- `clock(start, end, freq)`, `auctions(tickers, start, end, by)` (open_auction, close_auction,
  prev_close, prev_date), `universe(on, top, size, types)`
- `summaries(tickers, start, end, columns, lazy)`, `build(years=None, workers=4, full=False)`, `status()`

CLI: `nanoprobe build [--years] [--workers] [--full] | status [--json] | show TICKER DATE [--freq]`.

## Internals (private)

```
_clock.py     parse_freq, clock
_bars.py      resample, grid
_features.py  FEATURES and features (vol_ratio from earlier days)
_daily.py     daily_prices, summarize
_probe.py     Nanoprobe (store wiring, days iterator), build_year, the summaries store
cli.py        `nanoprobe`
```

Invariants:
- Bars are labelled by their end; a bar's values (and every feature at it) use only minutes that
  start before bar_end, and only that day's bars, except vol_ratio, which uses earlier days only.
  The look-ahead test perturbs everything after a bar and must see nothing change before it.
- Regular session only, from `Store.sessions` (early closes included). Never hard-code 09:30-16:00.
- Prices are raw within a day; across days only through prev_close (adjusted-factor ratio, known
  at the open) or summaries. Never mix adjusted and raw prices in one frame.
- Carry-forward is within the day only; prices before a security's first print are null.
- The official close (auction) lives in `auctions`/summaries only, never in an intraday bar frame.
- Whole-market reads go one day at a time (`days`); `bars` refuses tickers=None.
- Read-only on the Massive store; everything under this package's root is derived, `build` may
  overwrite it. Build workers are spawned, not forked (forked polars can deadlock).
