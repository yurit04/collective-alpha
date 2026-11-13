# 0010 · transwarp-conduit: IB execution, paper first (2026-11-04)

## Context
borg-queen produces target weights that pass the go criteria (decision 0009). The user trades
at Interactive Brokers. Execution must reproduce the backtest's assumption (trade at the close
the day after the signal) and must not be able to send an order by accident.

## Decision (phase 1)
A core package `transwarp-conduit` on massive-subspace-stream (security master) and
unimatrix-zero (prices, ADV and volatility from its return panel; CostModel for estimated
costs). It does not depend on borg-queen: it takes weights from anywhere.

- **ib_async against IB Gateway**, not IB's Client Portal web API: a local socket, no stored
  credentials (Gateway holds the login), mature order and execution callbacks. ib_async is the
  maintained successor of ib_insync. All of it sits behind a `Broker` protocol, so tests and
  rehearsals use `SimBroker`, and another broker could be added later.
- **Market-on-close orders** by default, because the backtest trades at the close: a live
  rebalance then differs from the backtest only by real costs, which phase 2 measures.
- **Plan, then submit.** `plan` is a dry run saved under the data root; `submit` re-checks
  everything and sends only that sheet. Submission refuses: a blocked or old sheet, a sheet
  already sent, a non-trading day, the last 15 minutes before the close, another account,
  changed positions, still-open orders from an earlier sheet, and any live account unless
  `--live`, `live = true` and `account` = that account all agree.
- **Read-only connections** for every read (IB enforces it), writable only to send or cancel.
- **Contracts by FIGI, else by checked symbol.** IB resolves a composite FIGI directly, which is
  our `security_id` (decision 0002): an exact identity, immune to reused tickers and renames.
  When IB does not know the FIGI, the security master ticker on the trade date → IB symbol, and
  a match needs the same company name and, where known, primary exchange. Checked matches are
  cached, manual pins go in a CSV.
- **Only managed positions are traded.** A position is managed if it is a target or this
  package ordered it before; anything else in the account is reported and left alone.
- **Whole shares** (the TWS API has no fractional orders); trades under $200 or 0.05% of
  capital are skipped, except closing a position.
- **An append-only journal** (sheets, orders, fills with commissions, events) and
  reconciliation of expected against actual positions after the close, with slippage against
  the decision price (the last close).

## Rehearsal
A real optimizer book (2026-10-30 signal, 458 names) through the real security master and
market data with SimBroker at $250k: 311 orders, turnover 1.96× capital, estimated cost $173;
whole-share rounding moved weights by 0.02% on average (at most 0.2%); 71 trades under $200
skipped. Planning takes under a second.

*Added 2026-11-13:* `market` (so `plan` and `holdings`) never prices from a close that has not
happened at the clock's time: during the session it uses the previous close even when the store
already holds the day's (a fixed clock in a rehearsal, or a re-run after the nightly sync). The
demo notebook's 10:00 ET rehearsal had been priced at that day's close.

## First check against IB (paper, read-only, 2026-11-04)
The 458 names of that book: 420 resolved by FIGI and 38 by symbol, none blocked; lookups take
about 10 s. The first version matched by symbol and name only and blocked 24 correct names,
because Massive and IB spell names differently (McDonald's / MC DONALD'S, N.V. / NV, ADR
descriptions, Charter Comm / CHARTER COMMUNICATIONS); after the FIGI lookup and a better name
rule, the name rule agrees with the FIGI on 419 of 420 (the exception is a rename, Arrowhead
Research → Arrowhead Pharmaceuticals, which the FIGI gets right). Borrow availability comes
from delayed market data (generic tick 236), which IB serves without a subscription.

## Rejected
- **Client Portal Web API:** needs a browser-authenticated session that expires, and weaker
  order-status streaming; fractional shares are its one advantage.
- **Sending orders straight from the optimizer:** no review point and no record of intent.
- **Sizing from the account's whole NAV with no notion of managed positions:** would sell the
  user's other holdings.
- **Clipping orders to limits silently:** an order over a limit blocks the sheet (or is dropped
  with a warning when asked), so the realized book never differs from the plan unnoticed.

## Next
Phase 2: monthly paper rebalances; compare fills with the CostModel (spread and impact
coefficients) and the slippage from the decision price. Phase 3: live with the limits, and
LOC / Adaptive / VWAP orders where they beat the close.
