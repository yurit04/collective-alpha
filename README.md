# collective-alpha

Assimilation of multi-asset market data, quantitative signals, and systematic execution algorithms.
Resistance to alpha decay is futile.

An equity alpha research platform for an individual investor: medium-frequency signal research,
backtesting and (eventually) systematic trading. Phase one is the **data layer**: pull everything
the Massive (ex-Polygon.io) account is entitled to and store it as typed Parquet for research.

## Setup

```bash
uv sync                      # creates .venv with Python 3.12 and every dependency
uv run ca config             # show effective settings
uv run ca probe              # what does this API key / S3 key reach?
uv run pytest                # offline unit tests
uv run jupyter lab           # notebooks
```

Secrets live outside the repo:

| what | default path | format |
|---|---|---|
| REST API key | `~/Documents/massive_key.txt` | one line |
| S3 flat-file credentials | `~/Documents/massive_s3_key.txt` | `Access Key ID` / value / `Secret Access Key` / value (dashboard paste), or two lines, or `k=v` |

Override any setting in `config/settings.toml` (copy `config/settings.example.toml`) or via
environment variables prefixed `CA_` (e.g. `CA_DATA_ROOT`, `CA_MAX_WORKERS`).

## Data store

Default root: `~/Documents/market_data/massive`

```
raw/flatfiles/us_stocks_sip/<dataset>/YYYY/MM/YYYY-MM-DD.csv.gz   vendor files, immutable
raw/rest/<table>/<partition>.json.gz                               raw REST pages, immutable
curated/day_aggs/year=YYYY/YYYY-MM-DD.parquet                      unadjusted OHLCV, all US stocks
curated/minute_aggs/year=YYYY/month=MM/YYYY-MM-DD.parquet          1-minute bars incl. pre/post market
curated/<snapshot table>/asof=YYYY-MM-DD/data.parquet              tickers, splits, dividends, ipos, ...
curated/<monthly table>/year=YYYY/month=MM/data.parquet            news, short_interest, short_volume
manifest.sqlite                                                    ledger: every file fetched/converted
logs/ca-YYYY-MM-DD.log
```

Bars carry `ts` (UTC, tz-aware) and `ts_ny` (naive exchange-local) plus a `date` column. Prices are
**unadjusted**; adjust in research code from the `splits` table (see the research template notebook).
`tickers` holds active *and* delisted names so universes can be built point-in-time.

Starter-plan entitlements (from `ca probe`, 2026-09-13): 5 years of history; flat files for day and
minute aggregates; REST reference, corporate actions, news, short interest, short volume, float.
Trades, quotes and financial statements return 403 and are skipped automatically.

## CLI

```bash
ca probe                                  # entitlement report
ca backfill                               # everything back to the horizon (idempotent, resumable)
ca update                                 # incremental daily refresh (cron / launchd it after the close)
ca sync bars minute_aggs_v1 --start 2025-01-01 --end 2025-01-31
ca sync reference [--details]             # tickers, types, exchanges, conditions, holidays [+ per-ticker details/events]
ca sync corporate-actions                 # splits, dividends, ipos
ca sync monthly news|short_interest|short_volume
ca sync fundamentals                      # statements/ratios/float (tier permitting)
ca convert day_aggs_v1                    # (re)convert downloaded raw files lacking parquet
ca coverage minute_aggs_v1                # trading days with no curated file
ca status [--errors]                      # manifest summary + disk free
```

Every sync records what it fetched in `manifest.sqlite`; re-running only fetches what is missing or
changed on the vendor side (size/etag).

## Security master

Tickers are reused and renamed (FB was Meta until 2022-06-08 and an ETF since 2025-06; META was an
ETF until 2022-01-28), so every point-in-time join goes through `security_master`:

```bash
ca sync tickers-pit        # monthly point-in-time snapshots of active tickers (vendor `date=` filter)
ca master build            # bars -> trading episodes -> identity per segment -> security_master + securities
ca master lookup FB        # every security a ticker has referred to
```

* `security_master`: one row per (ticker, validity window) with `security_id`, identity source and attributes.
  `security_id` is the composite FIGI when the vendor has one, else share-class FIGI, else `CIK:<cik>:<ticker>`,
  else `SYM:<ticker>:<first date>` (exchange test symbols such as ZVZZT).
* `securities`: one row per security with tickers used, first/last trade and delisting date.
* Identity comes from the monthly snapshots; when it changes inside one trading episode the exact boundary
  is pinned by bisecting dated ticker-detail lookups (cached under `raw/rest/ticker_details_pit`).
* `collective_alpha.universe.security_master.map_to_security(df, master)` attaches `security_id` to any
  frame with `ticker` and `date`.

## Research access

```python
from collective_alpha.storage.catalog import Catalog
cat = Catalog()                       # DuckDB views over the Parquet tree
cat.tables()
cat.sql("select * from day_aggs where ticker='AAPL' order by date").pl()
cat.day_panel(start, end)             # polars DataFrame
cat.scan("minute_aggs")               # polars LazyFrame
```

## Layout

```
src/collective_alpha/
  config.py            settings (TOML + env)
  calendar.py          NYSE sessions
  cli.py               `ca` entry point
  data/base.py         DataProvider interface (future vendors plug in here)
  data/massive/        auth, rest client, flat-file store, dataset specs, transforms, provider
  storage/             path layout, manifest ledger, parquet writer, DuckDB catalog
  universe/            security master (ticker -> security id over time); universes next
notebooks/inspection/  coverage & quality checks
notebooks/research/    alpha research (start from the template)
tests/                 offline unit tests
```

## Roadmap

1. ~~Security master~~ done. Universe construction and point-in-time daily panel (liquidity filters, delistings, adjustments).
2. Feature / signal library on daily and intraday bars, fundamentals-lite (float, short interest), news sentiment.
3. Backtester: vectorised daily and intraday rebalance, costs, IC/turnover diagnostics, walk-forward.
4. Portfolio construction and risk.
5. Execution: broker adapter, paper trading.

## Data notes (observed 2026-09-13)

* `short_volume` history starts 2024-02-06 on the vendor side; `short_interest` covers the full horizon.
* `news` volume drops from ~18k to ~5k articles/month after mid-2024 (vendor source change, not a sync gap).
* `dividends` includes announced future ex-dates; filter on `ex_dividend_date <= asof` for point-in-time use.
* Minute bars include pre/post-market (04:00–20:00 New York). Filter `ts_ny` for the regular session.
