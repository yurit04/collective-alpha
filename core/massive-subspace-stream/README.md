# massive-subspace-stream

Core module of collective-alpha. It downloads US equity data from
[Massive](https://massive.com) (formerly Polygon.io), stores it as Parquet under
`~/market_data/massive`, and serves it back as polars or pandas DataFrames.
It depends on no other package in this repo.

## Setup

`tools/bootstrap.sh` at the repo root runs every package's first-time setup in order, then
schedules the nightly run; see the root README's Quick start. By hand:

```bash
uv sync --all-packages      # from the repo root
uv run subspace init        # creates folders, checks credentials and plan coverage
```

Credentials are read from environment variables, or from `~/.massive.env`
(lines like `export MASSIVE_API_KEY="..."`):

| Variable | Used for |
|---|---|
| `MASSIVE_API_KEY` | REST API and websocket |
| `MASSIVE_S3_ACCESS_KEY_ID`, `MASSIVE_S3_SECRET_ACCESS_KEY` | flat files |
| `MASSIVE_REST_URL`, `MASSIVE_WS_URL`, `MASSIVE_S3_ENDPOINT`, `MASSIVE_S3_BUCKET` | optional endpoint overrides |

Other settings go in `collective.toml` (see `collective.toml.example` at the repo root).

### Massive plan

Set `plan` to your Massive Stocks subscription (`collective.toml`, or `MASSIVE_PLAN`).
The default is `advanced`.

| | starter | developer | advanced |
|---|---|---|---|
| Flat files reach back | 5 years | 10 years | everything (2003) |
| Flat-file datasets | day and minute bars | + trades | + quotes |
| Websocket | delayed 15 min, `AM` `A` | delayed, + `T` | real-time, + `Q` |
| Financials and ratios | not requested | not requested | refreshed |

What the plan setting changes:

- **Downloads start at the plan's limit.** Older days are never requested, and a day of a
  dataset the plan lacks is skipped.
- **`verify` lists days the plan can't repair** as `unrepairable`. Restore those from a
  backup, since they can't be downloaded again.
- **The stream connects to the plan's feed and subscribes only to channels the plan has.**
  A real-time `MASSIVE_WS_URL` left over from a bigger plan is replaced by the delayed feed.
- **A refused login stops the stream service instead of restarting it every 30 seconds.**
  `subspace status` shows the reason. Services installed before this change need
  `subspace service install` again.
- **The nightly refresh leaves out datasets the plan lacks.** It also doesn't look up the
  security master's dated tickers beyond the plan's history.

Snapshot datasets (`splits`, `dividends`, `ipos`, `tickers`, ...) are protected on every plan.
A refresh that answers with less history keeps the stored rows it no longer returns, and logs
a warning. Without that, a 5-year answer would silently cut the split history that adjusted
prices and vinculum's market caps depend on.

## Data services

```bash
subspace backfill initial                    # reference data, day + minute bars since 2003, REST datasets
subspace backfill initial --since 2015-01-01 --no-minute
subspace backfill history --dataset trades --start 2024-03-01 --end 2024-03-05 --symbols AAPL,MSFT
subspace backfill rest news short_interest   # refresh specific REST datasets
subspace sync                                # daily catch-up
subspace verify --dataset bars_minute --repair
subspace stream start | stop | status        # live websocket capture
subspace service install --only stream       # launchd: always-on stream (the sync runs in `collective run`)
subspace identity build                      # security master (first run: ~210 snapshots + dated lookups)
subspace identity check                      # coverage and regression checks
subspace identity show FB                    # every company FB has meant, and their other tickers
subspace status                              # coverage, sizes, stream health
subspace query bars AAPL --start 2024-01-01 --adjust splits
```

All jobs can be stopped and re-run: each finished day or dataset is recorded in
`_catalog/catalog.sqlite`, so it is skipped next time. `sync` also looks back 30 days from the
last stored day and fetches any day the store lacks, so a download that failed one night is
retried the next.

Scheduling: the nightly sync runs as the first step of `collective run` (one launchd job for
every package, installed with `collective service install --replace`; see
[unicomplex](../unicomplex/README.md)). Install only the stream here. A plain
`subspace service install` would also add this package's own 06:30 sync, which would run next
to the nightly job.

Only one job writes at a time: a backfill started while the nightly sync runs (or the other way
round) stops at once with "another job is writing to ...", naming the running job; run it again
when that one has finished.

## Querying

```python
from massive_subspace_stream import Store

store = Store()
daily = store.bars(["AAPL", "MSFT"], start="2020-01-01", adjust="splits")             # polars
minute = store.bars("NVDA", start="2024-06-03", end="2024-06-07", freq="1min",
                    session="regular", backend="pandas")                                # pandas
total_return = store.bars("KO", start="2010-01-01", adjust="all")
universe = store.universe("2015-06-30", types=["CS"])     # who traded that day, incl. later delistings
trades = store.trades("AAPL", start="2024-03-01")          # after `backfill history --dataset trades`
income = store.financials("income", tickers="AAPL")
si = store.short_interest("GME", start="2020-01-01")
news = store.news("TSLA", start="2025-01-01")
live = store.bars("SPY", start="2026-10-30", freq="1min", include_live=True)
for batch in store.follow("bars_1m", ["SPY"], timeout=600):  # new live rows as the service writes them
    print(batch)
lf = store.scan("bars_minute")                              # lazy polars over everything
store.coverage()                                            # what is stored
```

Conventions:

- Prices are stored **unadjusted**. `adjust="splits"` or `adjust="all"` (splits + dividends)
  is applied at query time, so stored history never has to be rewritten.
- Timestamps are UTC (`timestamp` = bar start). `date` is the US/Eastern trading date.
- `session="regular"` uses the NYSE calendar, including early closes.
- Delisted tickers are kept (no survivorship bias). Use `security_id`, not the ticker, to follow a
  company over time.
- Minute bars do **not** include the closing auction print (daily bars do). Use daily bars for
  daily volume and the official close.
- Financial statements: Massive keeps one row per fiscal period, dated by the *latest* filing
  that reported it, so `filing_date` is often a year after the original filing. The store keeps
  every version it downloads, so point-in-time history accumulates from the first download.

## Security master: tickers are not identifiers

Tickers get renamed (`FB` became `META` on 2022-06-09) and reused (`FB` has been a ProShares ETF
since 2025-06-26; `META` was a Roundhill ETF in 2021-22). The security master gives every
(ticker, day) in the bars a stable `security_id`, built from month-end point-in-time reference
snapshots, dated ticker lookups, trading gaps, CIK hand-offs and a small file of manual
corrections. Design: `docs/decisions/0002-security-master.md`.

```python
store.bars("META", start="2012-01-01", by="security", adjust="all")  # Meta across FB and META
store.bars("FB", start="2012-01-01")             # what traded as FB: Meta, then the ETF (see security_id)
store.resolve(["FB", "META"], on="2015-06-30")   # which security each ticker meant that day
store.securities(tickers="FB")                   # every security that used FB, with all its tickers
store.security_master("GOOG")                    # ticker x validity window -> security_id
store.universe("2015-06-30", types=["CS"])       # point-in-time types and names (FB is Facebook here)
store.security_attributes("BBG000MM2P62")        # month-end attributes (name, type, CIK) of a security
store.security_actions("splits")                 # splits as mapped to securities (what adjust= uses)
```

Once built, the master is used automatically:
- every bar row carries `security_id`;
- `adjust=` uses splits and dividends mapped to securities (Massive files a company's past actions
  under its current ticker, sometimes twice), with dividend factors recomputed per security;
- `universe()` uses the attributes as of that day.

`by="security"` resolves a ticker as of `end` (default today): use the current ticker (`META`),
or pass a `security_id`. `subspace sync` refreshes the master nightly (new month-end snapshots and
lookups only). Corrections Massive cannot supply (like Google's 2014 share-class switch) live in
`src/massive_subspace_stream/_identity/overrides.csv`, one documented row each.

Splits are checked against each security's own price: a recorded split the close does not
reflect is dropped (185 of 6,234 testable, 3%: actions filed under the wrong company or wrong),
and stock dividends Massive records as 1 -> N with N > 10 are read as (N-1) -> N (TotalEnergies'
scrip dividends appear as 1 -> 91.6).

Limits: spin-offs are not adjustment events (adjusted prices drop on the spin-off date);
non-USD dividends (0.5%, mostly CAD) are not adjusted for; old
snapshots rarely have `type`, so the security's latest known type fills the gap; ambiguous
corporate actions are counted by `subspace identity check`, not guessed.

## Datasets

| Name | Source | Notes |
|---|---|---|
| `bars_day`, `bars_minute` | flat files | full history from 2003-09-10; minute bars are about 100 GB |
| `trades`, `quotes` | flat files | on demand only: about 3 GB (trades) and 9 GB (quotes) per day compressed |
| `tickers`, `ticker_types`, `exchanges`, `conditions`, `market_holidays` | REST | reference data (`conditions`: trade/quote condition codes and how they affect bars) |
| `ticker_details` | REST | daily snapshot per active stock/ADR (~5,700 calls): market cap, share-class and weighted shares (ADS counts for ADRs), SIC; history builds up from the first run |
| `splits`, `dividends`, `ipos` | REST | corporate actions (a shorter answer never deletes stored history) |
| `income_statements`, `balance_sheets`, `cash_flow_statements`, `ratios` | REST | fundamentals |
| `news` | REST | incremental |
| `short_interest`, `short_volume`, `float` | REST | not refreshed by default (stored history stays readable): short data comes from `finra-sensor-array`, float from vinculum's 10-K public float. `subspace backfill rest short_interest` still fetches on request |
| live `bars_1m`, `bars_1s`, `trades`, `quotes`, `luld` | websocket | provisional; old days pruned once official files arrive |

## Storage layout

```
~/market_data/massive/
  bars/day/year=YYYY/part.parquet
  bars/minute/date=YYYY-MM-DD/part.parquet
  ticks/{trades,quotes}/date=YYYY-MM-DD/part.parquet
  reference/  corporate_actions/  fundamentals/  short_interest/  short_volume/  news/
  live/<kind>/date=YYYY-MM-DD/{part-*,hour-*}.parquet
  identity/snapshots/year=YYYY/part.parquet   identity/{security_master,securities,
      security_attributes,splits,dividends,lookups}.parquet
  _catalog/catalog.sqlite   _logs/   _run/
```
