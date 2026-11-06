# subspace-probe

Demo notebooks for every core module of the collective, **one folder per module, named after
the package**. A notebook in a module's folder shows that module's functionality and may use
the packages that module declares as dependencies, nothing further down the graph:
`vinculum/` notebooks may call Massive, EDGAR and FINRA; `massive-subspace-stream/` notebooks
call Massive only; `borg-queen/` notebooks may call unimatrix-zero, neural-processor, vinculum
and Massive. `tools/check_architecture.py` enforces it (rule 6), so a notebook that reaches
outside its module fails the architecture test.

```
notebooks/
  massive-subspace-stream/   Massive (formerly Polygon.io) equity data: bars, ticks, reference data, live stream, security master
  edgar-cortical-node/       SEC EDGAR company facts: point-in-time fundamentals and share counts
  finra-sensor-array/        FINRA short interest and daily short sale volume
  vinculum/                  the join: checked CIKs, market cap, size universes, fundamentals per security, float and short data
  neural-processor/          the signal library and per-date transforms
  unimatrix-zero/            factor sorts, information coefficients, backtests with costs
  borg-queen/                the factor risk model and the cost-aware optimizer
  transwarp-conduit/         execution: order sheets, the simulated broker, reconciliation
  nanoprobe-array/           intraday bars on a session clock, empty bars and halts, auction prices, features, daily intraday summaries
  tactical-cube/             intraday backtests: lagged and auction fills, participation, overnight holds, the look-ahead check
  unicomplex/                `collective status`
src/subspace_probe/          small shared helpers (chart style, timing, byte formatting)
```

Outputs are committed on purpose: the notebooks are meant to be read on GitHub, fully executed.
Re-run a notebook to refresh it after its module changes.

## massive-subspace-stream

| Notebook | What it shows |
|---|---|
| `00_tour.ipynb` | `Store` basics: what is stored, datasets, calendar and sessions, pandas vs polars, lazy scans |
| `01_daily_bars.ipynb` | Price adjustment (raw / splits / total return), point-in-time universe, market-wide history since 2003 |
| `02_minute_bars.ipynb` | Intraday volume and volatility patterns, regular vs extended hours, early closes, overnight vs intraday returns |
| `03_trades_quotes.ipynb` | On-demand tick data: trade sizes and conditions, bid-ask spreads, bars rebuilt from trades |
| `04_reference_fundamentals.ipynb` | Tickers, splits, dividends, IPOs, Massive's financial statements and the point-in-time problem, ratios, news; where short data and float moved |
| `05_realtime.ipynb` | The live stream service: status, today's live data, following new rows as they arrive |
| `06_identity.ipynb` | The security master: renamed and reused tickers, `security_id`, history by security, adjustment and universes that follow the company |

## edgar-cortical-node

| Notebook | What it shows |
|---|---|
| `00_tour.ipynb` | What is stored, point-in-time series and `as_of`, how often numbers are restated, shares outstanding |
| `01_point_in_time_mechanics.ipynb` | Raw facts behind one quarter, quarters derived from year-to-date figures and TTM, first-filed vs restated, the day a number becomes usable, shares and EPS across a split, semi-annual filers, the materialized tables |

## finra-sensor-array

| Notebook | What it shows |
|---|---|
| `00_short_data.ipynb` | Coverage, GameStop's short interest and days to cover, the 7-business-day publication lag and `date_by`, FINRA symbols, most-shorted names and total short interest over time, daily short sale volume and its sources |

## vinculum

| Notebook | What it shows |
|---|---|
| `00_tour.ipynb` | Checked CIKs (repaired and rejected Massive CIKs), share counts across splits and unit errors, ADR and class ratios, market cap history, agreement with Massive, size universes over time, point-in-time valuation ratios |
| `01_fundamentals.ipynb` | The 31 EDGAR fields and how each becomes a panel value (TTM, latest balance sheet, stale after 400 days), coverage by item and year, XBRL tags behind the values, filing lags, first-filed vs restated values, reporting currencies, split-safe TTM EPS, reorganizations; coverage of the tradable universe and the cross-check against Massive's financials |
| `02_ratios.ipynb` | The built-in valuation ratios and ratios of your own over the fundamentals panel; well-known companies side by side, cross-sectional distributions, SIC divisions from EDGAR profiles, the market's valuation and margins since 2012, a daily P/E |
| `03_float_and_short.ipynb` | Public float per 10-K, float fractions and float-adjusted ranks, FINRA short interest per security (GameStop, most shorted, by size) and daily short sale volume |

## neural-processor

| Notebook | What it shows |
|---|---|
| `00_signals.ipynb` | The signal catalogue, a panel of every signal for the top 1,000, coverage, how signals correlate, sector and size neutralization, composites, a first IC screen against next-month returns |
| `01_signals_up_close.ipynb` | The ratio library and its rules, a price signal verified by hand against bars, short-interest signals and the publication lag, growth signals across a split, transforms before and after, one company followed across two securities |

## unimatrix-zero

| Notebook | What it shows |
|---|---|
| `00_factor_sorts.ipynb` | Monthly quintile sorts of the top 1,000 on value, profitability, accruals, size and momentum with forward returns (successors, delistings); long-short performance, information coefficients, and what look-ahead does |
| `01_backtests.ipynb` | Long-short quintiles with execution lag and costs, what each assumption changes, value/quality/momentum/combined, long and short legs, exposures, capacity at $10M-$10B, long-only vs SPY |

## borg-queen

| Notebook | What it shows |
|---|---|
| `00_risk_model.ipynb` | Exposures and their cap-weighted neutrality, factor returns and R², the covariance and style correlations, specific risk by size, forecast against realized risk on test books, attribution, the built-in checks |
| `01_optimizer.ipynb` | Expected returns from a score, one rebalance, optimized against quintile books, capacity, forecast against realized risk and exposures, go / no-go |

## transwarp-conduit

| Notebook | What it shows |
|---|---|
| `00_paper_execution.ipynb` | Settings and limits, the simulated broker and name matching, contract mapping, planning a sheet and its checks, submitting to the simulator with fills and reconciliation, the next rebalance, a read-only look at the paper account (nothing is ever sent to IB) |

## nanoprobe-array

| Notebook | What it shows |
|---|---|
| `00_intraday.ipynb` | The session clock and early closes, empty bars and the 2020-03-09 halt on a grid, the closing auction missing from minute bars, features through a day, the top 500 one day at a time, daily summaries: SPY overnight vs intraday and intraday momentum |

## tactical-cube

| Notebook | What it shows |
|---|---|
| `00_intraday_backtests.ipynb` | One day fill by fill (lag, VWAP, closing auction), costs by time of entry, partial fills of a thin name at size, overnight holds matching the daily engine through the NVDA split, the look-ahead check catching a cheater, P&L by time of day |

## unicomplex

| Notebook | What it shows |
|---|---|
| `00_status.ipynb` | The freshness report, the expectation behind each check, one failing store, JSON output |

## Running

```bash
uv sync --all-packages
uv run jupyter lab projects/subspace-probe/notebooks
```

In VS Code or Cursor, open a notebook and pick the workspace `.venv` kernel. To re-execute a
notebook in place from the shell:

```bash
uv run jupyter nbconvert --to notebook --execute --inplace projects/subspace-probe/notebooks/vinculum/00_tour.ipynb
```

Notes:
- Notebooks read data only through the core packages' public APIs; the architecture checker
  rejects private imports and imports from outside a folder's module and its dependencies.
- Real-time data comes from the stream service (`subspace stream status`). The notebooks
  never open their own websocket, because Massive allows one connection per API key.
- The execution notebook rehearses against `SimBroker` in a temporary folder; its only contact
  with IB Gateway is a read-only account status.
