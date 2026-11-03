# 0008 · unimatrix-zero: the backtester (2026-11-02)

## Context
Notebook 03 sorted stocks into quintiles at month-end closes with no costs, partial delisting
returns and returns that broke at holding-company reorganizations. Strategy research needs one
tested engine that handles timing, costs and corporate events the same way every time.

## Decision
A core package `unimatrix-zero` on top of neural-processor (signals), vinculum (universes,
successors, short interest) and Massive daily bars.

- **Daily engine, monthly (or any) rebalancing.** Weights are fractions of capital. They are
  dated by the signal date, executed `lag` trading days later at the close (default 1), and
  then drift with daily total returns. Turnover is measured against the drifted weights. A
  period loop in Python with vectorized polars inside is fast enough: 15 years, monthly, top
  1,000 in about 25 s.
- **Returns follow successors** (kind "security"): the predecessor's bars before the link are
  the successor's, and the link-day return comes from raw closes. Weights on the predecessor
  are relabelled the same way.
- **Delistings:** an extra return row the day after the last trade, then cash. The exit is the
  last close, or -30% (Shumway 1997) for a performance delisting: a last close under $1, or a
  fall of more than half over 63 trading days. Since 2012, 621 of the 1,968 securities that
  were ever in the top 1,000 stopped trading; 48 count as performance delistings.
- **Spreads from dollar volume, not from the price-range estimator.** The plan was the
  Abdi-Ranaldo estimator from daily high/low/close. On real data it read 0 for about half of
  large caps and 10-40 bp for the rest (JPM 19 bp; true quoted half-spreads are under 1 bp),
  which put the cost drag of a top-1,000 long-short book at 3.4% a year. The default is
  30 bp x (ADV in $M)^-0.5: about 1 bp at $1B a day, 3 bp at $100M, 10 bp at $10M, in line with
  US quoted spreads since decimalization. The estimator stays available
  (`spread="estimate"`). Massive quotes would allow a calibration, but they are 3-9 GB a day.
- **Data breaks:** daily returns above +300% count as 0. Only 31 of 6.1M universe rows
  qualify, and they are bankruptcy emergences: Ocean Rig went from $0.075 to $24 under one
  security, a 320x "return". Left in, they produce fictitious gains and 4,000% volatilities,
  and those volatilities blow up impact costs.
- **Impact only with an AUM:** square-root law (1.0 x daily vol x sqrt(trade / ADV)).
- **Borrow:** 25 bp a year general collateral, 300 bp when short interest is above 20% of
  float (FINRA, as published on the execution date).
- **No plotting in core**: results are data frames; notebooks plot.

## Rejected
- **Monthly returns only:** cannot model execution lag, delisting dates or borrow accrual.
- **Event-driven order simulation:** far slower and unnecessary for close-to-close research.
- **Ticker-based return series:** break at reorganizations and reuse tickers across companies.

## Found while building
Penny ADRs with wrong ratios reach vinculum's top 1,000 with impossible market caps (NCNA:
$0.81, $37k a day, shown as a $6B company). They cost hundreds of basis points to trade with
impact. Backtests that model impact should use `min_dollar_volume`; the ADR ratio check belongs
in vinculum.

## Limits
No risk model or optimizer (planned for `borg-queen`). Delisting recoveries, borrow recalls and
intraday fills are not modelled.
