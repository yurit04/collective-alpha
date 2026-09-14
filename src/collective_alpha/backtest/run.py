"""Backtest a feature-based signal end to end on the curated store."""

from __future__ import annotations

import datetime as dt

import polars as pl

from collective_alpha.backtest.engine import BacktestConfig, BacktestResult, CostModel, run_backtest
from collective_alpha.backtest.weights import long_short_quantiles, on_rebalance_dates, signal_weighted
from collective_alpha.config import Settings, get_settings


def backtest_signal(
    signal: pl.DataFrame,
    scheme: str = "ls_quantile",
    rebalance: int | str = 21,
    n_quantiles: int = 10,
    gross: float = 2.0,
    max_weight: float | None = 0.05,
    config: BacktestConfig | None = None,
    settings: Settings | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    use_impact: bool = False,
) -> BacktestResult:
    """signal: (security_id, date, signal) restricted to the tradable universe."""
    from collective_alpha.panel.build import load_panel

    s = settings or get_settings()
    dates = sorted(set(signal["date"].to_list()))
    if scheme == "ls_quantile":
        w = long_short_quantiles(signal, n_quantiles, gross=gross, max_weight=max_weight)
    elif scheme == "long_top":
        w = long_short_quantiles(
            signal, n_quantiles, short_q=None, gross=gross / 2 if gross > 1 else gross, max_weight=max_weight
        )
    elif scheme == "signal_weighted":
        w = signal_weighted(signal, gross=gross, max_weight=max_weight)
    else:
        raise ValueError(f"unknown scheme {scheme}")
    w = on_rebalance_dates(w, dates, rebalance)
    panel = load_panel(s, start=start, end=end, columns=["ret", "is_last_trade"])
    # keep the panel to securities that ever get a target plus everything needed for their history
    ids = w["security_id"].unique()
    panel = panel.filter(pl.col("security_id").is_in(ids))
    adv = None
    if use_impact:
        from collective_alpha.features.base import load_features

        adv = (
            load_features(["price"], s, start, end, columns=["adv_21d"])
            .rename({"adv_21d": "adv"})
            .filter(pl.col("security_id").is_in(ids))
        )
    return run_backtest(w, panel, config, adv)


def backtest_feature(
    feature: str,
    universe: str = "liquid_1500",
    sign: float = 1.0,
    scheme: str = "ls_quantile",
    rebalance: int | str = 21,
    n_quantiles: int = 10,
    gross: float = 2.0,
    max_weight: float | None = 0.05,
    costs: CostModel | None = None,
    delay: int = 1,
    start: dt.date | None = None,
    end: dt.date | None = None,
    use_impact: bool = False,
    settings: Settings | None = None,
) -> BacktestResult:
    from collective_alpha.features.base import list_features, load_features

    s = settings or get_settings()
    groups = [g for g, cols in list_features(s).items() if feature in cols]
    if not groups:
        raise ValueError(f"feature {feature} not found in any built group")
    f = load_features([groups[0]], s, start, end, universe=universe, columns=[feature])
    sig = f.with_columns(signal=pl.col(feature) * sign).select("security_id", "date", "signal")
    cfg = BacktestConfig(delay=delay, costs=costs or CostModel())
    return backtest_signal(sig, scheme, rebalance, n_quantiles, gross, max_weight, cfg, s, start, end, use_impact)
