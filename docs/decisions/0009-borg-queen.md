# 0009 · borg-queen: risk model first, optimizer second (2026-11-03)

## Context
The combined value + quality + momentum book (notebook 06) earns a net Sharpe of about 0.54
but carries uncontrolled sector bets of up to 20% and loses money from $1B, because it is
equal weighted and turns over 11 times a year. Choosing weights that trade expected return
against risk and cost needs a risk model, and the optimizer is only as good as that model, so
the model is built and checked first (part 1); the optimizer (part 2) uses it.

## Decision (part 1)
A core package `borg-queen` on top of unimatrix-zero (return panel; later the cost model),
neural-processor (signals, sectors), vinculum (universes) and massive-subspace-stream.

- **A fundamental factor model, not a statistical one.** Exposures are the signals the
  strategies use, so the optimizer can constrain exactly the bets it should not take (sectors,
  beta, size). 21 factors: market, 12 Fama-French sectors, 8 styles (size, value, momentum,
  volatility, beta, profitability, leverage, liquidity).
- **Monthly exposures, daily returns.** Fundamentals change monthly in neural-processor; price
  styles are refreshed with them. Each day's returns are regressed on the latest month end
  strictly before it, so no forecast sees its own period.
- **WLS with √cap weights and a sector constraint** (cap-weighted sector returns sum to 0), as
  in Barra's US models: the market factor is the market (0.990 daily correlation with SPY).
- **Covariance:** EWMA volatility (half-life 84 days) × EWMA correlation (252) × a volatility
  regime adjustment (42). **Specific risk:** EWMA of squared residuals (84), shrunk 30% toward
  the size decile.
- **Stored as derived Parquet** under `~/market_data/borg_queen`; a build takes about 30 s, so
  there is no incremental update.

## Validation
Bias statistic (realized ÷ forecast risk; 1 is right) from 2013: 1.02-1.15 for every portfolio
in `queen check` (cap-weighted universe, random long-short books, style quintile spreads), 1.05
for notebook 06's combined book (1.00 sector neutral), 1.03 for single-stock specific risk.
Gate passed.

How the settings were chosen (share of 12-month windows with bias in 0.8-1.2):

| volatility / correlation / regime half-lives | market | value spread | beta spread |
|---|---|---|---|
| 84 / 504 / none (first build) | 0.54 | 0.60 | 0.62 |
| 42 / 504 / none | 0.70 | 0.62 | 0.71 |
| 84 / 504 / 42 | 0.62 | 0.76 | 0.80 |
| **84 / 252 / 42 (chosen)** | 0.62 | 0.79 | 0.85 |
| 42 / 252 / 42 | 0.76 | 0.79 | 0.86 |

Shorter volatility half-lives helped the market portfolio but not the level for style spreads;
the chosen row keeps 84 days so that forecasts (and later the optimizer's weights) move less.
The remaining under-forecast is concentrated in 2020-21, when value and momentum moved together
and the value spread realized 1.4-1.5× its forecast.

## Rejected
- **Sample covariance of stocks with shrinkage (Ledoit-Wolf):** 1,000 names and ~250 days per
  window give a noisy, near-singular matrix, and it cannot express sector or style limits.
- **Statistical (PCA) factors:** fit history well but have no meaning to constrain; possibly
  later as a check on the fundamental model.
- **Daily exposures:** more work for little gain at a monthly rebalance.
- **Centring styles equal weighted:** the cap-weighted universe would then carry style bets
  (a small-size tilt), which makes market risk harder to read.

## Decision (part 2, 2026-11-03)
A cost-aware optimizer in the same package (`Optimizer`, `Portfolio`), solved with cvxpy and
Clarabel (about 0.1 s per rebalance).

- **Expected return from the score, Grinold style:** IC (0.03 a month) × z × specific
  volatility. Scaling by specific volatility keeps the implied information ratio per name
  equal; the IC is an assumption, not fitted.
- **Volatility target as a constraint (8% a year), not a risk-aversion coefficient.** A target
  is readable and comparable across AUMs; a coefficient's meaning changes with the alpha scale.
  Risk aversion is available but off by default.
- **The backtest's own cost model in the objective.** The optimizer calls unimatrix-zero's
  `CostModel` (half-spread + commission, square-root impact at the AUM, borrow), so it avoids
  what the engine charges. Impact is a 1.5 power cone; weights are in percent of capital
  because Clarabel reports "optimal_inaccurate" in plain fractions.
- **Hard limits for what the strategy should not bet on:** each sector's net within ±1%, beta
  within ±0.05, size within ±0.1, 2% per name, trades ≤ 10% of ADV. Infeasible problems relax
  step by step (participation, exposures ×3, exposures off) and finally hold the current book.
- **Holdings carried forward:** `Portfolio.build` drifts each month's targets with daily returns
  to the next execution day, as `Engine.run` holds them, so trades happen only when their
  expected gain exceeds their cost.

## Results (part 2, notebook 07)
Notebook 06's combined score, monthly from December 2012, the same CostModel in optimizer and
engine:

| AUM | optimized net return | net Sharpe | turnover / year | quintile net Sharpe | quintile sector-neutral |
|---|---|---|---|---|---|
| none | 5.1% | 0.59 | 9.4 | 0.57 | 0.54 |
| $10M | 4.1% | 0.50 | 6.8 | 0.50 | 0.45 |
| $100M | 3.3% | 0.41 | 4.9 | 0.36 | 0.28 |
| $1B | 3.0% | 0.40 | 2.9 | -0.01 | -0.18 |

Go criteria, set before the run: net Sharpe at $100M ≥ 0.30 (0.41), a positive net return at
$1B (3.0%), sector bets ≤ 1% (1.00% at every rebalance; the quintile book reaches 23%). All
pass with the default settings; nothing was tuned to the result. With no AUM the optimizer adds
control rather than return; its gain is capacity (cost drag about 1.2% a year at every size,
against 6.9% for the quintile books at $1B). All 167 rebalances solved at every AUM without
relaxing. Realized volatility ran above the 8% target (bias 1.13 at $100M against 1.02-1.06
for the quintile books): an optimizer leans toward under-forecast risks. Momentum,
profitability and value earned 2.8%, 1.7% and 1.5% a year; specific returns cost 0.7%.

A Sharpe ratio over 14 years has a standard error near 0.27, so the margin at $100M is not
decisive; the capacity gap at $1B is. A variant score (different value and quality signals)
gave 0.83 / 0.58 at no AUM / $100M against 0.64 / 0.42 for its quintile book: the same pattern
at a higher level.

*Re-run 2026-11-13 with the fitted spreads of decision 0017 and the data and risk model as of
that day:* 4.9% / 0.56 / 9.3× with no AUM (quintile 0.57, sector neutral 0.55), 4.2% / 0.50 at
$10M, 3.3% / 0.41 at $100M, 2.9% / 0.39 at $1B (quintile books -0.01 and -0.17). All three go
criteria still pass. With the former spread rule in both optimizer and backtest the no-AUM
Sharpe is 0.58 on the same data, so most of the move comes from the data, not the costs.

## Next
Execution (`transwarp-conduit`): turn target weights into orders. Possible refinements: an IC
estimated out of sample per signal, more style limits (momentum carries most of the risk), and
weekly rebalances now that a solve is cheap.
