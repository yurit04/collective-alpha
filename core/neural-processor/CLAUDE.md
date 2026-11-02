# neural-processor (core, depends on vinculum, edgar-cortical-node, massive-subspace-stream)

Point-in-time signals per security over vinculum universes. User docs: README.md in this
folder. Design: docs/decisions/0007-neural-processor.md.

## Public API (`from neural_processor import ...`)

| Name | Use |
|---|---|
| `Processor(vinculum=None, store=None, edgar=None)` | `.panel(signals, start, end, tickers, by, top, size, universe, rank_by, freq="1m"\|"1d", transform, winsor, neutralize, policy, backend)` |
| `SIGNALS` / `Signal` | name -> group, formula, direction (+1/-1), source (fundamental/growth/price/short), items, market, needs_usd, expr, note |
| `GROUPS`, `catalogue()`, `resolve(signals)` | group names; the catalogue as a table; names or groups -> signal names |
| `transform`, `winsorize`, `neutralize`, `composite` | per-date cross-sectional operations on a panel |
| `add_lags(df, cols, freq, links)`, `short_signals(si, keys)` | building blocks used by `panel` |
| `SECTORS`, `sic_sector(sic)` | Fama-French 12 sectors from SIC codes |

`panel` columns: date, security_id, ticker, cik, sector, issuer_market_cap, [rank, size_bucket],
one column per signal (raw, or transformed when asked).

## Internals (private)

```
_signals.py     the catalogue: Signal definitions and polars expressions
_processor.py   Processor.panel: fundamentals, market columns, lags, universe, sectors, price, short
_price.py       daily bars -> price signals (rolling windows, month offsets, market return)
_lineage.py     follow vinculum successors of kind "security" (relabel predecessor rows)
_transforms.py  winsorize, neutralize, transform, composite
_sectors.py     FF12 mapping
```

Invariants:
- Everything per date: transforms and composites never use other dates' rows.
- Fundamentals only through `Vinculum.fundamentals` (point in time); growth lags are rows
  12 month ends earlier (or a year earlier within 10 days for daily panels), never restated.
- Price signals on day d use bars up to d's close; short interest only reports with
  published_date <= d (within 35 days).
- Sort before rolling or shifting: polars joins do not keep row order.
- A signal's `direction` orients composites; never flip a signal's raw sign in the panel.
- New signals: add a `Signal` to `_signals.py` with its items (EDGAR names in
  edgar_cortical_node.CONCEPTS) and a test; price/short signals also need code in their module.
