# 0007 · neural-processor: the signal library (2026-11-02)

## Context
Notebooks 02 and 03 each defined their own ratios and signals as polars expressions, with
slightly different rules (orientation by negation, EV with or without short-term debt). The
planned backtester needs one shared, tested definition of every signal.

## Decision
A new core package, `neural-processor`. It depends on vinculum, edgar-cortical-node (for
CONCEPTS and SIC codes) and massive-subspace-stream (daily bars).

- **Package, not vinculum:** vinculum joins data sources; signals are opinions about that data
  and will change much more often. Keeping them apart keeps vinculum's build and invariants
  small, and the backtester depends on the signal package, not on internals.
- **Catalogue as data:** each `Signal` carries group, formula, inputs, direction and an
  expression. `panel()` loads only the inputs the requested signals need. Composites and later
  the backtester orient signals by `direction` instead of storing negated copies (the old
  `low_accruals`, `small_size`).
- **Computed on request, no store:** fundamental panels take seconds; only price signals over
  long histories take minutes. A stored panel can come later if studies need it.
- **Growth vs a year earlier, point in time:** the row 12 month ends back, which used only what
  was usable then. Restated "prior year" columns would leak later information.
- **Successors followed:** price series and growth lags continue across vinculum's security
  handoffs, with the link-day return from raw closes. Without this ExxonMobil lost momentum,
  beta and growth for a year after July 2026.
- **Sectors moved to core:** the FF12 mapping moved here from subspace-probe, so core code can
  neutralize by sector. subspace-probe re-exports it.

## Rejected
- **Signals inside vinculum:** couples a nightly data build to research definitions.
- **Pre-computed daily panel for every signal:** about 50 columns x 30M security-days, rebuilt
  whenever a definition changes. Not worth it before the backtester shows which signals matter.
- **Equal-weighted market for beta:** small stocks dominate it; cap-weighted over the panel's
  own securities tracks SPY daily (correlation 0.99).

## Limits
Directions come from published studies (`note` marks mixed evidence). Sector is today's SIC
code. A split right after a security handoff is not reflected in EPS or share growth.
