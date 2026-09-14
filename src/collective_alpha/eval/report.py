"""Evaluate a signal end to end: attach forward returns, restrict to a universe, compute diagnostics."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from collective_alpha.config import Settings, get_settings
from collective_alpha.eval import metrics as M
from collective_alpha.eval.forward_returns import HORIZONS, forward_returns
from collective_alpha.storage import layout
from collective_alpha.storage.parquet import write_parquet_atomic

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- forward returns table


def fwd_dir(settings: Settings) -> Path:
    return layout.curated_table_dir(settings, "forward_returns")


def build_forward_returns(settings: Settings | None = None, delist_return: float = 0.0) -> dict:
    from collective_alpha.panel.build import load_panel

    s = settings or get_settings()
    panel = load_panel(s, columns=["tr_index", "open", "close", "split_ratio", "is_last_trade"])
    fr = forward_returns(panel, HORIZONS, delist_return=delist_return)
    d = fwd_dir(s)
    for stale in d.glob("year=*/data.parquet"):
        stale.unlink()
    n = 0
    for (year,), part in fr.with_columns(year=pl.col("date").dt.year()).group_by("year"):
        n += write_parquet_atomic(part.drop("year").sort(["date", "security_id"]), d / f"year={year}" / "data.parquet")
    return {"rows": n, "horizons": list(HORIZONS), "delist_return": delist_return}


def load_forward_returns(
    settings: Settings | None = None, start: dt.date | None = None, end: dt.date | None = None
) -> pl.DataFrame:
    s = settings or get_settings()
    lf = pl.scan_parquet(str(fwd_dir(s) / "year=*" / "data.parquet"), hive_partitioning=True).drop("year")
    if start:
        lf = lf.filter(pl.col("date") >= start)
    if end:
        lf = lf.filter(pl.col("date") <= end)
    return lf.collect()


# ---------------------------------------------------------------- evaluation


@dataclass
class SignalReport:
    name: str
    universe: str
    horizon: int
    n_quantiles: int
    ic: pl.DataFrame
    summary: dict
    decay: pl.DataFrame
    quantiles: pl.DataFrame
    long_short: pl.DataFrame
    ls_perf: dict
    autocorr: pl.DataFrame
    turnover: dict
    by_year: pl.DataFrame
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "signal": self.name,
            "universe": self.universe,
            "horizon": self.horizon,
            "ic": self.summary,
            "long_short": self.ls_perf,
            "turnover": self.turnover,
            "decay": self.decay.to_dicts(),
            "quantiles": self.quantiles.to_dicts(),
            "by_year": self.by_year.to_dicts(),
            "autocorr": self.autocorr.to_dicts(),
            "notes": self.notes,
        }

    def to_markdown(self) -> str:
        s = self.summary
        p = self.ls_perf
        lines = [
            f"## {self.name} on {self.universe} (h={self.horizon})",
            "",
            f"IC mean {s.get('ic_mean')}  IR {s.get('ic_ir')}  t {s.get('ic_tstat')}  hit {s.get('hit_rate')}  dates {s.get('n_dates')}  names/date {s.get('names_per_date')}",
            f"Long-short (Q{self.n_quantiles} - Q1): ann. return {p.get('ann_return')}  vol {p.get('ann_vol')}  Sharpe {p.get('sharpe')}  max DD {p.get('max_drawdown')}",
            f"Turnover: top {self.turnover.get('top_turnover')}  bottom {self.turnover.get('bottom_turnover')}",
            "",
            "IC decay: " + ", ".join(f"{r['horizon']}d={r['ic_mean']}" for r in self.decay.to_dicts()),
            "Quantile mean fwd ret (bp): "
            + ", ".join(f"Q{r['q']}={r['mean_ret_bp']}" for r in self.quantiles.to_dicts()),
            "By year IC: " + ", ".join(f"{r['year']}={r['ic_mean']}" for r in self.by_year.to_dicts()),
        ]
        if self.notes:
            lines += ["", *[f"note: {n}" for n in self.notes]]
        return "\n".join(lines)


def evaluate(
    signal: pl.DataFrame,
    fwd: pl.DataFrame,
    name: str = "signal",
    universe: str = "custom",
    horizon: int = 5,
    n_quantiles: int = 10,
    signal_col: str = "signal",
) -> SignalReport:
    """signal: (security_id, date, signal) already restricted to the universe.
    fwd: forward-returns table (security_id, date, fwd_ret_{h}d...)."""
    df = signal.join(fwd, on=["security_id", "date"], how="inner")
    fcol = f"fwd_ret_{horizon}d"
    if fcol not in df.columns:
        raise ValueError(f"no forward return column {fcol}")
    ic = M.rank_ic(df, signal_col, fcol)
    qret = M.quantile_returns(df, signal_col, fcol, n_quantiles)
    ls = M.long_short(qret, n_quantiles)
    members = df.filter(pl.col(signal_col).is_not_null()).with_columns(
        q=(
            (pl.col(signal_col).rank(method="ordinal").over("date") - 1)
            * n_quantiles
            // pl.col(signal_col).count().over("date")
            + 1
        ).cast(pl.Int32)
    )
    notes = []
    cov = df.filter(pl.col(signal_col).is_not_null()).group_by("date").len()["len"]
    if cov.len() and cov.median() < 50:
        notes.append(f"thin cross-section: median {int(cov.median())} names per date")
    return SignalReport(
        name=name,
        universe=universe,
        horizon=horizon,
        n_quantiles=n_quantiles,
        ic=ic,
        summary=M.ic_summary(ic, horizon),
        decay=M.ic_decay(df, signal_col),
        quantiles=M.quantile_summary(qret, horizon),
        long_short=ls,
        ls_perf=M.performance(ls["spread"], horizon=horizon),
        autocorr=M.signal_autocorr(df, signal_col),
        turnover=M.quantile_turnover(members, n_quantiles),
        by_year=M.by_year(ic),
        notes=notes,
    )


def evaluate_feature(
    feature: str,
    universe: str = "liquid_1500",
    horizon: int = 5,
    sign: float = 1.0,
    n_quantiles: int = 10,
    start: dt.date | None = None,
    end: dt.date | None = None,
    neutralize_by: str | None = None,
    settings: Settings | None = None,
) -> SignalReport:
    """Quick look at a raw feature as a signal: cross-sectional rank of sign * feature."""
    from collective_alpha.features.base import list_features, load_features
    from collective_alpha.features.signals import cs_rank, neutralize

    s = settings or get_settings()
    groups = [g for g, cols in list_features(s).items() if feature in cols]
    if not groups:
        raise ValueError(f"feature {feature} not found in any built group")
    f = load_features([groups[0]], s, start, end, universe=universe, columns=[feature]).with_columns(
        signal=pl.col(feature) * sign
    )
    if neutralize_by:
        from collective_alpha.universe.attributes import attributes_asof, load_security_attributes

        attrs = load_security_attributes(s)
        if neutralize_by not in attrs.columns:
            raise ValueError(f"attribute {neutralize_by} not available for neutralisation")
        f = attributes_asof(attrs.select("security_id", "asof", neutralize_by), f).pipe(
            neutralize, "signal", neutralize_by, "signal"
        )
    sig = cs_rank(f, "signal", "signal_rank").select("security_id", "date", pl.col("signal_rank").alias("signal"))
    fwd = load_forward_returns(s, start, end)
    label = f"{'-' if sign < 0 else ''}{feature}" + (f" ~ {neutralize_by}" if neutralize_by else "")
    return evaluate(sig, fwd, name=label, universe=universe, horizon=horizon, n_quantiles=n_quantiles)
