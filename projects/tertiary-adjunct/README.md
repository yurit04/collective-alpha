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

None yet.

## Run

```bash
uv run pytest projects/tertiary-adjunct
```

Notebooks in `notebooks/` (results committed). Reusable code in `src/tertiary_adjunct/`.

## Results

Not run yet.
