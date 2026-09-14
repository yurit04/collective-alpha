"""Strategy definition (TOML) -> signal for a date -> target weights."""

from __future__ import annotations

import datetime as dt
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import polars as pl

from collective_alpha.backtest.weights import rebalance_dates
from collective_alpha.config import REPO_ROOT, Settings, get_settings
from collective_alpha.portfolio.construct import PortfolioConfig, build_weights
from collective_alpha.portfolio.risk import fit_risk_model
from collective_alpha.portfolio.sectors import sector_table

STRATEGIES_DIR = REPO_ROOT / "config" / "strategies"


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    universe: str = "liquid_1500"
    signals: dict[str, float] = field(default_factory=dict)  # feature -> weight (sign included)
    rebalance: int | str = "monthly"
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    capital: float = 100_000.0
    risk_window: int = 252
    n_factors: int = 10
    min_notional: float = 200.0
    broker: str = "paper"

    @classmethod
    def load(cls, path: Path | str) -> StrategyConfig:
        p = Path(path)
        if not p.exists() and (STRATEGIES_DIR / f"{path}.toml").exists():
            p = STRATEGIES_DIR / f"{path}.toml"
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
        pf = PortfolioConfig(**raw.get("portfolio", {}))
        rb = raw.get("rebalance", "monthly")
        return cls(
            name=raw.get("name", p.stem),
            universe=raw.get("universe", "liquid_1500"),
            signals=dict(raw.get("signals", {})),
            rebalance=int(rb) if isinstance(rb, int) or (isinstance(rb, str) and rb.isdigit()) else rb,
            portfolio=pf,
            capital=float(raw.get("capital", 100_000)),
            risk_window=int(raw.get("risk_window", 252)),
            n_factors=int(raw.get("n_factors", 10)),
            min_notional=float(raw.get("min_notional", 200)),
            broker=raw.get("broker", "paper"),
        )


def build_signal(
    cfg: StrategyConfig, settings: Settings, start: dt.date | None = None, end: dt.date | None = None
) -> pl.DataFrame:
    """(security_id, date, signal) on the strategy universe: weighted sum of z-scored features."""
    from collective_alpha.features.base import list_features, load_features
    from collective_alpha.features.signals import combine

    avail = list_features(settings)
    groups = sorted({g for f in cfg.signals for g, cols in avail.items() if f in cols})
    missing = [f for f in cfg.signals if not any(f in cols for cols in avail.values())]
    if missing:
        raise ValueError(f"features not built: {missing}")
    df = load_features(groups, settings, start, end, universe=cfg.universe, columns=list(cfg.signals))
    if len(cfg.signals) == 1:
        ((f, w),) = cfg.signals.items()
        return (
            df.with_columns(signal=pl.col(f) * w)
            .select("security_id", "date", "signal")
            .filter(pl.col("signal").is_not_null())
        )
    return (
        combine(df, cfg.signals, "signal")
        .select("security_id", "date", "signal")
        .filter(pl.col("signal").is_not_null())
    )


def is_rebalance_day(cfg: StrategyConfig, sessions: list[dt.date], date: dt.date) -> bool:
    return date in rebalance_dates(sessions, cfg.rebalance)


def targets_for_date(
    cfg: StrategyConfig, date: dt.date, settings: Settings | None = None
) -> tuple[dict[str, float], dict]:
    """Target weights (security_id -> weight) from the signal at `date`, using the risk model fitted
    on returns up to and including `date`. Returns (targets, diagnostics)."""
    from collective_alpha.panel.build import load_panel
    from collective_alpha.universe.builder import load_master

    s = settings or get_settings()
    sig = build_signal(cfg, s, date, date)
    if sig.height == 0:
        return {}, {"date": str(date), "n_signal": 0}
    ids = sorted(sig["security_id"].to_list())
    lookback_start = date - dt.timedelta(days=int(cfg.risk_window * 1.6))
    panel = load_panel(s, lookback_start, date, columns=["ret"]).filter(pl.col("security_id").is_in(ids))
    dates = sorted(set(panel["date"].to_list()))[-cfg.risk_window :]
    panel = panel.filter(pl.col("date").is_in(dates))
    di = {d: i for i, d in enumerate(dates)}
    si = {x: j for j, x in enumerate(ids)}
    R = np.full((len(dates), len(ids)), np.nan)
    R[[di[d] for d in panel["date"].to_list()], [si[x] for x in panel["security_id"].to_list()]] = panel[
        "ret"
    ].to_numpy()
    risk = fit_risk_model(R, ids, k=cfg.n_factors)
    parts = sorted(p for p in (s.curated_dir / "ticker_details").glob("asof=*") if p.is_dir())
    sectors = None
    if parts:
        st = sector_table(pl.read_parquet(parts[-1] / "data.parquet"), load_master(s))
        m = dict(zip(st["security_id"].to_list(), st["sector"].to_list(), strict=True))
        sectors = np.array([m.get(x, "Unknown") for x in ids])
    alpha = np.full(len(ids), np.nan)
    alpha[[si[x] for x in sig["security_id"].to_list()]] = sig["signal"].to_numpy()
    w = build_weights(alpha, sectors, cfg.portfolio, risk)
    targets = {ids[j]: float(w[j]) for j in np.nonzero(w)[0]}
    diag = {
        "date": str(date),
        "n_signal": sig.height,
        "n_targets": len(targets),
        "gross": round(float(np.abs(w).sum()), 3),
        "net": round(float(w.sum()), 3),
        "pred_vol": round(risk.portfolio_vol(w), 4),
        "beta": round(risk.beta(w), 3),
    }
    return targets, diag
