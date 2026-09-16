"""Screen the whole feature library against forward returns, with a multiple-testing correction.

Testing ninety signals and keeping the best is the fastest way to find something that is not there.
Every feature-horizon pair counts as one test and significance is decided by Benjamini-Hochberg on
the whole family, not by a t-statistic read one row at a time.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from collective_alpha.config import Settings, get_settings

log = logging.getLogger(__name__)

# Identifiers, counts and diagnostics rather than candidate signals.
EXCLUDE = {
    "shares_source",
    "bar_coverage",
    "bar_coverage_21d",
    "suspect_split_252d",
    "n_bars",
    "news_sent_n",
    "news_sent",
    "si_adv",
}

HORIZONS = (1, 5, 21, 63)


@dataclass(frozen=True)
class ScreenConfig:
    universe: str = "liquid_1500"
    horizons: tuple[int, ...] = HORIZONS
    start: dt.date | None = None
    end: dt.date | None = None
    min_names: int = 50
    fdr_q: float = 0.10
    min_abs_ic: float = 0.01
    min_coverage: float = 0.50
    min_consistent_years: int = 3
    exclude: frozenset[str] = field(default_factory=lambda: frozenset(EXCLUDE))


def _t_to_p(t: float, n: int) -> float:
    """Two-sided p-value from a t-statistic, normal approximation (n is large here)."""
    if not math.isfinite(t):
        return 1.0
    return math.erfc(abs(t) / math.sqrt(2))


def benjamini_hochberg(p: np.ndarray, q: float = 0.10) -> np.ndarray:
    """Boolean mask of hypotheses that survive a false-discovery-rate of q."""
    n = p.size
    if n == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(p)
    ranked = p[order]
    thresh = q * (np.arange(1, n + 1) / n)
    below = ranked <= thresh
    keep = np.zeros(n, dtype=bool)
    if below.any():
        cutoff = np.max(np.nonzero(below)[0])
        keep[order[: cutoff + 1]] = True
    return keep


def ic_series(df: pl.DataFrame, feature: str, fwd: str, min_names: int) -> pl.DataFrame:
    """Per-date rank IC for one feature and one forward-return column."""
    d = df.select("date", feature, fwd).drop_nulls()
    return (
        d.group_by("date")
        .agg(ic=pl.corr(pl.col(feature).rank(), pl.col(fwd).rank()), n=pl.len())
        .filter(pl.col("n") >= min_names)
        .sort("date")
    )


def _summarise(ic: pl.DataFrame, horizon: int) -> dict:
    s = ic["ic"].drop_nulls()
    n = s.len()
    if n < 10:
        return {"n_dates": n}
    mean, std = float(s.mean()), float(s.std())
    n_eff = max(n / horizon, 2)
    t = mean / (std / math.sqrt(n_eff)) if std > 0 else float("nan")
    by_year = ic.group_by(pl.col("date").dt.year().alias("year")).agg(m=pl.col("ic").mean()).sort("year")
    same_sign = int((np.sign(by_year["m"].to_numpy()) == np.sign(mean)).sum())
    return {
        "n_dates": n,
        "ic_mean": mean,
        "ic_std": std,
        "ic_ir": mean / std if std > 0 else float("nan"),
        "t_stat": t,
        "p_value": _t_to_p(t, int(n_eff)),
        "hit_rate": float((s > 0).mean()),
        "years": by_year.height,
        "consistent_years": same_sign,
        "names_per_date": int(ic["n"].median()),
    }


def run_screen(cfg: ScreenConfig | None = None, settings: Settings | None = None) -> pl.DataFrame:
    """One row per (feature, horizon) with IC statistics and the family-wide significance decision."""
    from collective_alpha.eval.report import load_forward_returns
    from collective_alpha.features.base import list_features, load_features

    cfg = cfg or ScreenConfig()
    s = settings or get_settings()
    groups = list_features(s)
    feats = [c for cols in groups.values() for c in cols if c not in cfg.exclude]
    log.info("screening %d features x %d horizons on %s", len(feats), len(cfg.horizons), cfg.universe)

    df = load_features(list(groups), s, cfg.start, cfg.end, universe=cfg.universe)
    fwd = load_forward_returns(s, cfg.start, cfg.end)
    cols = [f"fwd_ret_{h}d" for h in cfg.horizons]
    df = df.join(fwd.select(["security_id", "date", *cols]), on=["security_id", "date"], how="inner")
    member_days = df.height
    log.info("panel: %d rows, %d dates", member_days, df["date"].n_unique())

    rows = []
    for i, f in enumerate(feats, 1):
        coverage = float(df[f].is_not_null().mean()) if f in df.columns else 0.0
        if coverage == 0.0:
            continue
        group = next(g for g, cols_ in groups.items() if f in cols_)
        for h in cfg.horizons:
            ic = ic_series(df, f, f"fwd_ret_{h}d", cfg.min_names)
            rows.append({"feature": f, "group": group, "horizon": h, "coverage": coverage, **_summarise(ic, h)})
        if i % 20 == 0:
            log.info("  %d/%d features", i, len(feats))

    out = pl.DataFrame(rows)
    if out.height == 0:
        return out
    out = out.filter(pl.col("p_value").is_not_null())
    keep = benjamini_hochberg(out["p_value"].to_numpy(), cfg.fdr_q)
    out = out.with_columns(
        bh_significant=pl.Series(keep),
        abs_t=pl.col("t_stat").abs(),
    ).with_columns(
        survivor=(
            pl.col("bh_significant")
            & (pl.col("ic_mean").abs() >= cfg.min_abs_ic)
            & (pl.col("consistent_years") >= cfg.min_consistent_years)
            & (pl.col("coverage") >= cfg.min_coverage)
        )
    )
    return out.sort("abs_t", descending=True)


def screen_path(settings: Settings, tag: str) -> object:
    from pathlib import Path

    p: Path = settings.data_root / "research" / f"screen_{tag}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def ic_matrix(features: list[str], horizon: int, cfg: ScreenConfig, settings: Settings | None = None) -> pl.DataFrame:
    """Daily IC series for several features side by side, for clustering."""
    from collective_alpha.eval.report import load_forward_returns
    from collective_alpha.features.base import list_features, load_features

    s = settings or get_settings()
    groups = list_features(s)
    need = [g for g, cols in groups.items() if any(f in cols for f in features)]
    df = load_features(need, s, cfg.start, cfg.end, universe=cfg.universe, columns=features)
    fwd = load_forward_returns(s, cfg.start, cfg.end).select("security_id", "date", f"fwd_ret_{horizon}d")
    df = df.join(fwd, on=["security_id", "date"], how="inner")
    out: pl.DataFrame | None = None
    for f in features:
        ic = ic_series(df, f, f"fwd_ret_{horizon}d", cfg.min_names).select("date", pl.col("ic").alias(f))
        out = ic if out is None else out.join(ic, on="date", how="full", coalesce=True)
    return out.sort("date") if out is not None else pl.DataFrame()


def cluster_by_ic(ic_wide: pl.DataFrame, threshold: float = 0.5) -> dict[str, int]:
    """Single-link clustering on the absolute correlation of daily IC series."""
    feats = [c for c in ic_wide.columns if c != "date"]
    if not feats:
        return {}
    m = ic_wide.select(feats).to_numpy()
    valid = ~np.isnan(m)
    corr = np.eye(len(feats))
    for a in range(len(feats)):
        for b in range(a + 1, len(feats)):
            both = valid[:, a] & valid[:, b]
            if both.sum() > 30:
                x, y = m[both, a], m[both, b]
                sx, sy = x.std(), y.std()
                corr[a, b] = corr[b, a] = (
                    float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy)) if sx > 0 and sy > 0 else 0.0
                )
    parent = list(range(len(feats)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a in range(len(feats)):
        for b in range(a + 1, len(feats)):
            if abs(corr[a, b]) >= threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
    roots = {}
    out = {}
    for i, f in enumerate(feats):
        r = find(i)
        out[f] = roots.setdefault(r, len(roots))
    return out
