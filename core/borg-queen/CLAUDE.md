# borg-queen (core, depends on unimatrix-zero, neural-processor, vinculum, massive-subspace-stream)

Risk and portfolio construction: a point-in-time factor risk model (part 1) and a cost-aware
optimizer producing weights for unimatrix_zero.Engine.run (part 2).
User docs: README.md in this folder. Design: docs/decisions/0009-borg-queen.md.

## Public API (`from borg_queen import ...`)

| Name | Use |
|---|---|
| `RiskModel(root=None, processor=None, engine=None)` | build, reads, forecasts; see below |
| `RiskSettings` | top, hl_vol, hl_corr, hl_vra, hl_spec, min_obs, shrink (saved in build.json) |
| `FACTORS`, `MARKET`, `STYLES` | factor names (market, 12 FF sectors, 8 styles); style -> signals |
| `build_exposures(panel)` | neural-processor panel -> standardized exposures |
| `bias_stat(bias_frame, window=None)` | bias statistic overall or rolling |
| `ENV_ROOT` | `BORG_QUEEN_DATA_ROOT` (default ~/market_data/borg_queen) |
| `alpha_from_score(panel, col, risk, ic=0.03, horizon_days=21)` | date, security_id, alpha (+ sector, issuer_market_cap) |
| `Constraints(...)`, `Constraints.long_only(**kw)` | optimizer limits (frozen dataclass, fractions of capital) |
| `Optimizer(risk, costs=None, constraints=None)` | `.solve(alpha, on, w0=None, market=None, nav=1.0)` -> `Solution` |
| `Solution` | `.weights` (security_id, weight), `.info` (status, relaxed, alpha, vol, cost, turnover, names, gross, beta, ...) |
| `Portfolio(optimizer, engine=None)` | `.build(alpha, start=None, end=None, lag=1)` -> (weights for Engine.run, diagnostics) |

`RiskModel` methods:
- `build(start="2011-01-01", end=None, settings=None)` -> info (~30 s); `status()`; `check()`
- reads: `months()`, `exposures(on=None)`, `factor_returns(start, end)`, `residuals(start, end)`,
  `factor_cov(on, annualize=False)`, `specific_var(on)` (daily units; month-end model on or before `on`)
- `predict(weights[, on])` -> date, model_date, total_vol, factor_vol, specific_vol, covered, uncovered
- `contributions(weights, on)` -> factor, exposure, factor_vol, contribution (sums to 1 with "specific")
- `attribute(weights)` -> daily total, one column per factor, specific, uncovered
- `bias(weights)` -> date, forecast_vol, realized_vol, days, z_sq_mean

`Optimizer.solve` needs `market` (security_id, adv, vol) whenever the CostModel uses volume
spreads or an AUM; `Portfolio.build` supplies it. Use the same CostModel in the optimizer and in
`Engine.run`, and the same lag.

CLI: `queen build | status | check`.

## Internals (private)

```
_styles.py     STYLES, FACTORS, build_exposures (winsorize, cap-weighted standardize, clip, re-centre)
_estimate.py   design matrix, cross_section (WLS, sector constraint), regressions (month blocks),
               factor_cov (EWMA vol x corr x regime_scale), specific_var (EWMA, size-decile shrink)
_model.py      RiskModel: build/store/read, predict, contributions, attribute, bias; bias_stat
_checks.py     `queen check`: SPY correlation, R2, bias on test portfolios
_optimize.py   alpha_from_score, Constraints, Optimizer (cvxpy + Clarabel, relaxation steps),
               Portfolio (rebalance loop, drift between executions)
cli.py         `queen`
```

Invariants:
- A day's returns use the exposures of the latest month end strictly before it; covariance and
  specific risk at date d use data up to d only. Never let a forecast see its own period.
- Weights for predict/attribute/bias are dated by signal date (month end) and held from the next
  trading day to the next date, as in unimatrix-zero with lag 0.
- Exposures use raw signal signs; the cap-weighted universe has zero style exposure.
- The sector constraint (cap-weighted sector returns sum to 0) keeps market and sectors
  identifiable; never drop it or the market column.
- Variances are daily; only outputs labelled vol are annualized (x 252).
- Everything under the data root is derived: `build` may overwrite it all.
- Optimizer: solver variables are in percent of capital (S = 100) and the impact term sits in
  its own power cone; in plain fractions Clarabel reports "optimal_inaccurate". Keep both.
- Optimizer costs come only from CostModel's public methods (spread_cost, impact_cost,
  borrow_bps), so a cost change in unimatrix-zero reaches optimizer and backtest together.
- An infeasible rebalance relaxes (participation, exposures x3, exposures off) and finally holds
  the current book; never return an empty book silently.
