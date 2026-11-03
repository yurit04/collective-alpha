# subspace-probe

Exploratory data analysis of the collective's data sources. Each source gets its own
notebook folder, so new sources can be added without crowding the project.

```
notebooks/
  massive/        Massive (formerly Polygon.io) equity data via massive-subspace-stream
  edgar/          SEC EDGAR company facts via edgar-cortical-node
  vinculum/       the Massive x EDGAR join: market cap, size universes, fundamentals per security
src/subspace_probe/   small shared helpers (chart style, timing; SIC sectors re-exported from neural-processor)
```

## Massive notebooks

| Notebook | What it shows |
|---|---|
| `00_tour.ipynb` | `Store` basics: what is stored, datasets, calendar and sessions, pandas vs polars, lazy scans |
| `01_daily_bars.ipynb` | Price adjustment (raw / splits / total return), point-in-time universe, market-wide history since 2003 |
| `02_minute_bars.ipynb` | Intraday volume and volatility patterns, regular vs extended hours, early closes, overnight vs intraday returns |
| `03_trades_quotes.ipynb` | On-demand tick data: trade sizes and conditions, bid-ask spreads, bars rebuilt from trades |
| `04_reference_fundamentals.ipynb` | Tickers, splits, dividends, IPOs, financial statements (point-in-time), short data, news |
| `05_realtime.ipynb` | The live stream service: status, today's live data, following new rows as they arrive |
| `06_identity.ipynb` | The security master: renamed and reused tickers, `security_id`, history by security, adjustment and universes that follow the company |

## EDGAR notebooks

| Notebook | What it shows |
|---|---|
| `00_tour.ipynb` | What is stored, point-in-time series, how often numbers are restated, shares outstanding, coverage of the tradable universe, cross-check against Massive's financials |

## Vinculum notebooks

| Notebook | What it shows |
|---|---|
| `00_tour.ipynb` | Checked CIKs (repaired and rejected Massive CIKs), share counts across splits and unit errors, ADR and class ratios, market cap history, agreement with Massive, size universes over time, point-in-time valuation ratios |
| `01_fundamentals.ipynb` | The 31 EDGAR fields and how each becomes a panel value (TTM, latest balance sheet, stale after 400 days), coverage by item and year, XBRL tags behind the values, filing lags, first-filed vs restated values, reporting currencies, split-safe TTM EPS, missing vs zero, reorganizations (successors) |
| `02_ratios.ipynb` | The 31 ratios of the neural-processor library (valuation incl. EV/EBITDA and shareholder yield, profitability, quality, leverage); well-known companies side by side, cross-sectional distributions, sector medians, aggregate P/E and margins since 2012, a daily P/E |
| `03_factor_sorts.ipynb` | Monthly quintile sorts of the top 1,000 on value, profitability, accruals, size and momentum with forward returns from unimatrix-zero (successors, delistings); long-short performance, information coefficients, and what look-ahead does to a backtest |
| `04_float_and_short.ipynb` | Public float per 10-K, float fractions and float-adjusted ranks, FINRA short interest (GameStop, most shorted, by size) and daily short sale volume |
| `05_signals.ipynb` | The neural-processor signal library: catalogue, a panel of every signal for the top 1,000, coverage, how signals correlate, sector and size neutralization, composites, a first IC screen |
| `06_backtests.ipynb` | unimatrix-zero backtests: long-short quintiles with execution lag and costs, what each assumption changes, value/quality/momentum/combined, long and short legs, exposures, capacity at $10M-$10B, long-only vs SPY |

## Running

```bash
uv sync --all-packages
uv run jupyter lab projects/subspace-probe/notebooks
```

In VS Code or Cursor, open a notebook and pick the workspace `.venv` kernel.

Notes:
- Notebooks read data only through the core packages' public APIs (`Store`, `EdgarStore`, `Vinculum`).
- Outputs are committed so the notebooks read well on GitHub. Re-run them to refresh.
- Real-time data comes from the stream service (`subspace stream status`). The notebooks
  never open their own websocket, because Massive allows one connection per API key.
