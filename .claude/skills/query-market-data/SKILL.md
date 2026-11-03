---
name: query-market-data
description: How to load stored market data (Massive bars, trades, quotes, news; FINRA short interest and short sale volume; SEC EDGAR point-in-time fundamentals and share counts; vinculum market cap, size universes and fundamentals per security) into pandas or polars, check what is downloaded, and fetch what is missing. Use for any analysis that needs market or fundamental data.
---

# Querying market data

```python
from massive_subspace_stream import Store
store = Store()                      # ~/market_data/massive
store.coverage()                     # what is stored (check this first)
```

## Bars
```python
store.bars(["AAPL", "MSFT"], start="2015-01-01", end="2024-12-31")             # daily, raw prices
store.bars("AAPL", start="2015-01-01", adjust="splits")                          # split-adjusted
store.bars("KO", start="2015-01-01", adjust="all")                               # total return
store.bars("SPY", start="2024-06-03", end="2024-06-07", freq="1min", session="regular")
store.bars(start="2024-06-03", end="2024-06-03", freq="1min", lazy=True)         # all tickers, lazy
```
- Raw prices are as traded. Use `adjust=` for anything spanning splits or dividends.
- Minute bars leave out the closing auction print; take daily volume and the official close
  from daily bars.
- `timestamp` is the bar start in UTC; `date` is the US/Eastern trading date.
- Point-in-time universe: `store.universe("2010-06-30", types=["CS"])` (includes later delistings).

## Companies, not tickers (security master)
Tickers are renamed and reused (`FB` = Meta until 2022-06-08, an unrelated ETF since 2025-06;
`META` = a Roundhill ETF in 2021-22). Every bar row has a `security_id`; follow companies by it.
```python
store.bars("META", start="2012-01-01", by="security", adjust="all")  # Meta's full history (FB + META)
store.resolve(["FB"], on="2015-06-30")      # which security a ticker meant on a day
store.securities(tickers="FB")              # all securities that used a ticker, with all their tickers
```
- Group, join and de-duplicate by `security_id`, never by `ticker`, in anything spanning time.
- `by="security"` resolves tickers as of `end` (default today): use the current ticker or an id.
- If `security_id` is missing, the master is not built: ask the user before running
  `uv run subspace identity build` (first run is a 10-20 minute download).

## Other data
```python
store.trades("AAPL", start="2024-03-01")          # needs: subspace backfill history --dataset trades --start ... --symbols AAPL
store.quotes("AAPL", start="2024-03-01")
store.financials("income", tickers="AAPL", start="2020-01-01")   # filters on filing_date (see caveat below)
store.news("TSLA", start="2025-01-01")
store.splits("AAPL"); store.dividends("AAPL"); store.tickers(active=True, types=["ETF"])
store.trading_days("2024-01-01", "2024-12-31"); store.sessions("2024-11-29")
store.scan("bars_minute")                         # lazy polars over everything, for custom queries
store.live("bars_1m", "SPY", start="2026-11-02")  # live capture (stream service must be running)
for batch in store.follow("bars_1m", ["SPY"], timeout=300): ...   # new live rows as they land
```
Every method takes `backend="pandas"` for pandas output.

Massive financials caveat: each fiscal period is dated by the *latest* filing that reported it
(often a year after the original) and may be restated. For backtests use EDGAR (below).

## Point-in-time fundamentals and shares (SEC EDGAR)
```python
from edgar_cortical_node import EdgarStore, CONCEPTS
edgar = EdgarStore()
u = store.universe("2020-06-30", types=["CS"])                # point-in-time CIK per traded ticker
cik = u.filter(pl.col("ticker") == "AAPL")["cik"][0]
ciks = u["cik"].drop_nulls().to_list()
edgar.as_of("2020-06-30", ciks=ciks, items=["revenue", "net_income", "equity"])  # knowable that day
edgar.series("revenue", ciks=[320193])     # quarters (+ttm), halves, years; filed / available_from
edgar.shares([320193])                     # shares outstanding history (as reported, not split-adjusted)
edgar.profiles([320193]); edgar.names([1326801])   # SIC, fiscal year end, tickers; name history
```
- Join to bars through the security master's `cik` (point in time), never by ticker; or use
  vinculum (below), which also checks the CIK against the SEC's own name history.
- Items: `CONCEPTS` (31): income statement (revenue, cost_of_revenue, gross_profit, sga, rnd,
  depreciation_amortization, operating_income, interest_expense, pretax_income, income_tax,
  net_income), cash flow (operating_cash_flow, capex, dividends_paid, buybacks, stock_comp),
  balance sheet (assets, current_assets, cash, receivables, inventory, goodwill, liabilities,
  current_liabilities, debt_current, short_term_borrowings, long_term_debt, equity), eps_basic,
  eps_diluted, public_float. `EdgarStore.concepts()` gives each item's tags and definition.
- Values are usable from `available_from` (day after filing); default is first-filed values.
- Not split-adjusted (shares, EPS): use vinculum for per-security, split-aware values.
- Coverage starts 2009-2011 (XBRL); foreign filers from 2014-15; many funds/BDCs have none.
- Refresh: `uv run cortical sync` (the first `cortical backfill` is a 1.4 GB download: ask first).

## Short interest and short sale volume (FINRA)
```python
from finra_sensor_array import FinraStore
f = FinraStore()
f.short_interest("GME", start="2020-01-01")               # twice monthly from 2017-12-29
f.short_volume("GME", start="2015-01-01")                 # daily off-exchange, from 2009-08-03
```
- Use `published_date` for signals (7 business days after `settlement_date`); daily short volume
  is usable from the next trading day. Fetch with `uv run sensor backfill short-interest|short-volume`
  (ask first for the short-volume history). Prefer these to Massive's `short_interest()` /
  `short_volume()` (same numbers, shorter history, no publication date).
- Rows are keyed by FINRA symbols (BRKB, BRK/B). Per security, use vinculum instead:
  `v.short_interest("GME", start=...)` (with si_pct_market_cap, si_pct_float) and
  `v.short_volume("AAPL", start=...)` (short_ratio, off_exchange_share).

## Market cap, size universes, fundamentals per security (vinculum)
Use this instead of joining EDGAR to bars yourself: it checks CIKs, applies splits, ADR ratios
and share classes, and fixes unit errors in filings.
```python
from vinculum import Vinculum
v = Vinculum()
v.market_cap("AAPL", start="2015-01-01")                 # daily, point in time
v.market_cap("META", start="2012-01-01", by="security")  # across renames
v.universe("2015-06-15", top=500)                         # ranked on the previous month end
v.universe("2015-06-15", size="small")                    # large 1-200, mid 201-1000, small 1001-3000, micro 3001+
v.ranks("2020-12-31")                                     # one row per company (GOOG/GOOGL once)
v.ranks("2020-12-31", by="float_market_cap")              # float-adjusted (10-K public float)
v.public_float("WMT")                                     # per 10-K: float, fraction, status
v.fundamentals(["revenue", "net_income", "equity"], start="2015-01-01", freq="1m")  # TTM flows + ratios
v.fundamentals(["eps_diluted"], tickers="NVDA")           # TTM EPS in that day's share units (split-safe)
v.successors()                                            # same company under a new security / CIK
v.scan()                                                  # lazy polars over the whole table
```
- Companies that reorganize into a holding company (XOM 2026, BLK 2024) continue under a new
  security_id: `v.successors()` links them; market cap and fundamentals carry over, but a
  return series by `security_id` must be linked through it.
- `issuer_market_cap` + `is_primary` for company-level work (ranks, size); `market_cap` per class.
- No market cap before XBRL (2009-2011; foreign filers about 2017-18); none for delisted ADRs.
- Float columns (`float_fraction`, `float_market_cap`, `issuer_float_market_cap`) come from the
  10-K public float: annual, none for foreign filers; prefer them to Massive's `float` dataset,
  which has no history.
- Ratios only for USD reporters (`currency` column). Check `cik_status` / `basis` when in doubt.
- Rebuilt nightly (`vinculum build`, ~30 s); `vinculum check` runs the regression checks.

## When data is missing
- Check `store.coverage()` or `uv run subspace status`.
- Fetch with the CLI (ask the user first for large jobs; see root CLAUDE.md):
  - `uv run subspace backfill history --dataset bars_minute --start 2020-01-01 --end 2020-12-31`
  - `uv run subspace backfill history --dataset trades --start D --end D --symbols AAPL,MSFT`
  - `uv run subspace backfill rest <name>`
- Never read Parquet files by path from project code; the layout is private to the core package.

## Signals per security (neural-processor)
```python
from neural_processor import Processor, SIGNALS, catalogue, composite, neutralize
p = Processor()
s = p.panel(["fcf_yield", "roe", "momentum_12_1"], start="2015-01-01", top=1000)   # month ends
s = p.panel("value", start="2015-01-01", size="mid", transform="zscore", neutralize=["sector"])
composite(s, {"fcf_yield": 1, "roe": 1, "momentum_12_1": 1})   # oriented by SIGNALS[...].direction
catalogue()                                                     # every signal's formula and direction
```
- Use these definitions instead of writing ratios by hand; add new signals to the library.
- Point in time, survivorship-free universes, successors followed. Price signals read daily
  bars for the whole universe: request only the signals a study needs.

## Backtests (unimatrix-zero)
```python
from unimatrix_zero import Engine, CostModel, quantile_weights
w = quantile_weights(s, "fcf_yield", q=0.2)          # s: a neural-processor panel
bt = Engine().run(w)                                  # next-day close, costs, delistings, successors
bt.summary(); bt.by_year(); bt.exposures()
```
- Use the engine for any strategy return; quick screens: `Engine().ic(...)`, `.quantile_returns(...)`.
