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

## Next (part 2)
An optimizer (cvxpy): expected return from a composite score (Grinold: IC × specific vol ×
score), minus risk aversion × forecast variance, minus the cost unimatrix-zero will charge
(spread and square-root impact); sector, style, position and ADV limits; holdings carried
forward between rebalances. Notebook 07 compares it with the quintile book at $10M-$1B.
