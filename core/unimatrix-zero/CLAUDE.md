# unimatrix-zero (core, depends on neural-processor, vinculum, massive-subspace-stream)

Point-in-time backtests. User docs: README.md in this folder. Design:
docs/decisions/0008-unimatrix-zero.md.

## Public API (`from unimatrix_zero import ...`)

| Name | Use |
|---|---|
| `Engine(vinculum=None, store=None)` | `.run(weights, start, end, lag=1, costs, delisting, min_price, min_dollar_volume, market="SPY")` -> `Backtest`; `.returns(ids, start, end, delisting)`, `.market(start, end)`, `.forward_returns(panel, lag)`, `.ic(panel, cols, lag)`, `.quantile_returns(panel, col, n, lag)`, `.links()`, `.calendar(start, end)`, `.execution_dates(signal_dates, cal, lag)` (static; the one rule for signal date -> execution date, used by borg-queen too) |
| `Backtest` | `.daily`, `.trades` (with spread_bps, impact_bps, adv, vol), `.weights`, `.market`, `.settings`, `.cost_warnings`, `.data_warnings` (held through a day vinculum's `suspect_splits` flags); `.summary()`, `.by_year()`, `.exposures()` |
| `CostModel`, `NO_COSTS` | commission, spread ("volume" / "estimate" / bps), impact (with `aum`), borrow (GC / hard to borrow), `warn_bps` (cost warnings, default 100) |
| `quantile_weights`, `rank_weights`, `top_n_weights`, `cap_weights` | signal panel -> weights (date, security_id, weight, sector, issuer_market_cap) |
| `daily_returns(bars, links, calendar, delisting)` | the return panel builder |
| `summary`, `by_year`, `exposures`, `stats` | analytics on a daily frame / weights |

## Internals (private)

```
_returns.py    daily returns: successor splice, delisting rows, ADV, vol, Abdi-Ranaldo spread
_weights.py    portfolio builders, orientation by SIGNALS direction, sector neutrality, caps
_costs.py      CostModel
_engine.py     Engine (period loop: rebalance from drifted weights, hold, borrow), screens, cache
_analytics.py  statistics, by year, exposures
```

Invariants:
- Weights are dated by signal date; execution is `lag` trading days later at the close (a signal
  dated on a non-trading day counts from the last trading day on or before it). Never
  let a return on or before the execution date accrue to new weights.
- Weights are fractions of NAV; long-short legs +1 / -1. Positions drift between rebalances,
  and turnover is measured against the drifted weights (two-sided, initial build included).
- The end of the data is the market's last bar, never the portfolio's (a delisted security's
  last day must not look like the end of the data).
- Delisted positions are cash after their delisting row; closing them costs nothing.
- A daily return above +300% is a data break (return 0, `break` set), never P&L.
- A target with no bar on its execution date is dropped (leg rescaled), never "bought".
- `min_price` / `min_dollar_volume` are tradability rules (mandates, capacity), off by default.
  Never use them to hide data errors: those surface in `cost_warnings` / `data_warnings` and
  belong to vinculum.
- Large one-day falls are P&L, never zeroed (most are real: failed trials, frauds). A fall that
  looks like a missing split is listed in `data_warnings` and warned once; the fix belongs in
  the data (Massive's identity overrides), not here.
- Successors: relabel weights and returns with neural_processor.relabel; returns across a link
  come from raw closes.
- Sort before any shift or rolling window: polars joins do not keep row order.
