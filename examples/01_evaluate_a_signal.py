"""Evaluate a signal idea end to end: build it, measure it, then trade it with real costs.

    uv run python examples/01_evaluate_a_signal.py

The idea: buy stocks whose closing price sits near the top of the day's range and whose closing
half-hour carries unusual volume, on the theory that end-of-day buying pressure persists. Both
inputs come from the intraday table, so this also shows the minute-bar features in use.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from collective_alpha.backtest.engine import BacktestConfig, CostModel
from collective_alpha.backtest.run import backtest_signal
from collective_alpha.eval.report import evaluate, load_forward_returns
from collective_alpha.features.base import load_features
from collective_alpha.features.signals import combine

UNIVERSE = "liquid_1500"
START = dt.date(2022, 1, 1)


def build_signal() -> pl.DataFrame:
    f = load_features(
        ["intra"],
        start=START,
        universe=UNIVERSE,
        columns=["close_vs_or", "share_close30", "share_close30_21d"],
    )
    # unusual closing-half-hour volume, relative to the name's own recent level
    f = f.with_columns(close30_surprise=pl.col("share_close30") / pl.col("share_close30_21d") - 1)
    # combine() z-scores each input within the date and takes a weighted mean
    return combine(f, {"close_vs_or": 1.0, "close30_surprise": 1.0}).select("security_id", "date", "signal")


def main() -> None:
    sig = build_signal().drop_nulls("signal")
    print(f"signal rows {sig.height:,} over {sig['date'].n_unique():,} sessions")

    fwd = load_forward_returns(start=START)
    for horizon in (1, 5, 21):
        rep = evaluate(sig, fwd, name="close strength", universe=UNIVERSE, horizon=horizon)
        s = rep.summary
        print(
            f"  h={horizon:2d}d  IC {s['ic_mean']:+.4f}  IR {s['ic_ir']:+.2f}  t {s['ic_tstat']:+.1f}  hit {s['hit_rate']:.3f}"
        )

    # Trade it: long-short deciles, weekly, with each name charged its own estimated spread.
    costs = CostModel(commission_bps=0.5, slippage_bps=2.0, borrow_rate=0.005)
    res = backtest_signal(
        sig,
        scheme="ls_quantile",
        rebalance=5,
        gross=2.0,
        config=BacktestConfig(delay=1, costs=costs),
        start=START,
        use_spreads=True,
    )
    print(res.to_markdown())


if __name__ == "__main__":
    main()
