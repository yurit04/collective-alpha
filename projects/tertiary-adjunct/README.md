# tertiary-adjunct

A cross-sectional US equity strategy rebalanced twice a month, run with $100,000. Two forms are
tested: **long-short** (market neutral) and **long-only against SPY**. It is built from
`neural-processor` signals, the `borg-queen` risk model and optimizer, and `unimatrix-zero`
backtests with the measured costs. A form that passes goes to paper trading through
`transwarp-conduit`.

**Goal.** One documented strategy and a go/no-go decision made by rules written down first.

## Protocol (fixed 2026-11-21, before any result of this project)

Every number is in [`configs/protocol.toml`](configs/protocol.toml). It was committed before
the first run, and the git history shows that. Anything changed later goes under "Deviations".

**What was already known.** The `subspace-probe` notebooks have shown a monthly, equal-group
combination of value, quality and momentum on the top 1,000 for 2013-2026 (optimized net
Sharpe 0.56 with no AUM). Composite C1 below is that score, so 2013-2026 is not unseen for it,
and the holdout cannot be clean for C1. The protocol limits what else can be fitted to the
history. Paper trading is the only test nothing has seen.

1. **Data and dates.** Point-in-time signals for the top 1,000 by issuer market cap on each
   signal date. Signal dates are the last trading day of each month and the last trading day
   on or before the 15th. Trades happen at the next session's close (lag 1). Development runs
   2013-2019; the holdout runs from 2020 to the last stored session.
2. **Three scores, no others.**
   - **C1:** equal weight on value, quality and momentum.
   - **C2:** equal weight on seven groups: value, quality, momentum, leverage, investment,
     low risk and short interest.
   - **C3:** the same seven groups' signals, weighted walk-forward. Each signal's weight comes
     from its trailing 36 month-end rank ICs (mean over standard deviation, negatives set to 0),
     shrunk halfway to equal weights. An IC counts only after its month has passed, and with
     fewer than 24 months the weights are equal.
3. **Two portfolios.** Both use the risk model and the cost-aware optimizer, with expected
   return = 0.03 × score z × specific volatility.
   - **Long-short:** gross 2, 8% volatility target, sectors within 1%, beta within 0.05, at
     most 2% per name.
   - **Long-only:** at most 5% per name, a 5% tracking-error cap against the 500 largest
     companies (cap weighted, standing in for SPY), active sectors within 3%, beta within 1 ±
     0.1.
4. **$100,000 is real.** Targets are rounded to whole shares at the signal-date close.
   Positions under $250 are dropped, and names priced above half the largest position are
   left out.
   - Costs: the fitted half-spread, charged even on market-on-close fills (the auction case is
     a sensitivity), plus square-root impact at $100k and borrow on shorts.
   - Commission: IBKR Pro tiered pricing, $0.0035 a share with a $0.35 minimum, instead of a
     flat 1 bp.
5. **Choosing.** For each form, the score with the best development figure is chosen: net
   Sharpe ratio for long-short, net information ratio against SPY for long-only. That makes 3
   trials per form, and the deflated Sharpe ratio accounts for them.
6. **Go, checked once on the holdout:**
   - **Long-short:** net Sharpe ≥ 0.30 and max drawdown no worse than -20%.
   - **Long-only:** net information ratio against SPY ≥ 0.30, at least +1%/yr net above SPY,
     and relative drawdown no worse than -15%.
   - **Both:** a development deflated Sharpe ≥ 0.90, and the development figure still positive
     with lag 2 and with costs doubled.

   If both forms pass, the user chooses which one to paper trade. If neither passes, nothing is
   paper traded on this strategy.

## Deviations

None. Choices the protocol did not spell out, made in code before any result:

- **C3 with partly known signals.** Once 24 months of ICs are known, a signal with no trailing
  IC yet (short interest before 2018) gets only its share of the equal-weight half.
- **What the backtest charges vs what `conduit plan` sends.** The backtest trades every
  difference between the drifted book and the new whole-share targets. `conduit plan` skips
  trades under $200, so paper trading would trade a little less than the backtest.
- **The drift the optimizer trades from is unrounded.** Rounding to whole shares happens after
  the optimizer, on its targets.

## Run

```bash
uv run pytest projects/tertiary-adjunct
```

Notebooks in `notebooks/` (results committed). Reusable code in `src/tertiary_adjunct/`.

## Results

### Development, 2013-2019 (notebooks 00 and 01)

**No-go: neither form passes the development gate**, so neither can pass the protocol,
whatever the holdout shows.

| Form | Chosen | Figure (C1 / C2 / C3) | Deflated Sharpe | Lag 2 | Costs × 2 | Gate |
|---|---|---|---|---|---|---|
| Long-short | C3 | net Sharpe 0.24 / 0.41 / **0.44** | 0.82 (needs 0.90) | 0.43 | 0.27 | fails |
| Long-only | C1 | net IR vs SPY **-0.13** / -0.76 / -0.64 | 0.14 | -0.13 | -0.20 | fails |

- **Long-short.**
  - 3.9%/yr net at 10% volatility, max drawdown -23%.
  - Robust to lag and costs, but seven years at a 0.44 Sharpe ratio are not enough to tell it
    from zero after three trials. Even undeflated, the probability is 0.87.
  - Nearly all the return came in 2015. It has been flat to down since 2016.
- **Long-only.**
  - Every score trailed SPY net: C1 by 1.1%/yr, C2 by 4.7%/yr and C3 by 3.8%/yr.
  - A value-and-quality tilt within large caps lost to large-cap growth over these years.
- **Costs at $100k.**
  - Long-short pays about 1.6%/yr: 0.6% half-spread and impact, 0.3% borrow, and 0.7% IBKR
    commission (about 107 orders a rebalance). Commission alone takes 0.07 off its Sharpe ratio.
  - Long-only pays about 0.4%/yr.
  - Whole shares cost almost nothing next to fractional ones.

### Holdout, 2020 onward

Not run. With the development gate failed, the decision is no-go either way. An unseen
2020-2026 is worth more to the next protocol than a number that cannot change this one.

### Paper trading

Per the protocol, nothing is paper traded on this strategy. `Study.targets` (one rebalance
as `Conduit.plan` takes it) stays for a strategy that passes.
