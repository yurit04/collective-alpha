# adaptation-matrix (core, depends on massive-subspace-stream, vinculum, unimatrix-zero)

Transaction costs measured from Massive trades and NBBO quotes: quoted, effective and realized
half-spreads, price impact, auctions against the quote, minute quotes and signed flow, and trades
matched to the NBBO for the largest names. Built once from a schedule of days (the raw ticks are
deleted after each day), because a smaller Massive plan cannot download trades and quotes again.
It also fits the backtesters' cost models on them (`calibrate`). User docs: README.md in this
folder. Design: docs/decisions/0016-adaptation-matrix.md, 0017-cost-calibration.md.

## Public API (`from adaptation_matrix import ...`)

| Name | Use |
|---|---|
| `AdaptationMatrix(root=None, settings=None, store=None, ingestor=None, vinculum=None)` | reads, schedule, run; see below |
| `load_settings(config_file=None, **overrides)` / `Settings` | data_root (`ADAPTATION_DATA_ROOT`), max_store_gb (480), disk_margin_gb (50), keep_top (1000), keep_etfs (100), recent_months (6), chunk_quotes |
| `measure_block(trades, quotes, open_ns, close_ns, interp, keep, official)` -> `DayBlock(minute, daily, trades)` | the measures for tick frames in massive-subspace-stream's schema (pure; used by the run and tests) |
| `schedule(sessions, end, recent_months)` | the day plan as a frame (day, set, priority) |
| `MEASURE_VERSION`, `RunResult`, `ConfigError`, `BusyError` | measure version stored per day / run summary / errors |
| `Calibration`, `FittedSpread` | what `calibrate()` returns: `spread` (FittedSpread: adv_slope, vol_slope, knots, levels by year, `predict(adv, vol, year)`), `validation` (held-out period by ADV bucket, against the former volume rule), `close`, `impact`, `tod` (multiplier by segment start), `tod_pairs()` (tactical-cube's `tod`), `to_json()` |

`AdaptationMatrix` methods (reads take `backend="polars"|"pandas"`, `by="ticker"|"security"`, `lazy`):
- `daily(tickers, start, end)`: per measured day and ticker: `qhs_bps` (time-weighted quoted
  half-spread), `qspread` ($), `depth_usd`, `q_ns`; `eff_bps` (dollar-weighted effective
  half-spread), `eff_ew_bps`, `eff_lit_bps`, `eff_trf_bps`; `rs_1m_bps`, `pi_1m_bps`,
  `rs_5m_bps`, `pi_5m_bps` (realized half-spread and price impact, rs + pi = eff); volume,
  dollar_volume, buy_dv, sell_dv, trf_dv, mdv (matched dollar volume); n_trades, n_matched,
  n_quotes; outside_share, odd_lot_share, iso_share, unsigned_share; open/close auction price
  and volume (largest flagged print; none flagged in the 2000s), official_open/close (day bar),
  mid and qhs at open+1 min, close-10 min and just before the close; `close_vs_mid_bps`
  (official close vs the pre-close mid), `close_vs_mid10_bps`, `open_vs_mid1_bps`
- `minute(tickers, start, end)`: per day, ticker and session minute (0 = first): q_ns, qhs_bps,
  qspread, depth_usd, bid_end, ask_end, q_segments, n_trades, volume, dollar_volume, buy_dv,
  sell_dv, trf_dv, mdv, eff/rs/pi bps (weights: q_ns for quotes, mdv for trades)
- `buckets(tickers, start, end, minutes=30)`: minutes aggregated with the same weights
- `trades(tickers, start, end)`: kept names only: t (UTC, matching time), price, size, exchange,
  conditions, kind (0 continuous, 1 open auction, 2 close, 3 reopen), bid, ask, sign, mid_1m,
  mid_5m, quote_age_us
- `days()`, `status()`: state per day (status, set, version, rows, GB, download/convert/measure s)
- `schedule(end, recent_months)` -> day, set, priority, done; `estimate(sched, mb_per_s)` -> GB
  and hours per set and year (lists Massive's files; downloads nothing)
- `calibration_inputs(engine=None, days=None, refresh=False)`: measured stock-days joined with
  unimatrix-zero `Engine.returns` (adv, vol, raw_close, half_spread) and an asset class (stock,
  etf, other); cached in `_calibration/inputs.parquet` (about 3 minutes to rebuild)
- `calibrate(engine=None, test_start=None, tod_days=60, refresh=False, write=True)` ->
  `Calibration`; writes `_calibration/calibration.json`
- `run(end, sets, limit, days, on_day)` -> `RunResult`: download, measure, store, delete;
  resumable; long (ask first). `measure_stored(day)`: a day already in the Massive store

CLI: `adapt plan [--days] | run [--sets --limit --day] | measure-stored DAY | calibrate [--test-start --refresh] | status | show TICKER`.

## Internals (private)

```
_config.py    settings ([adaptation_matrix] in collective.toml, ADAPTATION_DATA_ROOT)
_layout.py    paths, state.json, run lock, store size
_clean.py     condition-code rules, usable quotes, matching time, interpolated time
_measure.py   one block of tickers of one day -> minute, daily, matched trades
_schedule.py  recent / monthly sample / event days and their order
_calibrate.py inputs for the fits, spread fit (Poisson, year levels), validation, close, impact,
              time-of-day profile, Calibration
_run.py       Runner: downloader thread (one day ahead), convert, measure in ticker blocks,
              PartWriter (atomic per-table files), disk and store limits, state
_store.py     AdaptationMatrix
cli.py        `adapt`
```

Invariants:
- Never hold massive-subspace-stream's writer lock: tick files come from
  `Ingestor.download_ticks` into `_work/` and are deleted after the day; a run never blocks the
  nightly sync. One run at a time (`_run/run.lock`).
- A day is `done` only after its three tables are renamed into place; state records
  `MEASURE_VERSION`. Raise the version when a measure's definition changes; days of an older
  version are measured again only while the plan still has trades and quotes.
- Lit trades and quotes match on the SIP timestamp; TRF trades (exchange 4) on
  `participant_timestamp` (execution time; the SIP stamp is 8-265 ms late). Whole-second days
  (the 2000s) use Holden-Jacobsen interpolated time. The quote matched is the last usable one
  strictly before the trade; unusable quotes (crossed, one-sided, spread > half the mid) leave
  the previous one in force.
- Measures in bps of the prevailing mid, half-spreads; trades more than 25% from the mid are
  left out of measures but counted. Days with a high `outside_share` (> 0.2: SPY in 2005, whose
  quotes are not a full NBBO) are data problems; filter them, never fix them in place.
- Auction comparisons use the day bar's official open and close (verified equal to the auction
  prints), so days without flagged auction prints still get them.
- The store is an archive: `collective roots` marks it `archive`, and `tools/reset.sh` keeps it
  unless `--include-archive`.
- The fits use only inputs a backtest has from daily bars on any plan (ADV, volatility, year), as
  unimatrix-zero's `Engine.returns` computes them; never fit on a measure the backtester cannot
  see. Stocks only (ETFs trade far tighter), stock-days with 20+ matched trades and
  `outside_share` < 0.2.
- `calibrate` reports; it changes nothing elsewhere. unimatrix-zero's `SPREAD_FIT` and
  tactical-cube's `tod` default are copies of its output (`adapt calibrate` says whether
  `SPREAD_FIT` still matches); update them together with decision 0017.
