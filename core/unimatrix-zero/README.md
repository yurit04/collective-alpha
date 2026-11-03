# unimatrix-zero

Point-in-time backtests over vinculum universes and neural-processor signals: daily total
returns linked across reorganizations with delisting exits, portfolio builders, a daily engine
with execution lag and costs, and performance analytics. No downloads; no plotting.

```python
from neural_processor import Processor, composite
from unimatrix_zero import Engine, CostModel, NO_COSTS, quantile_weights, rank_weights, top_n_weights

names = ["fcf_yield", "roe", "momentum_12_1"]
sig = composite(Processor().panel(names, start="2012-01-01", top=1000), names, name="score")
w = quantile_weights(sig, "score", q=0.2)              # long top fifth, short bottom fifth
eng = Engine()
bt = eng.run(w)                                         # lag=1, CostModel(), delisting="shumway"
bt.summary(); bt.by_year(); bt.exposures(); bt.daily; bt.trades
eng.run(w, costs=CostModel(aum=1e9))                    # with square-root market impact
eng.run(w, costs=NO_COSTS, lag=0)                       # the frictionless version
eng.ic(sig, names); eng.quantile_returns(sig, "score"); eng.forward_returns(sig)   # quick screens
```

## Returns (`Engine.returns`)

- Daily close-to-close total returns from Massive bars adjusted for splits and dividends.
- **Successors:** a company that moved to a new security (vinculum successors of kind
  "security", e.g. ExxonMobil in July 2026) is one series. The link-day return comes from raw
  closes, and weights on the predecessor count as weights on the successor.
- **Delistings:** a security that stops trading before the data ends gets a final return on the
  next trading day, then is held as cash until the next rebalance (selling it costs nothing).
  - The final return is 0 (exit at the last close: right for most large-cap exits, which are
    acquisitions).
  - For a performance delisting it is -30% (Shumway 1997). A performance delisting is a last
    close under $1, or a fall of more than half over 63 trading days.
  - `delisting="last"` turns the adjustment off.
- **Data breaks:** a one-day return above +300% is not a return. In the data these are
  bankruptcy emergences: pennies to $20+ under the same security, as with Ocean Rig, Ensco,
  Dynegy and Superior Energy. That day counts as 0 and `break` is set.
- Also per day: raw close, 21-day mean dollar volume, 63-day volatility (breaks skipped, daily
  returns clipped at +/-50%), and an Abdi-Ranaldo half-spread estimate.

## Weights

All builders orient the signal by `neural_processor.SIGNALS[...].direction` (+1 for composites),
so the top is always the expected winner. Long legs sum to +1, short legs to -1 (fractions of
capital). Each returns date, security_id, weight, sector, issuer_market_cap.

- `quantile_weights(panel, col, q, long_short, weighting="equal"|"cap"|"signal", sector_neutral, max_weight)`
- `rank_weights(panel, col, sector_neutral, max_weight)`: proportional to the centred rank.
- `top_n_weights(panel, col, n, weighting, max_weight)`: long-only.
- `sector_neutral`: each sector's long and short legs are its share of the names, so each sector
  nets to zero. `max_weight` caps names within a leg, which can break exact sector neutrality.

## Engine (`Engine.run`)

- Weights dated by **signal date** trade at the close `lag` trading days later (default 1).
  Positions then drift with returns; at the next execution date the book trades from drifted to
  target weights.
- **Turnover** is two-sided, as a multiple of capital, and includes the initial build-up.
- **Filters:** `min_price` and `min_dollar_volume` (on the signal date) drop names and rescale
  each leg.
- **Result `Backtest`:**
  - `daily`: date, gross, net, long, short, borrow, cost, turnover, gross_exposure, net_exposure,
    nav;
  - `trades`;
  - `weights`, as traded;
  - `summary()`, `by_year()`, `exposures()`.

## Costs (`CostModel`)

| Cost | Default |
|---|---|
| half-spread | `spread="volume"`: 30 bp x (ADV in $M)^-0.5, clipped to [0.5, 200] bp; about 1 bp at $1B a day, 3 bp at $100M, 10 bp at $10M |
| commission | 1 bp per trade |
| market impact | only with `aum`: 1.0 x daily vol x sqrt(trade $ / ADV $) |
| borrow | 25 bp a year on short value; 300 bp when short interest > 20% of float |

`spread="estimate"` uses the Abdi-Ranaldo estimate instead. It is noisy for liquid stocks:
about half of large caps read 0 and the rest 10-40 bp, which roughly triples costs. A number
gives a fixed half-spread in bp. `NO_COSTS` turns everything off.

## Analytics

`summary()` has one row each for net, gross, long leg and short leg:
- annualized geometric return, volatility, Sharpe, t-stat and max drawdown;
- on the net row also: turnover per rebalance and per year, cost drag (gross minus net,
  including borrow), average gross and net exposure, and beta and alpha against SPY.

`exposures()` gives, per rebalance: the number of long and short names, gross and net weight,
the size tilt (long minus short weighted log market cap), and the net weight per sector.

## Speed

A monthly long-short run over 15 years of the top 1,000 takes about 25 s. Most of that is
reading daily bars, which are cached between runs on the same universe.

## Limits

- No risk model or optimizer (planned: `borg-queen`): exposures are reported, not controlled.
- Delisting returns are a rule of thumb; bankruptcy recoveries are not in the data.
- Costs are models; no borrow recalls, locate failures or intraday fills. A few penny ADRs carry
  wrong market caps in vinculum and reach the top 1,000; use `min_dollar_volume` (e.g. $1M a
  day) whenever impact matters.
- Signals and trades at daily closes only.
