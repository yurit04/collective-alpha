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

- Weights dated by **signal date** trade at the close `lag` trading days later (default 1). A
  signal dated on a weekend or holiday counts from the last trading day on or before it
  (`Engine.execution_dates`).
  Positions then drift with returns; at the next execution date the book trades from drifted to
  target weights.
- **Turnover** is two-sided, as a multiple of capital, and includes the initial build-up.
- **Untradable targets:** a name with no bar on its execution date is dropped and its leg
  rescaled. These are mostly completed acquisitions between the signal and the trade, such as
  Broadcom Corp in February 2016; the count is in `settings["untradable_dropped"]`.
- **Tradability rules** (optional, on the signal date): `min_price` and `min_dollar_volume`
  (21-day mean) drop names and rescale each leg. Use them for what a real book could trade: a
  mandate's price or liquidity floor, or a capacity study. There, set the floor from the
  position size, e.g. AUM x weight / (a participation limit such as 10% of daily volume).
  The default is off. The universes are vinculum size ranks, so illiquid names are rare, and
  the cost model already charges them. In the top 1,000 since 2012, a $1M-a-day floor changes
  the combined book's net return by under 0.2 points a year. The floors are not a data
  filter: a wrong market cap is vinculum's to fix, and the cost warnings below are how one
  shows up.
- **Result `Backtest`:**
  - `daily`: date, gross, net, long, short, borrow, cost, turnover, gross_exposure, net_exposure,
    nav;
  - `trades`: before and after weights, cost, `spread_bps` and `impact_bps` per unit traded, and
    `adv` and `vol`;
  - `cost_warnings`: trades costing more than `CostModel.warn_bps` (100 bp by default) per unit
    traded. One warning reports the count and the worst security.
  - `weights`, as traded;
  - `summary()`, `by_year()`, `exposures()`.

## Costs (`CostModel`)

| Cost | Default |
|---|---|
| half-spread | `spread="volume"`: 30 bp x (ADV in $M)^-0.5, clipped to [0.5, 200] bp; about 1 bp at $1B a day, 3 bp at $100M, 10 bp at $10M |
| commission | 1 bp per trade |
| market impact | only with `aum`: 1.0 x daily vol x sqrt(trade $ / ADV $) |
| borrow | 25 bp a year on short value; 300 bp when short interest > 20% of float |

**Cost warnings:** a trade in a listed stock rarely costs 100 bp. One that does is either a
position too large for the stock (raise the AUM and they multiply) or a data problem: a market
cap inflated by a wrong share count or ADS ratio puts a tiny company into a size universe.
`Backtest.cost_warnings` lists them with ticker, dollar volume and the cost split; report data
problems to vinculum rather than filtering them here.

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
- Costs are models; no borrow recalls, locate failures or intraday fills.
- Market caps come from vinculum. The cost warnings found twelve wrong market caps on
  2026-11-03, fixed in vinculum the same day (0004 addendum): ADSs typed as common stock and two
  US share counts. LATAM's old ADS ($2.4T in 2020) followed. New ones would show up the same
  way.
- Signals and trades at daily closes only.
