# transwarp-conduit

Execution through **Interactive Brokers**: target weights (from `borg_queen`, or any source) →
a checked **order sheet** → **market-on-close orders**, paper accounts unless you explicitly opt
in to live → fills, **reconciliation** and slippage against the decision price.

Phase 1 (built): connection, account and positions, contract mapping, order sheets with limits,
paper orders, journal, reconciliation. Planned: a month or more of paper rebalances and a
cost comparison with unimatrix-zero's CostModel (phase 2), then live trading (phase 3).

## Setup

1. An **IBKR Pro** account (the API does not work with IBKR Lite) and its paper account.
2. **IB Gateway** (or TWS) running on this Mac and logged in to the **paper** account, with
   *Configure → Settings → API → Settings*: "Enable ActiveX and Socket Clients" on, socket port
   4002 (Gateway paper default; TWS paper is 7497: set `port` to whichever yours uses), "Read-Only API" off only when you want to send
   orders, and 127.0.0.1 as a trusted IP. This package never sees your IB password.
3. Optional settings in `collective.toml` (same lookup as the other packages):

   ```toml
   [transwarp_conduit]
   port = 4002              # 4001 / 7496 for live
   client_id = 17
   # account = "DU1234567"  # needed if the login manages several accounts, and for live
   # capital = 100000       # dollars the strategy runs; default: net liquidation value
   # live = false           # live orders also need --live and account = the live id

   [transwarp_conduit.limits]
   max_weight = 0.03        # per name, after trading
   max_gross = 2.1
   max_net = 0.10
   max_turnover = 2.5       # per sheet, as a multiple of capital
   max_order_usd = 250000
   max_participation = 0.10 # of 21-day average dollar volume
   min_trade_usd = 200
   min_trade_weight = 0.0005
   ```

   Environment variables override: `TRANSWARP_PORT`, `TRANSWARP_ACCOUNT`, `TRANSWARP_CAPITAL`, …;
   `TRANSWARP_DATA_ROOT` moves the journal (default `~/market_data/transwarp_conduit`).

## Use

```bash
uv run conduit status                       # account (masked), paper?, NAV, positions, open orders
uv run conduit contracts targets.parquet    # which securities map to an IB contract, which do not
uv run conduit plan targets.parquet         # dry run: order sheet saved under the data root
uv run conduit show 20261104-41f8d8         # look at it again
uv run conduit submit 20261104-41f8d8       # after the checks, sends MOC orders (asks first)
uv run conduit orders 20261104-41f8d8       # their status
uv run conduit reconcile 20261104-41f8d8    # after the close: expected vs actual, slippage
uv run conduit cancel 20261104-41f8d8       # this sheet's open orders
uv run conduit cancel-all                   # kill switch: every open order in the account
```

`targets.parquet` (or `.csv`): `security_id, weight` (fractions of capital), optionally `date`
(the latest date is used unless `--on` picks one), e.g. the last date of
`borg_queen.Portfolio.build`'s weights, or one `Optimizer.solve` started from
`Conduit().holdings()`:

```python
from transwarp_conduit import Conduit

c = Conduit()
w0 = c.holdings()                                  # managed positions as weights of capital
sol = Optimizer(rm, CostModel(aum=capital)).solve(alpha_today, signal_date, w0, market)
sheet = c.plan(sol.weights)                        # dry run
print(sheet.summary()); sheet.orders
c.submit(sheet.sheet_id)                           # paper; on the execution day, before the cutoff
```

## How it works

- **Contracts.** First by **FIGI**: IB looks up the listing's composite FIGI from the security
  master, an exact identity (420 of 458 names in the first check against IB, including a
  renamed company). When IB does not know the FIGI, by **symbol**: the ticker on the trade date
  (`BRK.B` becomes `BRK B`), and the match must have the same company name after legal forms
  and security descriptions are dropped (McDonald's = MC DONALD'S, Mercado Libre =
  MERCADOLIBRE, Charter Comm = CHARTER COMMUNICATIONS; First Solar is not First Horizon, Apple
  is not Apple Hospitality REIT) and, where known, the same primary exchange. Otherwise the
  name is `not_found`, `ambiguous` or `name_mismatch` and cannot be traded. Checked matches
  are cached for 30 days in `contracts.parquet`. Pin a contract by hand in
  `contract_overrides.csv` (`security_id,con_id,note`) under the data root.
- **Order sheet.** Capital = the `capital` setting, else net liquidation. Target shares =
  weight × capital ÷ last close (from unimatrix-zero's return panel, as in the backtest),
  rounded to whole shares (IB's API has no fractional shares). Trades below `min_trade_usd` or
  `min_trade_weight` are skipped, except closing a position. A position this package has
  ordered before and that is no longer a target is sold; any other position is **unmanaged**
  and left alone (and listed). Each order shows the CostModel's estimated cost (half-spread,
  commission, square-root impact at this capital).
- **Checks.** Per order: a tradable contract, a price, `max_order_usd`, `max_participation`,
  borrow (IB's shortable shares, from delayed market data, which needs no subscription; a name
  with no answer is a warning). Per sheet:
  `max_weight`, `max_gross`, `max_net`, `max_turnover`. Any failure makes the sheet `blocked`
  (or `--drop-blocked` drops those orders with a warning).
- **Submit** refuses unless the sheet is `planned`, made today and not yet sent; today is a
  trading day and it is before the close minus `cutoff_minutes` (15; MOC orders must reach the
  exchanges by 15:50 ET, 12:50 on half days); the connected account is the sheet's; the
  account's positions have not changed since planning; no earlier sheet's orders are still
  open; and the account is a paper account (`DU…`), unless `--live` **and** `live = true`
  **and** `account` = this live account. Orders carry the sheet id as `orderRef`.
- **Connections** are read-only (IB enforces it) except for submit and cancel.
- **Journal** (data root, append-only): `sheets/<id>/` (orders, unmanaged positions, sheet.json),
  `orders.parquet` (every order sent), `fills.parquet` (executions with commissions, by
  exec id), `events.jsonl`. IB reports executions for about a day: run `conduit fills` or
  `reconcile` the same evening.

## Limits

- One account, US common stocks and ADRs in USD, whole shares.
- MOC only in practice (MKT works; LOC and algos are phase 3).
- Borrow availability is a snapshot when the sheet is planned; IB can still reject a short it
  cannot locate at the close.
- Prices are the last close in the local data: run the nightly sync before planning.
- Nothing runs on a schedule: you plan and submit each rebalance.
