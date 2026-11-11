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
Penny ADRs with wrong ratios reached vinculum's top 1,000 with impossible market caps (NCNA:
$0.81 and $37k a day, shown as a $6B company), and impact costs on them ran to hundreds of
basis points. The first version worked around this with `min_dollar_volume`. vinculum has since
fixed it (0004, addendum 2026-11-03).

## Addendum (2026-11-03): the liquidity floor, untradable targets, cost warnings
On the rebuilt data, with the combined long-short book (top 1,000, monthly, 2012-2026):

| AUM | floor | net return | net Sharpe | cost per unit traded | max | trades over 100 bp |
|---|---|---|---|---|---|---|
| none | off | 4.97% | 0.54 | 5.0 bp | 201 bp | 44 |
| none | $1M | 5.03% | 0.55 | 4.8 bp | 69 bp | 0 |
| $10M | off | 4.14% | 0.46 | 12.1 bp | 978 bp | 237 |
| $10M | $1M | 4.26% | 0.47 | 11.3 bp | 716 bp | 56 |
| $100M | off | 2.51% | 0.30 | 26.0 bp | 2,545 bp | 767 |
| $100M | $1M | 2.73% | 0.32 | 24.4 bp | 1,915 bp | 546 |

(Before the adv fix below; turnover 11.2-11.3 a year in every row.)

- **The floor is a tradability rule, off by default.** It changes the net return by under 0.2
  points and does not remove the data errors below, several of which trade over $1M a day.
  It stays for mandates and capacity studies.
- **Untradable targets.** 36 targets had no bar on their execution day, because the company was
  acquired between the month-end signal and the next day's trade (Broadcom Corp, Salix,
  TriQuint/RFMD, Sprint, Tableau, Yamana, Cimarex). The engine used to "buy" them at the 200 bp
  spread cap; now they are dropped and the leg rescaled.
- **New listings.** The 21-day dollar volume needed 5 days, so trades in a stock's first week
  (Albertsons after its 2020 IPO, the Hillshire and Tegna spin-offs) fell back to the cap. It
  now averages the days there are.
- **Cost warnings.** Trades above `CostModel.warn_bps` (100 bp) are listed in
  `Backtest.cost_warnings` and reported in one warning. With impact on, most flagged trades are
  real illiquid US listings of foreign companies (James Hardie, Woori, Nomura, KEPCO, ICL, ORIX,
  Advantest, Makita, Nidec, Mizuho), thin-float US companies (Seaboard, Valhi, Clear Channel
  Outdoor), or squeezes (Wins Finance, Regencell). Twelve are vinculum data errors, reported
  there:
  - ADSs that Massive types as CS, valued as if 1 ADS = 1 share: FANH `BBG000TJXWP2`, PLL
    `BBG00YJ7NBQ1`, MFH `BBG007WGC6B4`, TAOM `SYM:TAOM:2011-06-09`, SOS `BBG00GCTC8Y9`, SPI
    `CIK:0001210618:SPI`, MOB `BBG017F91SX8`, QH `BBG00V95T6J5`, ISS `CIK:0001500308:ISS`,
    TCL `SYM:TCL:2008-02-13`.
  - US share counts: BIOL `BBG000DDG8B9` (32.5 billion shares in 2024) and ARDM
    `BBG00HVZ59F9` (pre-reverse-split count in 2014).

  All twelve were fixed in vinculum the same day (0004, second addendum of 2026-11-03). Fixing
  them surfaced a thirteenth: LATAM's old ADS `BBG000JJBNH6`, shown at up to $2.4T in 2020
  because its counts were scaled to its 2022 reorganization's 1000x issuance. Its warning
  (201 bp on no recorded volume, July 2020) came through the successor link as LTM
  `BBG01PDH58Y1`. That was fixed as well (0004, follow-up).

## Limits
No risk model or optimizer (planned for `borg-queen`). Delisting recoveries, borrow recalls and
intraday fills are not modelled.

## Addendum 2026-11-11: large falls are not data breaks
The +300% rule stays one-sided on purpose. In the top 3,000 (2005-2026) there are 539 one-day
falls of 50% or more; most are real (failed trials, frauds, delisting warnings), so zeroing
them would hide real losses. A missed split looks different: the SEC share count, filed
before vs after, rises by about the ratio the price fell. That test (vinculum's
`suspect_splits`) found 5 of the 539: Alphabet 2022 and O'Reilly 2025 (real splits misread,
fixed in the security master, 0002), Core-Mark and ARC Group Worldwide 2014 (splits Massive
lacked) and CAMAC Energy 2014 (an acquisition paid in new shares, not a split). A sixth,
Meridian Bancorp's 2014 second-step conversion (2.4484 new shares per old), appeared once the
check ran on the panel itself. The missing splits are fixed by override rows; the engine now
lists any held day the check flags in `Backtest.data_warnings` and warns, without changing
the return.
