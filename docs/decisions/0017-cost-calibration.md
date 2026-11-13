# 0017 · Cost models fitted on measured trades and quotes (2026-11-13)

## Context
Decision 0016 harvested 424 days of Massive trades and NBBO quotes (2003-09 to 2026-11) into
adaptation-matrix before the switch to Starter. Its phase 2 was to replace the assumed costs:
unimatrix-zero's half-spread rule (30 bp × (ADV in $M)^-0.5, set from quoted spreads),
tactical-cube's time-of-day multipliers (3 / 2 / 1.5 in the first half hour), the half-spread
charged on closing fills, and the square-root impact coefficient of 1.

## What the archive shows
2.1M stock-days (common stocks and ADRs with 20+ matched trades and `outside_share` < 0.2),
joined with the inputs the backtester computes (`Engine.returns`: 21-day ADV, 63-day volatility):

| Check | Result |
|---|---|
| Former rule, held out (May-Nov 2026), dollar-weighted predicted/measured | 0.96 under $1M a day, 0.87 at $1-10M, 0.75 at $10-100M, 0.63 at $100M-1B, 0.38 at $1-10B, 0.55 over $10B |
| What explains the effective half-spread | ADV and volatility (R² of logs 0.73 with ADV alone, 0.83 with volatility); price, a one-cent tick floor and Abdi-Ranaldo add nothing |
| Level over the years (same ADV and volatility) | 3.5 bp at $100M a day in 2003, 2.3-2.6 bp in 2009-2013, 2.9-3.4 bp since 2020 |
| A first look that mixed in ETFs | suggested the rule *over*charged small names 2-3×; thin ETFs trade far tighter than stocks of their volume, so they are fitted out |
| The close vs the pre-close mid, 2023-2026 | signed median 0 bp in every size bucket; mean absolute 1-2× the quoted half-spread; auction 12-20% of the day's volume over $10M a day, 9% at $1-10M, 1% under $1M |
| Time of day, stocks over $10M a day, last 60 sessions | 3.7× the day's spread in the first minute, 2.6× to 5, 1.75× to 15, 1.4× to 30, 1.2× to 60, then 1.0 falling to 0.6× in the last minutes; similar for every size and in 2003-2025 |
| Square-root impact from tape flow, 2023-2026 | 0.72-0.99 below $1B a day, 1.1-1.45 above (few days); 0.87 overall |
| SPY, QQQ, IWM | effective half-spreads of 0.12 / 0.16 / 0.21 bp, flat through the day; the stock fit gives its 0.5 bp floor |

## Decision
1. **A fitted spread is the default** (`CostModel(spread="fitted")`, `SPREAD_FIT`): the effective
   half-spread = level(year) × (ADV/$100M)^-0.388 with slope changes of -0.112 above $1M and
   +0.133 above $10M, × (vol/2%)^0.624. A Poisson fit with a level per year: it predicts the
   mean cost (what trades pay on average), not the median, from inputs every plan has. Held out
   on the last six months it is within 5-15% of the measured cost in every ADV bucket,
   dollar-weighted. Each trade pays its own year's level (`spread_cost(at=date)`; years outside
   2003-2026 take the nearest), so a 2010 backtest is not charged 2026 spreads. The former rule
   stays as `spread="volume"`.
2. **The close: an option, not the default.** `CostModel(auction=True)` charges no half-spread on
   fills: market-on-close orders get the official close, which is also the backtest's mark, and
   the close sits a median 0 bp from the pre-close mid. The default keeps charging the half-spread
   at the close, because without an AUM (no impact) an auction backtest would trade thin names
   almost free, and their auctions are 1-9% of daily volume.
3. **Measured time of day in tactical-cube** (`IntradayCosts.tod`): the profile above, as
   multipliers of the fitted daily spread (they average to 1 over a day's trading).
4. **Impact coefficient stays 1.0.** The tape measures 0.7-1.15 for most sizes, consistent with
   1.0 but not precise enough to move it: net flow mixes informed and uninformed trading. Paper
   and live fills (transwarp-conduit phase 2) are the check.
5. **ETFs keep their own spread.** The fit covers stocks; studies of index ETFs pass the ETF's
   measured spread as a number with a flat profile (the intraday trials do).
6. **adaptation-matrix fits, unimatrix-zero holds the numbers.** `adapt calibrate` (or
   `AdaptationMatrix.calibrate()`) builds the inputs from `Engine.returns`, fits, validates and
   writes `_calibration/calibration.json`; it says whether `SPREAD_FIT` still matches. The numbers
   are copied into `unimatrix_zero.SPREAD_FIT` and tactical-cube's `tod` default, so backtests
   need neither the archive nor adaptation-matrix. adaptation-matrix now depends on unimatrix-zero
   (for `Engine.returns` and the comparison); the graph stays acyclic.

## Effect on the intraday trials (decision 0014)
Re-run with these costs (SPY and QQQ at their measured 0.12 / 0.16 bp, flat): SPY momentum's
cost per unit traded fell from 1.10 to 0.91 bp (net -3.8% to -2.9%/yr), the breakout's from 1.22
to 1.03 bp, overnight holding is unchanged at 0.85 bp (auction fills pay no spread either way),
and the top-500 reversal's rose from 3.46 to 3.73 bp (its 10:00 entries pay 1.2-1.4x the fitted
spread). No verdict changed. For SPY, 0.66 of the 0.91 bp is modelled impact, which this
archive cannot pin down at that size.

## Why not
- **A fixed spread per stock from the archive:** measured on 424 days, not on the days a
  backtest trades, and empty for names listed since or delisted before; a formula on ADV and
  volatility covers every security on every day.
- **Fit on the median or on logs:** a trade's expected cost is the mean; the log fit's smearing
  correction depends on the size bucket (residuals are wider for small names).
- **One slope in ADV:** it under-predicted names over $1B a day by half (their spreads flatten
  near the tick); two slope changes fix it without a price term.
- **Charge no spread at the close by default:** correct for small MOC orders, but the default must
  stay safe for backtests without an AUM.
