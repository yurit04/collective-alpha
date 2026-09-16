# Research sprint 1: screening the feature library

**Protocol fixed on 2026-09-16, before any result was looked at.** The point of writing it down
first is that the main risk in this exercise is not a bug, it is testing ninety-odd signals and
keeping whichever looked best.

## Sample split

| period | dates | use |
|---|---|---|
| in-sample | 2021-09-13 to 2024-12-31 | screening, clustering, choosing weights |
| out-of-sample | 2025-01-01 to 2026-09-11 | run once, at the end, for the combination only |

The out-of-sample period is not to be touched until the in-sample work is finished and the
combination is fixed. Any decision informed by it invalidates it.

## What is tested

Every feature in the library, at horizons of 1, 5, 21 and 63 sessions, on `liquid_1500`. The liquid
universe is primary on purpose: a signal that works only in `all_common` is usually a small-cap
liquidity effect that transaction costs will eat.

Features that are identifiers, counts or diagnostics rather than candidate signals are excluded by
name: `shares_source`, `bar_coverage`, `bar_coverage_21d`, `suspect_split_252d`, `n_bars`,
`news_sent_n`.

## Metric

Rank information coefficient per date, then across dates: mean, standard deviation, and a
t-statistic with the effective sample size reduced by the horizon to account for overlap. Tests are
two-sided, because a signal that predicts with a negative sign is a real signal traded the other way.
Reporting the sign afterwards does not cost a multiple-testing penalty, but choosing the horizon does,
so every feature-horizon pair counts as one test.

## Multiple testing

The family is every feature-horizon pair, roughly 90 features by 4 horizons. Benjamini-Hochberg at a
false discovery rate of 10% decides significance. A raw t of 2 means nothing in a family this size.

## Survivor criteria, all four required

1. Benjamini-Hochberg significant at q = 0.10 in-sample
2. absolute mean IC at least 0.01
3. the sign of the yearly IC agrees with the full-period sign in at least 3 of the 4 in-sample years
4. the feature is present for at least half of universe member-days

## What happens to survivors

1. Cluster by the correlation of their daily IC series. Features inside a cluster are the same bet
   wearing different clothes.
2. Keep one representative per cluster, the highest absolute t-statistic.
3. Combine the representatives as an equal-weighted mean of winsorised z-scores, each signed by its
   in-sample IC. Equal weights, not fitted weights: with this few years of data, fitted weights are
   an overfitting machine.
4. Backtest the combination in-sample with per-name estimated spreads.
5. Run out-of-sample once and report whatever it says.

## What this cannot establish

Five years is a short sample containing one bear market, one mania and one rate cycle. Survivorship
of the *features* is not tested: the library was written by someone who knows which anomalies are
famous, so the screen is closer to a confirmation than a discovery. Treat a surviving signal as a
hypothesis worth paper trading, not as an edge.

---

## Results

Filled in below as the sprint runs.
