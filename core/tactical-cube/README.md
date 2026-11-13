# tactical-cube

Intraday backtests on the bars from `nanoprobe-array`. A strategy is a function that sees one
day's bars (with features) and returns target weights at bar ends; the cube turns them into
orders, fills them a bar or more later (or in the official opening and closing auctions), caps
each fill at a share of the bar's volume, charges intraday costs, and reports daily returns in
`unimatrix-zero`'s format so intraday and daily strategies compare with the same analytics.
Design: [docs/decisions/0014-intraday.md](../../docs/decisions/0014-intraday.md) (phase 2).

```python
import polars as pl
from tactical_cube import Cube, IntradayCosts

def momentum(day, bars):
    """Long SPY for the last half hour if it rose from the previous close to 10:00."""
    at_10 = bars.filter(pl.col("minutes_from_open") == 30)
    enter = bars.filter(pl.col("minutes_to_close") == 30)
    if at_10.is_empty() or enter.is_empty():
        return None
    side = 1.0 if (1 + at_10["gap"][0]) * (1 + at_10["ret_open"][0]) > 1 else -1.0
    return pl.DataFrame({"bar_end": enter["bar_end"], "security_id": enter["security_id"], "weight": [side]})

bt = Cube().run(momentum, "2004-01-02", "2026-10-30", tickers=["SPY"], freq="5min",
                features=["gap", "ret_open"], costs=IntradayCosts(aum=1e6))
bt.summary()          # gross and net: return, vol, Sharpe, t, drawdown, turnover, cost drag
bt.by_time_of_day()   # where in the day the P&L comes from
bt.fill_stats()       # spread, impact, commission, slippage, participation by fill kind
bt.round_trips()      # per security and day: gross, costs, net, minutes held
```

## Execution

- **Targets** are `bar_end`, `security_id`, `weight` (fraction of capital), and optionally
  `auction` (`"close"` or `"open"`). Each decision time is a whole book: names left out go to 0.
- **Lag.** A decision at a bar's end starts filling `lag` bars later (default 1) at the fill
  bar's VWAP (`fill="open"` / `"close"` also available). Orders on empty (`stale`) bars wait.
- **Participation.** With an AUM, each fill takes at most `participation` (default 10%) of the
  bar's share volume; the rest keeps working until it fills, is replaced or the day ends
  (`bt.unfilled` says why).
- **Auctions.** `auction="close"` trades at the official close (decided at least `moc_cutoff`
  minutes before), `auction="open"` at the next session's official open.
- **Flat by.** `flat_by="close"` (default) exits in the closing auction, `"15:30"` from that bar
  (New York time; a rebalance that would only start trading after it is ignored), `None` holds overnight: positions roll at `prev_close`, so splits are neutral
  and dividends are paid, and shorts held overnight pay borrow at vinculum's short-interest rate.
- **Look-ahead check.** On the first `check_lookahead` days with targets (default 2) the strategy
  is re-run on the day's bars cut at 25, 50 and 75% of the session; if any target up to the
  cut changes, `LookaheadError` is raised.

## Costs

`IntradayCosts(base=CostModel(...), aum=1e6)`:

- half-spread from unimatrix-zero's `CostModel` (fitted on measured costs from dollar volume,
  volatility and the year, or fixed bps), times a measured time-of-day multiplier: 3.7x in the
  first minute, 2.6x to 5, 1.75x to 15, 1.4x to 30, 1.2x to 60, then 1.0 down to 0.6x in the
  last minutes (decision 0017); 0 in the auctions. Index ETFs: pass their measured spread as a
  number and `tod=()`; their spread is flat through the day (SPY 0.12 bp)
- square-root impact on the whole order's share of ADV (21 days, known before the open) and
  63-day volatility; every fill of an order pays the same rate, so splitting it is not cheaper
- IB fixed commission, $0.0035 a share with a $0.35 minimum per order (without an AUM,
  `commission_bps`)
- borrow on shorts held overnight

Without an AUM the backtest is per unit of capital: no impact, no participation cap.

## Speed

SPY over 23 years at 5 minutes runs in about 40 s; the top 500 in about 30 s a year.
