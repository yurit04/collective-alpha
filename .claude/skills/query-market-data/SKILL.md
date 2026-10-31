---
name: query-market-data
description: How to load stored Massive market data (bars, trades, quotes, fundamentals, short data, news) into pandas or polars, check what is downloaded, and fetch what is missing. Use for any analysis that needs market data.
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
- `timestamp` is the bar start in UTC; `date` is the US/Eastern trading date.
- Point-in-time universe: `store.universe("2010-06-30", types=["CS"])` (includes later delistings).

## Other data
```python
store.trades("AAPL", start="2024-03-01")          # needs: subspace backfill history --dataset trades --start ... --symbols AAPL
store.quotes("AAPL", start="2024-03-01")
store.financials("income", tickers="AAPL", start="2020-01-01")   # filters on filing_date (no look-ahead)
store.short_interest("GME"); store.short_volume("GME"); store.news("TSLA", start="2025-01-01")
store.splits("AAPL"); store.dividends("AAPL"); store.tickers(active=True, types=["ETF"])
store.trading_days("2024-01-01", "2024-12-31"); store.sessions("2024-11-29")
store.scan("bars_minute")                         # lazy polars over everything, for custom queries
```
Every method takes `backend="pandas"` for pandas output.

## When data is missing
- Check `store.coverage()` or `uv run subspace status`.
- Fetch with the CLI (ask the user first for large jobs; see root CLAUDE.md):
  - `uv run subspace backfill history --dataset bars_minute --start 2020-01-01 --end 2020-12-31`
  - `uv run subspace backfill history --dataset trades --start D --end D --symbols AAPL,MSFT`
  - `uv run subspace backfill rest <name>`
- Never read Parquet files by path from project code; the layout is private to the core package.
