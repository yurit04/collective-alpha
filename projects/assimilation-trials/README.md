# assimilation-trials

Published intraday effects, re-tested on 5-minute bars with realistic execution and costs
through [`tactical-cube`](../../core/tactical-cube/README.md).

**Goal.** Find out which well-known intraday effects survive trading costs for a $1M book, and
how much each one earns per unit traded (its break-even cost).

**Hypotheses.**
1. Intraday momentum (Gao, Han, Li and Zhou 2018): SPY's return from the previous close to
   10:00 predicts the last half hour.
2. SPY earns its return overnight, not during the session.
3. An opening range breakout on SPY and QQQ follows through to the close.
4. Among the 500 largest stocks, the first half hour's biggest losers beat its biggest winners
   for the rest of the day.

**Execution.** Decisions at a bar's end, fills from the next bar at its VWAP (or in the official
auctions), at most 10% of a bar's volume, flat at the close except for the overnight study.
Costs for $1M: time-of-day half-spreads, square-root impact on the whole order, IB per-share
commission, borrow on overnight shorts. Since 2004 (the reversal since 2010).

## Run

```bash
uv run pytest projects/assimilation-trials
cd projects/assimilation-trials/notebooks
uv run jupyter nbconvert --to notebook --execute --inplace 00_trials.ipynb   # ~13 minutes
```

Parameters: `configs/trials.toml`. Strategies: `src/assimilation_trials/__init__.py`
(`momentum`, `overnight`, `breakout`, `reversal`, `stats_by_period`). Needs the nanoprobe
summaries (`uv run nanoprobe build`).

## Results (2026-11-05)

| Study | Gross /yr | Net /yr | Net t | Turnover /yr | Earns, bp per unit traded | Pays, bp |
|---|---|---|---|---|---|---|
| Momentum, SPY | +1.7% | -3.8% | -3.2 | 504 | 0.36 | 1.10 |
| Overnight, SPY | +7.9% | +3.4% | +1.7 | 504 | 1.64 | 0.85 |
| Intraday, SPY | +2.6% | -1.7% | -0.2 | 504 | 0.71 | 0.85 |
| Breakout, SPY and QQQ | -2.0% | -7.0% | -3.0 | 430 | -0.34 | 1.22 |
| Reversal, top 500 | +2.1% | -14.3% | -12.0 | 504 | 0.44 | 3.46 |

- At $1M, none of the intraday effects pays for its trading. Holding SPY overnight through
  the auctions is the only positive one after costs, and it is weak (t 1.7).
- Momentum's gross return came almost all in 2005-2014 and has been flat since 2015. The
  reversal was strongest in 2015-2019. The breakout has no edge even before costs.
- The costs are modelled, not yet measured (decision 0014 phase 3). For SPY the verdict turns
  on impact: the model charges 0.66 bp for a $1M order. With only the half-spread and
  commission (about 0.12 bp), momentum would keep roughly +1.1%/yr.
- The reversal leaves out names that traded under $1M between the open and 10:00: vinculum's
  top 500 holds a few ADRs that trade mostly at home. (It also held Top Ships in late 2018,
  ranked 22nd on a share count on the wrong basis, since fixed in vinculum.)
