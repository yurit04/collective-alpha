"""Rebalance loop: at each rebalance date fit the risk model on trailing returns, build weights
from the signal, and hand the targets to the backtest engine. Also reports exposures."""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import polars as pl

from collective_alpha.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from collective_alpha.backtest.weights import rebalance_dates
from collective_alpha.config import Settings, get_settings
from collective_alpha.portfolio.construct import PortfolioConfig, build_weights
from collective_alpha.portfolio.risk import RiskModel, fit_risk_model
from collective_alpha.portfolio.sectors import sector_table

log = logging.getLogger(__name__)


def _returns_wide(panel: pl.DataFrame) -> tuple[list[dt.date], list[str], np.ndarray]:
    dates = sorted(set(panel["date"].to_list()))
    ids = sorted(set(panel["security_id"].to_list()))
    di = {d: i for i, d in enumerate(dates)}
    si = {s: j for j, s in enumerate(ids)}
    R = np.full((len(dates), len(ids)), np.nan)
    R[[di[d] for d in panel["date"].to_list()], [si[s] for s in panel["security_id"].to_list()]] = panel[
        "ret"
    ].to_numpy()
    return dates, ids, R


def construct_targets(
    signal: pl.DataFrame,
    panel: pl.DataFrame,
    sectors: pl.DataFrame | None,
    cfg: PortfolioConfig,
    rebalance: int | str = 21,
    risk_window: int = 252,
    n_factors: int = 10,
    adv: pl.DataFrame | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns (targets (security_id, date, weight), diagnostics per rebalance date)."""
    dates, ids, R = _returns_wide(panel)
    si = {s: j for j, s in enumerate(ids)}
    sec_arr = None
    if sectors is not None:
        m = dict(zip(sectors["security_id"].to_list(), sectors["sector"].to_list(), strict=True))
        sec_arr = np.array([m.get(s, "Unknown") for s in ids])
    adv_map = None
    if adv is not None:
        adv_map = {(s, d): v for s, d, v in adv.select("security_id", "date", "adv").iter_rows()}
    rd = sorted(d for d in rebalance_dates(sorted(set(signal["date"].to_list())), rebalance))
    sig_by_date = {d: g for (d,), g in signal.group_by("date")}
    prev = np.zeros(len(ids))
    rows = []
    diag = []
    for d in rd:
        if d not in sig_by_date:
            continue
        i = dates.index(d) if d in dates else None
        if i is None or i < 60:
            continue
        window = R[max(0, i - risk_window + 1) : i + 1]
        risk: RiskModel = fit_risk_model(window, ids, k=n_factors)
        g = sig_by_date[d]
        alpha = np.full(len(ids), np.nan)
        alpha[[si[s] for s in g["security_id"].to_list()]] = g["signal"].to_numpy()
        adv_arr = None
        if adv_map is not None:
            adv_arr = np.array([adv_map.get((s, d), np.nan) for s in ids])
        w = build_weights(alpha, sec_arr, cfg, risk, prev, adv_arr)
        nz = np.nonzero(w)[0]
        rows += [(s_id, d, float(w[j])) for j, s_id in ((j, ids[j]) for j in nz)]
        diag.append(
            {
                "date": d,
                "n": int(len(nz)),
                "gross": float(np.abs(w).sum()),
                "net": float(w.sum()),
                "pred_vol": risk.portfolio_vol(w),
                "beta": risk.beta(w),
                "turnover": float(np.abs(w - prev).sum()),
                "max_abs_w": float(np.abs(w).max()) if len(nz) else 0.0,
                "max_sector": float(np.max(np.abs([w[sec_arr == s].sum() for s in np.unique(sec_arr)])))
                if sec_arr is not None
                else None,
            }
        )
        prev = w
    targets = pl.DataFrame(rows, schema=["security_id", "date", "weight"], orient="row")
    return targets, pl.DataFrame(diag)


def backtest_portfolio(
    signal: pl.DataFrame,
    cfg: PortfolioConfig,
    rebalance: int | str = 21,
    bt: BacktestConfig | None = None,
    settings: Settings | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    risk_window: int = 252,
    n_factors: int = 10,
) -> tuple[BacktestResult, pl.DataFrame]:
    from collective_alpha.panel.build import load_panel
    from collective_alpha.universe.builder import load_master

    s = settings or get_settings()
    ids = signal["security_id"].unique()
    panel = load_panel(s, start=start, end=end, columns=["ret", "is_last_trade"]).filter(
        pl.col("security_id").is_in(ids)
    )
    parts = sorted(p for p in (s.curated_dir / "ticker_details").glob("asof=*") if p.is_dir())
    sectors = sector_table(pl.read_parquet(parts[-1] / "data.parquet"), load_master(s)) if parts else None
    adv = None
    if cfg.max_adv_participation:
        from collective_alpha.features.base import load_features

        adv = (
            load_features(["price"], s, start, end, columns=["adv_21d"])
            .rename({"adv_21d": "adv"})
            .filter(pl.col("security_id").is_in(ids))
        )
    targets, diag = construct_targets(signal, panel, sectors, cfg, rebalance, risk_window, n_factors, adv)
    res = run_backtest(targets, panel, bt or BacktestConfig())
    return res, diag


def portfolio_feature(
    feature: str,
    universe: str = "liquid_1500",
    sign: float = 1.0,
    cfg: PortfolioConfig | None = None,
    rebalance: int | str = 21,
    bt: BacktestConfig | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    settings: Settings | None = None,
) -> tuple[BacktestResult, pl.DataFrame]:
    from collective_alpha.features.base import list_features, load_features

    s = settings or get_settings()
    groups = [g for g, cols in list_features(s).items() if feature in cols]
    if not groups:
        raise ValueError(f"feature {feature} not found")
    f = load_features([groups[0]], s, start, end, universe=universe, columns=[feature])
    sig = (
        f.with_columns(signal=pl.col(feature) * sign)
        .select("security_id", "date", "signal")
        .filter(pl.col("signal").is_not_null())
    )
    return backtest_portfolio(sig, cfg or PortfolioConfig(), rebalance, bt, s, start, end)


def exposure_report(diag: pl.DataFrame) -> dict:
    if diag.height == 0:
        return {}
    return {
        "rebalances": diag.height,
        "avg_names": round(float(diag["n"].mean()), 1),
        "avg_gross": round(float(diag["gross"].mean()), 3),
        "avg_net": round(float(diag["net"].mean()), 3),
        "avg_pred_vol": round(float(diag["pred_vol"].mean()), 4),
        "avg_abs_beta": round(float(diag["beta"].abs().mean()), 3),
        "avg_turnover_per_rebalance": round(float(diag["turnover"].mean()), 3),
        "max_abs_weight": round(float(diag["max_abs_w"].max()), 4),
        "max_sector_exposure": round(float(diag["max_sector"].max()), 4)
        if diag["max_sector"].null_count() < diag.height
        else None,
    }
