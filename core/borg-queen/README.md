# borg-queen

Risk and portfolio construction: **a point-in-time factor risk model** over vinculum's top
companies (part 1), and **a cost-aware optimizer** whose weights feed `unimatrix_zero.Engine.run`
(part 2).

```python
from borg_queen import RiskModel, bias_stat

rm = RiskModel()                     # reads ~/market_data/borg_queen (BORG_QUEEN_DATA_ROOT)
rm.build("2011-01-01")               # ~30 s: exposures, factor returns, specific risk
rm.predict(weights)                  # weights: date, security_id, weight -> annualized vol per date
rm.contributions(w_one_date, on)     # share of variance per factor and specific
rm.attribute(weights)                # daily return: exposure x factor return, specific
b = rm.bias(weights); bias_stat(b)   # forecast against realized risk (~1 when right)
rm.factor_returns(); rm.factor_cov("2026-10-30", annualize=True); rm.exposures("2026-10-30")
```

CLI: `queen build [--start 2011-01-01] [--top 1000] | status | check`.

```python
from borg_queen import Constraints, Optimizer, Portfolio, alpha_from_score
from unimatrix_zero import CostModel

alpha = alpha_from_score(panel, "combined", rm)      # expected return per name and month end
cm = CostModel(aum=1e8)                              # the same costs in optimizer and backtest
w, diag = Portfolio(Optimizer(rm, cm, Constraints())).build(alpha)   # ~25 s for 14 years monthly
bt = rm.engine.run(w, costs=cm)
Optimizer(rm, cm).solve(alpha_one_date, on, current_weights, market)  # one rebalance
```

## The model

- **Universe:** vinculum's top 1,000 companies at each month end (primary class,
  survivorship free), from neural-processor.
- **Factors (21):** market, the 12 Fama-French sectors, and 8 styles built from
  neural-processor signals:

  | style | signals |
  |---|---|
  | size | log issuer market cap |
  | value | book/market, earnings yield, cash-flow yield |
  | momentum | 12-1 month return |
  | volatility | log 3-month volatility |
  | beta | 12-month beta |
  | profitability | gross profitability, ROE |
  | leverage | liabilities / assets |
  | liquidity | log turnover |

  Each signal is clipped at its 1st/99th percentile per date and standardized: (x − cap-weighted
  mean) / standard deviation. A style averages its signals and is standardized again, clipped at
  ±3, and re-centred, so the cap-weighted universe has zero exposure to every style. A missing
  style is 0, a missing sector "Other". Exposures use raw signs (high value = cheap, high
  volatility = volatile), not signal directions.
- **Factor returns:** each trading day, returns are regressed on the exposures of the latest
  month end before that day, weighted by √market cap, with the cap-weighted sector returns
  constrained to sum to zero. The market factor is then the market, and sectors are relative to
  it. Returns beyond 5 robust standard deviations are clipped for the fit only; delisting rows
  and data breaks (unimatrix-zero) are left out. Residuals are the specific returns.
- **Factor covariance (daily):** EWMA volatilities (half-life 84 trading days) × EWMA
  correlations (252), × a volatility regime adjustment (42): the recent average of squared
  factor returns over their forecast variance, as in Barra USE4. It raises forecasts when
  factors have been moving more than forecast.
- **Specific variance:** EWMA of squared residuals (half-life 84, at least 42 days), shrunk 30%
  toward the cap-weighted mean of its size decile; a name with fewer days gets the decile's value.
- Forecasts are annualized (× 252). `RiskSettings` holds every parameter; they are saved in
  `build.json`.

## How good it is (build of 2026-11-03, `queen check`)

- The market factor's daily correlation with SPY is 0.990. Median daily R² is 0.33 (0.24 in
  2017, 0.50 in 2022).
- Bias statistic (realized ÷ forecast risk, from daily returns standardized by the forecast of
  the month end before; 1 is right, 0.8-1.2 passes) from 2013:

  | portfolio | bias | 12-month windows in 0.8-1.2 |
  |---|---|---|
  | cap-weighted universe | 1.08 | 62% |
  | random 50+50 long-short | 1.10 | 75% |
  | random 2+2 long-short | 1.02 | 77% |
  | style quintile spreads | 1.05-1.15 | 66-89% |
  | notebook 06 combined book (as built / sector neutral) | 1.05 / 1.00 | |
  | single stocks, specific risk | 1.03 | |

- Risk is under-forecast in sudden regime changes: in 2020-21 the value spread realized 1.4-1.5×
  its forecast as value and momentum moved together. Shorter half-lives did not fix the level;
  the regime adjustment and the 252-day correlation half-life helped most (0.54 → 0.62 of
  12-month windows inside for the market, 0.60 → 0.79 for value).

## The optimizer

- **Expected return** (`alpha_from_score`, Grinold): IC × z × specific volatility over the
  horizon, where z is the score standardized per date (clipped at ±3) and the specific
  volatility comes from that month's risk model. IC defaults to 0.03 a month.
- **Objective** at each rebalance: expected return − cost − borrow (− risk aversion × forecast
  variance, off by default). Cost is unimatrix-zero's `CostModel`: half-spread + commission per
  unit traded and, with an AUM, square-root impact (daily vol × √(trade / ADV)), so the
  optimizer avoids exactly what the backtest will charge. Borrow is charged on shorts.
- **Constraints** (`Constraints`, defaults): long-short with net 0 and gross 2; forecast
  volatility ≤ 8% a year; each sector's net weight within ±1%; predicted beta to the
  cap-weighted universe within ±0.05; size exposure within ±0.1 (more styles can be added);
  ≤ 2% per name; each trade ≤ 10% of ADV (with an AUM). `Constraints.long_only()` gives a fully
  invested long-only book with no exposure limits.
- **When infeasible** it relaxes step by step: drop the participation limit, triple the
  exposure limits, drop them; if nothing works it holds the current book (`relaxed` in the
  diagnostics says which). Names outside the model's universe are sold; names with no volume
  cannot be costed and are sold too.
- **`Portfolio.build`** rebalances at each alpha date, executing `lag` trading days later as
  `Engine.run` does: each solve starts from the previous targets drifted with daily returns.
  Diagnostics per date: status, relaxed, alpha, forecast vol, expected cost, turnover, names,
  gross, beta, nav, seconds.
- Solved with cvxpy and Clarabel, about 0.1 s per rebalance for 1,000 names.

### Results (notebook 07, notebook 06's combined score, 2013-2026)

| AUM | optimized net return / Sharpe / turnover | quintile net Sharpe | quintile sector-neutral net Sharpe |
|---|---|---|---|
| none | 5.1% / 0.59 / 9.4× | 0.57 | 0.54 |
| $10M | 4.1% / 0.50 / 6.8× | 0.50 | 0.45 |
| $100M | 3.3% / 0.41 / 4.9× | 0.36 | 0.28 |
| $1B | 3.0% / 0.40 / 2.9× | -0.01 | -0.18 |

Small, it matches the quintile books while holding sectors to 1% (quintile: up to 23%); large,
it keeps earning where they lose money. Every rebalance solved without relaxing. Realized
volatility ran about 10% above the 8% target (bias 1.13 at $100M).

## Files (data root)

`exposures.parquet` (month ends), `factor_returns.parquet` (daily: one column per factor, r2,
n), `residuals.parquet` (date, security_id, ret, resid), `specific.parquet` (month ends),
`build.json`. All derived: `build` rewrites them.

## Limits

- Exposures change only at month ends; daily price styles (beta, volatility) lag up to a month.
- `attribute` and `bias` hold weights constant between their dates (no drift).
- The optimizer's limits apply at each rebalance; between rebalances holdings drift.
- The IC is an assumption: the optimizer trusts expected returns as given.
- The estimation universe is the top 1,000: a name outside it has no exposures or specific risk
  (`predict` reports its weight as `uncovered`).
- No currency factors; ADRs are treated as US stocks.
