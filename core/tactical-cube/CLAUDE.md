# tactical-cube (core, depends on nanoprobe-array, unimatrix-zero, vinculum)

Intraday backtests on nanoprobe-array bars: decisions at bar ends, fills a few bars later or in
the official auctions, participation caps, flat-by times or overnight holds, intraday costs and
a look-ahead check. User docs: README.md in this folder. Design: docs/decisions/0014-intraday.md
(phase 2).

## Public API (`from tactical_cube import ...`)

| Name | Use |
|---|---|
| `Cube(probe=None, vinculum=None)` | `run(strategy | targets=, start, end, ...)` -> `IntradayBacktest` |
| `IntradayBacktest` | daily, bars, fills, unfilled, by_security, market, settings, warnings; `summary()`, `by_year()`, `by_time_of_day()`, `round_trips()`, `fill_stats()` |
| `IntradayCosts(base=CostModel(), aum=None, tod, auction_spread_bps=0, commission_per_share=0.0035, commission_min=0.35)` | per-fill costs; `apply(fills, capital)`; `NO_INTRADAY_COSTS` |
| `simulate_day(day, bars, targets, aux, carry, pending, *, capital, lag, fill, flat_by, participation, moc_cutoff)` | one session -> `DayResult` (row, bars, fills, unfilled, carry, pending, by_security, warnings) |
| `check_lookahead(strategy, day, bars, cuts=(0.25,0.5,0.75), targets=None)` / `LookaheadError` | re-run on bars cut part-way through the day |
| `market_inputs(summaries)` | nanoprobe summaries -> date, security_id, ticker, open, close, prev_close, adv, vol, ret (adv and vol from earlier days only) |

`Cube.run` arguments: `strategy(day, bars) -> targets` or a `targets` frame with `date`;
`tickers` / `top` / `size` (as `Nanoprobe.days`), `freq="5min"`, `features`, `lag=1`,
`fill="vwap"|"open"|"close"`, `flat_by="close"|"HH:MM"|None`, `costs`, `participation=0.10`
(only with `costs.aum`), `moc_cutoff=10`, `check_lookahead=2` (days), `market="SPY"`,
`or_minutes`, `lookback`.

Targets: `bar_end` (a bar's end, UTC), `security_id`, `weight` (fraction of capital),
optional `auction` (`null` | `"close"` | `"open"`). Each (bar_end, auction) group is a whole
book: names not in it go to 0.

## Internals (private)

```
_costs.py      IntradayCosts (spread x measured time-of-day profile, sqrt impact, IB per share), NO_INTRADAY_COSTS
_sim.py        simulate_day on dense security x bar numpy arrays; schemas
_aux.py        market_inputs, AuxCache (a year of summaries, split by date)
_lookahead.py  check_lookahead
_cube.py       Cube (day loop, costs, borrow on overnight shorts), IntradayBacktest
```

Invariants:
- A decision at a bar's end fills no earlier than `lag` (>= 1) bars later; a bar's own prices are
  never tradable at its end. Nothing fills on a `stale` (empty) bar; the order waits.
- Positions are shares. Marks: bar closes within the day, the official open and close (from
  summaries) at the auctions, `prev_close` across nights (so splits are neutral and dividends
  are paid). Bar P&L = held x change in mark + traded x (mark - fill price).
- Closing-auction orders must be decided at least `moc_cutoff` minutes before the close. With
  `flat_by` set, nothing is held overnight; opening-auction orders need `flat_by=None`.
- Capital is fixed every day (returns are per unit of capital; `nav` compounds them), so daily
  output reads like unimatrix-zero's and its `summary`/`by_year` apply.
- Impact and the commission minimum are per order (date, security, kind, decision_time, side):
  splitting an order across bars never makes it cheaper.
- adv/vol come from earlier days only (shifted one day). Borrow is charged only on shorts held
  overnight, at the vinculum short-interest rate known that day (GC rate otherwise).
- Buy-and-hold through MOC orders sized at the official close matches unimatrix-zero
  `Engine.returns` exactly (checked to 1e-16, decision 0014).
- Read-only: no store of its own; reads through nanoprobe-array and vinculum.
