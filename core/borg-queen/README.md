# borg-queen

Risk and portfolio construction. **Part 1 (built): a point-in-time factor risk model** over
vinculum's top companies. Part 2 (planned): a cost-aware optimizer whose weights feed
`unimatrix_zero.Engine.run`.

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

## Files (data root)

`exposures.parquet` (month ends), `factor_returns.parquet` (daily: one column per factor, r2,
n), `residuals.parquet` (date, security_id, ret, resid), `specific.parquet` (month ends),
`build.json`. All derived: `build` rewrites them.

## Limits

- Exposures change only at month ends; daily price styles (beta, volatility) lag up to a month.
- `attribute` and `bias` hold weights constant between their dates (no drift).
- The estimation universe is the top 1,000: a name outside it has no exposures or specific risk
  (`predict` reports its weight as `uncovered`).
- No currency factors; ADRs are treated as US stocks.
