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

Run on 2026-09-16 against the five-year store. Raw output:
`<data_root>/research/screen_is_liquid1500.parquet`, reproducible with

```bash
ca research screen --universe liquid_1500 --end 2024-12-31 --tag is_liquid1500
```

### The screen found one signal, not nineteen

| stage | count |
|---|---|
| tests, 87 features by 4 horizons | 348 |
| significant at Benjamini-Hochberg q = 0.10 | 22 |
| passing all four survivor criteria | 19 |
| **distinct bets after clustering** | **2** |

Eighteen of the nineteen survivors are at a one-day horizon, and single-link clustering on their
daily IC series at a threshold of 0.5 puts **all eighteen in one cluster**. They are the same bet:
large, liquid, profitable, lightly shorted stocks outperformed. `roe`, `roa`, `cfo_ttm`,
`revenue_ttm`, `net_income_ttm`, `liabilities`, `earnings_yield`, `profit_margin`, `leverage`,
`cap`, `log_cap`, `log_price`, `amihud_21d`, `spread_est`, `spread_21d`, `share_pre_21d`,
`si_shares` and `si_ratio` all move together. The nineteenth, `si_days_to_cover` at 63 sessions,
stands apart only because it was tested at a different horizon.

A bar chart of t-statistics would have shown nineteen findings. The clustering step is the only
reason this reads as one.

### A flaw in the feature library

Several of those "fundamental" survivors are size in disguise. `revenue_ttm`, `net_income_ttm`,
`cfo_ttm`, `liabilities` and `assets` are raw dollar levels: a company with fifty billion in revenue
is simply a big company. They should be scaled by assets, sales or market value before being offered
as signals. The properly scaled ones, `earnings_yield` and `book_to_market`, behave quite
differently. This is a library fix, not a research finding, and it is the most actionable thing the
sprint produced.

### Famous anomalies in this sample

Clearing the family-wide bar required an absolute t of about 2.75.

| feature | horizon | IC | t | verdict |
|---|---|---|---|---|
| `si_ratio` | 1d | -0.021 | -3.16 | significant, and consistently negative at every horizon |
| `earnings_yield` | 1d | +0.019 | +2.99 | significant |
| `mom_12_1` | 1d | +0.019 | +2.47 | misses the bar |
| `turnover_21d` | 1d | -0.018 | -2.62 | misses |
| `vol_21d` | 1d | -0.021 | -2.18 | misses |
| `ret_5d` (reversal) | 5d | -0.015 | -1.28 | nothing |
| `accruals` | 63d | +0.028 | +1.29 | nothing |
| `asset_growth` | 21d | +0.012 | +1.14 | nothing |
| `book_to_market` | 21d | -0.001 | -0.06 | nothing at all |

Value was absent from this sample. Momentum, the most documented anomaly in equities, produced a t
of 2.47 and did not clear a correction for ninety-odd tests.

### A limitation of the protocol, not of the market

Twenty-one of the twenty-two significant results are at one day. That is an artefact. The
t-statistic deflates the sample by the horizon to account for overlapping returns, so a 63-session
test over three and a bit years has about thirteen independent observations. Nothing can clear a
multiple-testing bar on thirteen observations. **This was effectively a one-day-horizon screen**, and
slow signals were untestable rather than tested and rejected. Fixing it needs either a longer
history than the Starter plan provides or non-overlapping evaluation at long horizons.

### The combination, in-sample

Per the protocol, one representative per cluster: `roe` (the highest absolute t in the big cluster)
and `si_days_to_cover`, combined as an equal-weighted mean of winsorised z-scores, signed by their
in-sample ICs. The combination is extremely persistent, with a rank autocorrelation of 0.99 at one
day and 0.82 at twenty-one, so rebalancing frequency barely changes turnover.

| rebalance | net return | Sharpe | turnover/day | costs |
|---|---|---|---|---|
| daily | +6.0% | 0.51 | 0.170 | 2.9% |
| weekly | +5.5% | 0.47 | 0.135 | 2.4% |
| monthly | +2.9% | 0.26 | 0.072 | 1.5% |

Weekly was designated the headline before the out-of-sample run.

### Out-of-sample: it does not work

2025-01-02 to 2026-09-11, run once.

| measure | in-sample | out-of-sample |
|---|---|---|
| IC at 5 days | +0.020 | +0.011 |
| net return, weekly rebalance | +5.5% | **-7.4%** |
| Sharpe | +0.47 | **-0.57** |
| max drawdown | | -23.5% |
| 2025 / 2026 | | +3.9% / -16.2% |

The other two frequencies were also negative: -4.9% daily, -2.0% monthly. Gross returns were
negative too, so this is not a cost story.

### Why it failed, which is the interesting part

The information coefficient stayed **positive** out of sample while the long-short book lost money.
The decile profile explains it.

| | Q1 | Q2 | Q5 | Q9 | Q10 | top minus bottom |
|---|---|---|---|---|---|---|
| in-sample, bp per 5 days | -2.6 | -3.6 | +13.1 | +20.1 | +13.6 | **+16.2** |
| out-of-sample | **+37.6** | +22.2 | +26.6 | +38.9 | +30.2 | **-7.4** |

In-sample the entire edge sat in the short leg: only the bottom two deciles had negative returns and
everything above them was flat at +13 to +20 bp. The signal was not "buy quality", it was "avoid
junk".

Out-of-sample every decile was positive, and the bottom decile, the unprofitable heavily shorted
names, was the **best performing** of all. That is a junk rally with a short squeeze in it, and it is
precisely the regime that kills a short book built on low profitability and high short interest. The
rank correlation survived because the middle of the distribution stayed monotone; the tradable
spread did not, because the tail inverted.

### Verdict

**Nothing graduates to paper trading.** The one factor the library expresses at a testable horizon
does not survive its out-of-sample period, and the way it failed is structural rather than bad luck:
a short book concentrated in junk is short a squeeze.

### Hypotheses for a future sprint, each needing its own fresh test window

1. Scale the raw-dollar fundamentals so they stop being size proxies, then rerun. This changes the
   inputs, so the whole screen must be redone.
2. Test the long leg alone. The in-sample decile profile says the long side never had an edge, which
   argues against it, but it is cheap to check.
3. Give slow signals a fair test with non-overlapping windows at 21 and 63 sessions, where the
   present protocol is blind.
4. Short interest was the single most consistent input, negative at every horizon with |t| between
   2.4 and 3.2. It deserves a study of its own, with explicit attention to squeeze risk.

