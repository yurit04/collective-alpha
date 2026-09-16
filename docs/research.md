# Research guide

The workflow is always the same four steps: express the idea as a signal, measure whether it
predicts returns, trade it in a backtest with honest costs, then decide whether portfolio
construction rescues or ruins it. Each step is cheap, so kill bad ideas early.

Runnable versions of everything here live in [`examples/`](../examples).

## 1. Express the idea

A signal is a long frame of `security_id`, `date`, `signal`, restricted to a tradable universe. The
value must use only information known by that session's close. Features already satisfy that, so the
usual path is to load features and transform them.

```python
import datetime as dt
import polars as pl
from collective_alpha.features.base import load_features

f = load_features(
    ["price", "intra"],
    start=dt.date(2022, 1, 1),
    universe="liquid_1500",
    columns=["mom_12_1", "vol_21d", "spread_21d"],
)

# risk-adjusted momentum, tradable only where the spread is not punitive
sig = (
    f.filter(pl.col("spread_21d") < 0.002)
    .with_columns(signal=pl.col("mom_12_1") / pl.col("vol_21d"))
    .select("security_id", "date", "signal")
    .drop_nulls("signal")
)
```

Cross-sectional helpers live in `collective_alpha.features.signals`:

| function | what it does |
|---|---|
| `cs_rank(df, col)` | percentile rank within each date |
| `cs_zscore(df, col, winsor=0.01)` | winsorised z-score within each date |
| `neutralize(df, col, group)` | demean within a group, for example a sector |
| `combine(df, {"a": 1.0, "b": -0.5})` | weighted mean of z-scored columns, ignoring nulls |
| `lag(df, col, n)` | shift a value n sessions later, to trade with a delay |

For a single feature, skip all of this and use the command line:

```bash
ca eval feature mom_12_1 --universe liquid_1500 --horizon 21
```

## 2. Measure it

```python
from collective_alpha.eval.report import evaluate, load_forward_returns

fwd = load_forward_returns(start=dt.date(2022, 1, 1))
rep = evaluate(sig, fwd, name="risk-adjusted momentum", universe="liquid_1500", horizon=21)
print(rep.to_markdown())
```

The report answers four questions.

* **Does it predict?** Rank information coefficient per date, with mean, information ratio, t-statistic
  scaled for overlapping horizons, and hit rate. A daily IC of 0.02 with a t above 3 is a real signal;
  an IC of 0.05 with a t of 1 is noise.
* **How fast does it decay?** `decay` shows the IC at every horizon from 1 to 63 sessions. A signal
  that peaks at 1 day and dies by 10 needs daily trading and will be eaten by costs.
* **Is it monotone?** `quantiles` gives the mean forward return per signal decile. A signal driven by
  one extreme decile is fragile.
* **What will it cost to hold?** `turnover` and `autocorr` say how much of the book changes. Turnover
  above 0.5 per rebalance means costs decide the outcome.

Check `by_year` before believing anything. Momentum here earns +38% in 2024 and loses 20% in 2023.

## 3. Trade it

```python
from collective_alpha.backtest.engine import BacktestConfig, CostModel
from collective_alpha.backtest.run import backtest_signal

res = backtest_signal(
    sig,
    scheme="ls_quantile",     # or long_top, signal_weighted
    rebalance=21,             # sessions, or "monthly" / "weekly"
    gross=2.0,
    config=BacktestConfig(delay=1, costs=CostModel(commission_bps=0.5, slippage_bps=2.0)),
    use_spreads=True,         # per-name estimated spreads, not a flat assumption
)
print(res.to_markdown())
```

**Always pass `use_spreads=True` outside the large-cap universe.** The flat 3 bp half-spread is about
right for megacaps and roughly seven times too cheap for microcaps. Weekly reversal across
`all_common` looks marginally profitable under the flat assumption and loses 15.8% a year once each
name is charged its own spread.

A target set on date D executes at the close `delay` sessions later, so `delay=1` means you see the
close, then trade the next one. Weights drift with prices between rebalances. A security's last bar is
followed by liquidation at `delist_return`.

## 4. Manage the risk

Raw decile books are unbalanced: concentrated in whatever sector the signal likes and carrying
whatever market exposure falls out. Portfolio construction fixes both.

```python
from collective_alpha.portfolio.construct import PortfolioConfig
from collective_alpha.portfolio.run import exposure_report, portfolio_feature

cfg = PortfolioConfig(
    method="heuristic",    # or "mvo" for the convex solver
    gross=2.0,
    max_weight=0.02,
    n_names=400,           # keep the strongest longs and shorts
    sector_neutral=True,
    vol_target=0.12,
)
res, diag = portfolio_feature("mom_12_1", "all_common", cfg=cfg, rebalance=21)
print(res.to_markdown(), exposure_report(diag))
```

What that does to five years of momentum on the broad universe:

| book | net return | Sharpe | max drawdown | costs |
|---|---|---|---|---|
| decile spread, flat costs | +34.7% | 1.38 | -33.7% | 1.5% |
| decile spread, real spreads | +32.5% | 1.29 | -33.8% | 3.7% |
| sector neutral, 12% vol target | +17.4% | 2.31 | -7.3% | 0.4% |

Half the return, a fifth of the drawdown, and a much better Sharpe. Which you prefer depends on
whether you can sit through a 34% drawdown.

## Pitfalls this platform already handles, and ones it does not

**Handled.** Delisted names are in the store, so studies are survivorship free. Universe membership is
decided from lagged data and frozen between rebalances. Features use only what was known by the close.
SEC values are usable only from their filing date. Symbol reuse resolves through the security master.

**Not handled, watch yourself.**

* **Multiple testing.** Nothing stops you trying fifty signals and keeping the best. Decide the test
  period before you look, and treat a t-statistic below 3 on a signal you found by searching as noise.
* **Universe shopping.** A signal that only works in `all_common` is usually a small-cap liquidity
  effect. Check it survives in `liquid_1500` with real spreads.
* **Sub-period fragility.** Always read `by_year`.
* **Cost optimism.** The default half-spread flatters small caps. Use `--spreads`.
* **Capacity.** The backtester charges impact only when you ask for it. A book with $10 M of capital
  trading microcaps needs `--impact` and an ADV participation cap.

## Where the walk-forward machinery lives

`collective_alpha.eval.metrics.walk_forward_windows(dates, train_sessions, test_sessions)` returns
rolling train and test windows for anything fitted rather than hand-specified. It is used by nothing
yet; when you start fitting weights across signals, fit inside the train window only.
