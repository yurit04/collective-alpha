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

## Universes

Named, point-in-time universes are defined in `config/universes.toml` and built with:

```bash
ca universe build all          # or a single name
ca universe stats liquid_1500  # members and turnover per rebalance
```

Rules (all evaluated with data through the *previous* close):

* eligibility from point-in-time attributes: type in (CS, ADRC), primary exchange in (NYSE, Nasdaq, NYSE American)
* last close >= `min_price`, average dollar volume over `adv_window` sessions >= `min_adv`,
  at least `min_history_days` sessions of history, traded within the last 5 sessions
* optional `one_class_per_issuer`: keep the most liquid share class per CIK
* ranked by average dollar volume; enter at rank <= `top_n`, stay while rank <= `exit_n` (hysteresis)
* monthly rebalance on the first session; membership frozen in between except delistings, which drop
  the security the day after its last trade

Output: `curated/universes/name=<name>/year=YYYY/data.parquet` with one row per (date, security_id)
plus rank, lagged ADV and close, rebalance date and an `is_new` flag. Query via the `universes` view
(`where name = 'liquid_1500'`), or `collective_alpha.universe.universes.load_universe`.

## SEC company facts (second data provider)

Point-in-time shares outstanding and basic fundamentals come from SEC EDGAR XBRL company facts,
behind the same provider interface as Massive:

```bash
ca sync sec-facts                 # downloads the nightly companyfacts.zip (~1.4 GB) once, then parses
                                  # every CIK present in `securities` -> curated/sec_facts/asof=<date>
ca sync sec-facts --source api    # per-CIK API instead (8 req/s), for small incremental refreshes
```

* SEC's fair-access policy requires a user agent with a contact e-mail. The default is a
  placeholder; set `sec_user_agent` in `config/settings.toml` or `CA_SEC_USER_AGENT` to your own contact.
* `sec_facts` is a long table (cik, concept, unit, start, end, val, filed, form, ...). Concepts kept are
  listed in `data/sec/facts.py`; every filing is retained so values can be used as-of their *filed* date.
* Share counts for market cap (`universe/marketcap.py`): one concept per issuer, the one it reports in
  the most filings (ties: cover-page `shares_outstanding` > balance-sheet `common_shares` >
  `wavg_shares_basic`), so units never mix across filings. A count is usable on date D only if filed on
  or before D and its period end is within 400 days. Values more than 20x away from the issuer's latest
  filing are dropped as filing errors.
* ADRs: SEC filers report ordinary shares while the price is per depositary share. The vendor's current
  ADS count calibrates a static ADS ratio (snapped to a round number when within 15%) which is applied to
  the whole history; domestic multi-class issuers use the SEC total across classes.
* Cap-ranked universes (`cap_1000`, `cap_3000`) use `rank_by = "cap"`; issuers without a usable share
  count are excluded rather than guessed.

## Daily panel

`ca panel build` writes `curated/panel/year=YYYY` (view `panel`): one row per (security_id, date) with
unadjusted bars plus split/dividend-aware fields:

* `split_ratio` (new shares per old, effective that session), `dividend` (USD cash per share going ex)
* `ret` total return `(close * split_ratio + dividend) / prev_close - 1`; `ret_px` without dividends
* `adj_close` split-adjusted close on today's share basis; `tr_index` total-return index per security
* `gap_sessions` sessions since the previous bar; `is_last_trade` marks the final bar (delisting or data end)
* `suspect_split` flags whole-day price level shifts (>= 2.5x, both open and close) with no split on record:
  reverse splits the vendor missed. Drop or winsorise those returns in research code.

Cash dividends larger than 50% of the previous close are treated as data errors and dropped.
Load with `collective_alpha.panel.build.load_panel(start, end, universe="liquid_1500")`, and pivot to a
date x security matrix with `to_wide(panel, "ret")`. `ca panel check` prints a sanity report and the
largest absolute returns.

## Features and signals

Feature groups are materialised under `curated/features/<group>/year=YYYY` (views `features_<group>`).
Every value at (security_id, date) uses only information known by that session's close.

```bash
ca features build all        # or one of: price size fund short news
ca features list
```

| group | examples |
|---|---|
| price | `ret_{1,5,21,63,126,252}d`, `mom_12_1`, `vol_21d`, `vol_63d`, `parkinson_21d`, `max_ret_21d`, `dist_52w_high`, `gap_overnight`, `ret_intraday`, `adv_21d`, `amihud_21d`, `volume_ratio_21d`, `suspect_split_252d` |
| size | `shares` (SEC, as-of filed date), `cap`, `log_cap`, `turnover_21d` |
| fund | TTM `revenue_ttm`, `net_income_ttm`, `cfo_ttm` (Q4 derived from the 10-K), `equity`, `assets`, `earnings_yield`, `book_to_market`, `sales_to_price`, `cfo_yield`, `roe`, `asset_growth`, `accruals`, `leverage` |
| short | `si_shares`, `si_ratio`, `si_days_to_cover`, `si_change` (settlement + 10-day publication lag), `sv_ratio`, `sv_ratio_5d`, `sv_ratio_21d` |
| news | `news_count`, `news_count_5d`, `news_count_21d`, `news_sent_5d`, `news_sent_21d` (articles after 16:00 New York count toward the next session) |

Load with `collective_alpha.features.base.load_features(["price", "size"], universe="liquid_1500")`.
Cross-sectional transforms live in `features/signals.py`: `cs_rank`, `cs_zscore` (winsorised),
`neutralize` (demean within a group such as a sector), `combine` (weighted z-score sum), `lag`.

## Signal evaluation

```bash
ca eval forward                                   # curated/forward_returns: close-to-close over 1..63 sessions,
                                                  # next-day open->close and close->open; delisting handled
ca eval feature mom_12_1 --universe liquid_1500 --horizon 21
ca eval feature ret_5d --sign -1 --horizon 5      # short-term reversal
ca eval feature earnings_yield --universe cap_1000 --horizon 21 --neutralize-by primary_exchange
```

`collective_alpha.eval` computes, for any long frame (security_id, date, signal): per-date rank IC with
mean, IR, t-stat (scaled for overlapping horizons) and hit rate; IC decay across horizons; equal-weight
quantile returns and the top-minus-bottom spread with annualised return, vol, Sharpe and drawdown;
signal rank autocorrelation and top/bottom quantile turnover; IC by year; rolling walk-forward windows
for model-based signals. `evaluate(signal, fwd)` returns a `SignalReport` with `to_markdown()` / `to_dict()`.

Forward returns are what a position opened at the signal date's close earns. If a security stops
trading inside the horizon the return compounds to its last bar and then a configurable
`delist_return` (default 0). Rows too close to the end of the store are null, never truncated.

## Backtesting

```bash
ca backtest feature mom_12_1 --universe liquid_1500 --rebalance 21            # long-short deciles, monthly
ca backtest feature ret_5d --sign -1 --rebalance 5 --scheme signal_weighted    # rank-weighted book, weekly
ca backtest feature si_ratio --sign -1 --impact --half-spread-bps 5             # with sqrt impact on ADV
```

`collective_alpha.backtest` is a vectorised daily engine over target weights:

* **weights**: `long_short_quantiles` (equal-weight top vs bottom quantile, dollar neutral, or long-only),
  `signal_weighted` (proportional to demeaned rank), rebalanced every n sessions or monthly/weekly, with
  a per-name cap
* **timing**: a target set on signal date D executes at the close `delay` sessions later (default 1) and
  first earns the following session; weights drift with prices between rebalances; a security's last bar
  is followed by liquidation at `delist_return`
* **costs**: commission + half-spread + slippage in bps on traded notional, an annual borrow rate on the
  short leg, and an optional square-root impact term on ADV participation
* **output**: daily gross/net returns, cost, borrow, turnover, gross/net exposure and name counts;
  `summary()` (annualised return, vol, Sharpe, max drawdown, Calmar, cost drag) and `by_year()`

Sanity on the store (liquid_1500, 2022-2026, default costs): 12-1 momentum deciles rebalanced monthly earn
~13% net with Sharpe ~0.45 and 1.1% cost drag; 5-day reversal rebalanced weekly loses money after ~9% of
annual costs.

## Portfolio construction and risk

```bash
ca portfolio feature mom_12_1 --universe liquid_1500 --vol-target 0.10                 # heuristic (default)
ca portfolio feature mom_12_1 --method mvo --vol-target 0.10 --turnover-penalty 0.002  # mean-variance (cvxpy)
```

* **risk model** (`portfolio/risk.py`): statistical factor model refit at every rebalance on a trailing
  window (default 252 sessions): PCA factors (default 10) plus diagonal idiosyncratic variance with a
  shrinkage floor, and each name's beta to the equal-weight universe. Gives predicted portfolio vol,
  factor exposures and beta.
* **sectors** (`portfolio/sectors.py`): Fama-French 12 industries from SIC codes; `Unknown` when the
  vendor has none (about a fifth of names, mostly delisted).
* **heuristic** construction: demeaned cross-sectional rank, sector-demeaned, per-name cap, per-leg scaling
  to gross/net (iterated so caps and sector neutrality hold together), optional ADV participation cap,
  then scaled down to a volatility target using the risk model.
* **mvo** construction: maximise alpha'w - lambda w'Sigma w - tau |w - w_prev|_1 subject to net, gross,
  per-name cap, sector bands, beta band and liquidity caps, solved with Clarabel; falls back to the
  heuristic if the solver fails.
* `portfolio/run.py` runs the rebalance loop, hands targets to the backtest engine and reports
  exposures per rebalance (names, gross, net, predicted vol, beta, turnover, max weight, max sector).

Note: predicted vol from the statistical model runs below realised vol (roughly half on liquid_1500);
size the vol target accordingly or use it as a relative rather than absolute control.

## Execution and paper trading

Strategies are TOML files in `config/strategies/` (signals as feature weights, universe, rebalance
schedule, portfolio settings, capital, broker). The pipeline runs once per session after the store
is refreshed:

```bash
ca trade run momentum_ls                 # fill pending orders at today's open, mark at the close,
                                         # rebalance if due (orders execute at the next open)
ca trade replay momentum_ls --start 2026-06-01 --end 2026-09-11   # seed / audit a paper book
ca trade status momentum_ls
ca trade export momentum_ls --path orders.csv   # pending orders for manual execution at a broker
```

* `execution/orders.py`: orders, fills, position accounting, target weights -> integer share orders
  (closes names that left the targets, skips trades below `min_notional`)
* `execution/broker.py`: `Broker` interface and `PaperBroker`, a SQLite ledger under
  `<data_root>/paper/<strategy>.sqlite` with positions, orders, fills and a daily NAV series; fills at
  the next open with slippage and commission
* `execution/strategy.py`: strategy config, signal from one or several features (z-scored and combined),
  target weights for a date via the portfolio module and a risk model fitted on trailing returns
* `execution/pipeline.py`: session runner, replay, status, CSV export
* `scripts/daily_update.sh` chains the nightly refresh: update -> master -> universes -> panel ->
  features -> forward returns -> every strategy in `config/strategies/`

Live broker adapters are not implemented; `broker = "paper"` is the only value. The CSV export is the
bridge to a real account until one is added.

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
  data/sec/            EDGAR client, companyfacts parser, provider
  storage/             path layout, manifest ledger, parquet writer, DuckDB catalog
  universe/            security master, point-in-time attributes, universe builder, market cap
  panel/               corporate-action cleaning, daily panel with returns
  features/            feature groups (price, size, fund, short, news) and signal transforms
  eval/                forward returns, IC / quantile / turnover diagnostics, signal reports
  backtest/            weight schemes, vectorised daily engine with costs, feature backtests
  portfolio/           sectors, statistical risk model, heuristic and mean-variance construction
  execution/           orders, paper broker ledger, strategy configs, daily trading pipeline
notebooks/inspection/  coverage & quality checks
notebooks/research/    alpha research (start from the template)
tests/                 offline unit tests
```

## Roadmap

1. All planned layers are in place: security master, universes, SEC feed, panel, features, evaluation, backtester, portfolio construction, paper execution. Next candidates: a live broker adapter, intraday features from minute bars, more fundamental concepts.
2. Feature / signal library on daily and intraday bars, fundamentals-lite (float, short interest), news sentiment.
3. Backtester: vectorised daily and intraday rebalance, costs, IC/turnover diagnostics, walk-forward.
4. Portfolio construction and risk.
5. Execution: broker adapter, paper trading.

## Data notes (observed 2026-09-13)

* `short_volume` history starts 2024-02-06 on the vendor side; `short_interest` covers the full horizon.
* `news` volume drops from ~18k to ~5k articles/month after mid-2024 (vendor source change, not a sync gap).
* `dividends` includes announced future ex-dates; filter on `ex_dividend_date <= asof` for point-in-time use.
* Minute bars include pre/post-market (04:00–20:00 New York). Filter `ts_ny` for the regular session.
