"""Calibration inputs: measured costs joined with the inputs the backtester has on any plan.

The cost models (unimatrix-zero's CostModel, tactical-cube's IntradayCosts) see only daily-bar
inputs: 21-day dollar volume, 63-day volatility, the raw close and the Abdi-Ranaldo estimate.
Fitting on exactly those columns, as unimatrix-zero's Engine.returns computes them, makes the
fitted formulas usable after trades and quotes are gone.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

log = logging.getLogger(__name__)

ETF_TYPES = ("ETF", "ETN", "ETV", "ETS")
STOCK_TYPES = ("CS", "ADRC")
CLUSTER_GAP = timedelta(days=7)      # measured days closer than this share one returns read

MEASURED = ["eff_bps", "eff_ew_bps", "qhs_bps", "rs_5m_bps", "pi_5m_bps", "rs_1m_bps", "pi_1m_bps",
            "dollar_volume", "mdv", "n_matched", "outside_share", "odd_lot_share", "trf_dv",
            "close_vs_mid_bps", "close_vs_mid10_bps", "open_vs_mid1_bps", "qhs_preclose_bps", "qhs_open1_bps",
            "close_auction_volume", "open_auction_volume", "volume", "official_close", "buy_dv", "sell_dv",
            "mid_open1", "mid_close10", "mid_preclose", "n_trades"]


def clusters(days: list[date], gap: timedelta = CLUSTER_GAP) -> list[list[date]]:
    """Sorted days split into runs whose neighbours are less than `gap` apart."""
    out: list[list[date]] = []
    for d in sorted(days):
        if out and d - out[-1][-1] < gap:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def asset_class(type_col: str = "type") -> pl.Expr:
    """'stock' (CS, ADRC, or untyped: old reference data lacks types), 'etf', or 'other'
    (warrants, preferreds, units, rights)."""
    t = pl.col(type_col)
    return (pl.when(t.is_in(ETF_TYPES)).then(pl.lit("etf"))
            .when(t.is_in(STOCK_TYPES) | t.is_null()).then(pl.lit("stock"))
            .otherwise(pl.lit("other")))


def build_inputs(matrix: Any, engine: Any, days: list[date], store: Any = None) -> pl.DataFrame:
    """One row per measured day and security: the measures (MEASURED) and the backtester's
    inputs (adv, vol, raw_close, half_spread from Engine.returns), with `class`."""
    store = store or engine.store
    parts = []
    for run in clusters(days):
        d = matrix.daily(None, run[0], run[-1], lazy=True)
        d = d.filter(pl.col("date").is_in(run), pl.col("security_id").is_not_null()).collect()
        if d.is_empty():
            continue
        ids = d["security_id"].unique().to_list()
        r = engine.returns(ids, run[0], run[-1]).select("security_id", "date", "adv", "vol", "raw_close",
                                                         "half_spread")
        types = pl.concat([store.universe(x).select("security_id", "type").with_columns(pl.lit(x).alias("date"))
                           for x in run]).unique(["security_id", "date"])
        cols = [c for c in MEASURED if c in d.columns]
        parts.append(d.select("date", "ticker", "security_id", *cols)
                     .join(r, on=["security_id", "date"], how="inner")
                     .join(types, on=["security_id", "date"], how="left")
                     .with_columns(asset_class().alias("class")))
        log.info("calibration inputs: %s..%s, %d rows", run[0], run[-1], parts[-1].height)
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="diagonal_relaxed").sort("date", "security_id")


# ---------------------------------------------------------------------------------- the fits
KNOTS = (1e6, 1e7)          # ADV (dollars) where the volume slope may change
REF_ADV, REF_VOL = 1e8, 0.02
MIN_MATCHED = 20            # trades a stock-day needs to enter the spread fit
SIZE_EDGES = (1e6, 1e7, 1e8, 1e9, 1e10)
SIZE_LABELS = ("<1M", "1-10M", "10-100M", "100M-1B", "1-10B", ">10B")
TOD_EDGES = (0, 1, 5, 15, 30, 60, 120, 180, 240, 300, 360, 375, 385, 390)


def size_bucket(edges=SIZE_EDGES, labels=SIZE_LABELS) -> pl.Expr:
    """ADV bucket label (dollars a day)."""
    e = pl.col("adv")
    expr = pl.lit(labels[-1])
    for edge, lab in reversed(list(zip(edges, labels[:-1], strict=True))):
        expr = pl.when(e < edge).then(pl.lit(lab)).otherwise(expr)
    return expr.alias("size")


@dataclass
class FittedSpread:
    """Effective half-spread in bps: level[year] x (ADV/$100M)^adv_slope x prod over knots of
    (max(ADV/knot, 1) / max($100M/knot, 1))^change x (vol/2%)^vol_slope, so level is the
    half-spread at $100M ADV and 2% volatility. Fitted by Poisson pseudo-likelihood (the
    conditional mean, so it is an expected cost) on stock-days with at least MIN_MATCHED trades."""

    adv_slope: float
    knots: list[tuple[float, float]]         # (ADV dollars, slope change above it)
    vol_slope: float
    levels: dict[int, float]                 # year -> bps at $100M ADV and 2% daily volatility
    rows: int = 0

    def predict(self, adv: np.ndarray, vol: np.ndarray, year: np.ndarray) -> np.ndarray:
        years = sorted(self.levels)
        y = np.clip(year, years[0], years[-1])
        lvl = np.array([self.levels.get(int(v), np.nan) for v in y])
        la = np.log(adv / REF_ADV)
        eta = self.adv_slope * la + self.vol_slope * np.log(vol / REF_VOL)
        for k, ch in self.knots:
            eta = eta + ch * _hinge(adv, k)
        return lvl * np.exp(eta)


def _hinge(adv: np.ndarray, knot: float) -> np.ndarray:
    """max(log(ADV/knot), 0), zero at the reference ADV ($100M)."""
    return np.maximum(np.log(adv / knot), 0) - max(np.log(REF_ADV / knot), 0)


def _stock_days(inputs: pl.DataFrame) -> pl.DataFrame:
    return inputs.filter(pl.col("class") == "stock", pl.col("adv") > 0, pl.col("vol") > 0, pl.col("eff_bps") > 0,
                         pl.col("outside_share") < 0.2, pl.col("n_matched") >= MIN_MATCHED)


def fit_spread(inputs: pl.DataFrame, knots=KNOTS, iters: int = 50) -> FittedSpread:
    """Poisson regression (log link) of eff_bps on log ADV with hinges, log volatility and a level
    per year, by Newton's method."""
    d = _stock_days(inputs).with_columns(pl.col("date").dt.year().alias("year"))
    years = sorted(d["year"].unique().to_list())
    yi = {v: i for i, v in enumerate(years)}
    adv, vol, y = d["adv"].to_numpy(), d["vol"].to_numpy(), d["eff_bps"].to_numpy()
    la = np.log(adv / REF_ADV)
    Z = np.column_stack([la, np.log(vol / REF_VOL)] + [_hinge(adv, k) for k in knots])
    idx = np.array([yi[v] for v in d["year"].to_numpy()])
    ny, k = len(years), Z.shape[1]
    th = np.concatenate([np.full(ny, np.log(np.mean(y))), np.zeros(k)])
    for _ in range(iters):
        mu = np.exp(th[idx] + Z @ th[ny:])
        r = y - mu
        g = np.concatenate([np.bincount(idx, r, ny), Z.T @ r])
        H = np.zeros((ny + k, ny + k))
        H[np.arange(ny), np.arange(ny)] = np.bincount(idx, mu, ny)
        Zm = Z * mu[:, None]
        cross = np.zeros((ny, k))
        for j in range(k):
            cross[:, j] = np.bincount(idx, Zm[:, j], ny)
        H[:ny, ny:], H[ny:, :ny], H[ny:, ny:] = cross, cross.T, Z.T @ Zm
        step = np.linalg.solve(H, g)
        th = th + step
        if np.max(np.abs(step)) < 1e-9:
            break
    b = th[ny:]
    return FittedSpread(adv_slope=float(b[0]), vol_slope=float(b[1]),
                     knots=[(float(kn), float(c)) for kn, c in zip(knots, b[2:], strict=True)],
                     levels={int(v): float(np.exp(th[i])) for i, v in enumerate(years)}, rows=d.height)


def volume_rule(adv: np.ndarray, coef: float = 30.0) -> np.ndarray:
    """unimatrix-zero's former default: coef x (ADV in $M)^-0.5, clipped to [0.5, 200] bps."""
    return np.clip(coef * (adv / 1e6) ** -0.5, 0.5, 200.0)


def compare(inputs: pl.DataFrame, fit: FittedSpread, start: date | None = None) -> pl.DataFrame:
    """Measured vs predicted per ADV bucket: mean bps, median ratio and dollar-weighted ratio
    (sum of predicted x dollar volume over measured x dollar volume) for the fit and the
    volume rule."""
    d = _stock_days(inputs)
    if start:
        d = d.filter(pl.col("date") >= start)
    pred = fit.predict(d["adv"].to_numpy(), d["vol"].to_numpy(), d["date"].dt.year().to_numpy())
    d = d.with_columns(pl.Series("fit", pred), pl.Series("rule", volume_rule(d["adv"].to_numpy())), size_bucket())

    def stats(c: str) -> list[pl.Expr]:
        return [pl.col(c).mean().alias(f"{c}_bps"), (pl.col(c) / pl.col("eff_bps")).median().alias(f"{c}_ratio"),
                ((pl.col(c) * pl.col("dollar_volume")).sum() / (pl.col("eff_bps") * pl.col("dollar_volume")).sum())
                .alias(f"{c}_dw_ratio")]
    order = {lab: i for i, lab in enumerate(SIZE_LABELS)}
    return (d.group_by("size").agg(pl.len().alias("stock_days"), pl.col("eff_bps").mean().alias("measured_bps"),
                                   *stats("fit"), *stats("rule"))
            .sort(pl.col("size").replace_strict(order, return_dtype=pl.Int8)))


def close_table(inputs: pl.DataFrame, start: date | None = None) -> pl.DataFrame:
    """The official close against the pre-close midpoint per ADV bucket: signed median and mean
    (the cost of a market-on-close order of either side, if the close were not the mark), mean
    absolute deviation, the pre-close quoted half-spread and the closing auction's share of the
    day's volume (days with a flagged auction print)."""
    d = inputs.filter(pl.col("class") == "stock", pl.col("adv") > 0, pl.col("outside_share") < 0.2,
                      pl.col("n_matched") >= MIN_MATCHED, pl.col("close_vs_mid_bps").is_not_null())
    if start:
        d = d.filter(pl.col("date") >= start)
    order = {lab: i for i, lab in enumerate(SIZE_LABELS)}
    share = pl.col("close_auction_volume") / pl.col("volume")
    return (d.with_columns(size_bucket()).group_by("size")
            .agg(pl.len().alias("stock_days"), pl.col("close_vs_mid_bps").median().alias("signed_median_bps"),
                 pl.col("close_vs_mid_bps").mean().alias("signed_mean_bps"),
                 pl.col("close_vs_mid_bps").abs().mean().alias("abs_mean_bps"),
                 pl.col("qhs_preclose_bps").median().alias("preclose_qhs_bps"),
                 share.median().alias("auction_share"))
            .sort(pl.col("size").replace_strict(order, return_dtype=pl.Int8)))


def impact_table(inputs: pl.DataFrame, start: date | None = None) -> pl.DataFrame:
    """Square-root impact coefficient per ADV bucket: the session's mid return (open+1 minute to
    just before the close) net of the day's median among names over $100M ADV, in units of daily
    volatility, regressed through the origin on sign(f) sqrt(|f|), f = Lee-Ready net dollar flow /
    ADV. Comparable with CostModel.impact_coef. Tape flow mixes informed and uninformed trading,
    so this is a range, not a measurement of one's own orders."""
    d = inputs.filter(pl.col("class") == "stock", pl.col("adv") > 0, pl.col("vol") > 0, pl.col("n_matched") >= 50,
                      pl.col("outside_share") < 0.2, pl.col("mid_open1") > 0, pl.col("mid_preclose") > 0)
    if start:
        d = d.filter(pl.col("date") >= start)
    d = d.with_columns(r=(pl.col("mid_preclose") / pl.col("mid_open1")).log(),
                       f=(pl.col("buy_dv") - pl.col("sell_dv")) / pl.col("adv"))
    mkt = d.filter(pl.col("adv") > 1e8).group_by("date").agg(pl.col("r").median().alias("rm"))
    d = (d.join(mkt, on="date").with_columns(y=(pl.col("r") - pl.col("rm")) / pl.col("vol"),
                                             z=pl.col("f").sign() * pl.col("f").abs().sqrt())
         .filter(pl.col("f").abs() < 1, pl.col("y").abs() < 10).with_columns(size_bucket()))
    coef = ((pl.col("z") * pl.col("y")).sum() / (pl.col("z") ** 2).sum())
    order = {lab: i for i, lab in enumerate(SIZE_LABELS)}
    by = (d.group_by("size").agg(pl.len().alias("stock_days"), coef.alias("impact_coef"),
                                 pl.col("f").abs().median().alias("median_abs_flow"),
                                 pl.col("pi_5m_bps").mean().alias("pi_5m_bps"))
          .sort(pl.col("size").replace_strict(order, return_dtype=pl.Int8)))
    allrow = d.select(pl.lit("all").alias("size"), pl.len().alias("stock_days"), coef.alias("impact_coef"),
                      pl.col("f").abs().median().alias("median_abs_flow"), pl.col("pi_5m_bps").mean().alias("pi_5m_bps"))
    return pl.concat([by, allrow], how="vertical_relaxed")


def tod_segments(matrix: Any, days: list[date], edges=TOD_EDGES) -> pl.DataFrame:
    """Per day, security and session segment (start minute): dollar-weighted effective
    half-spread, matched dollar volume and time-weighted quoted half-spread."""
    labels = [str(e) for e in edges[:-1]]
    seg = pl.col("minute").cut(list(edges[1:-1]), left_closed=True, labels=labels).cast(pl.String).cast(pl.Int16)
    qw = pl.col("q_ns").cast(pl.Float64)
    out = []
    for d in days:
        lf = matrix.minute(None, d, d, lazy=True)
        out.append(lf.filter(pl.col("security_id").is_not_null()).with_columns(seg.alias("seg"))
                   .group_by("security_id", "seg")
                   .agg(((pl.col("eff_bps") * pl.col("mdv")).sum()
                         / pl.col("mdv").filter(pl.col("eff_bps").is_not_null()).sum()).alias("eff"),
                        ((pl.col("qhs_bps") * qw).sum() / qw.filter(pl.col("qhs_bps").is_not_null()).sum()).alias("qhs"),
                        pl.col("mdv").sum().alias("mdv"))
                   .collect().with_columns(pl.lit(d).alias("date")))
    return pl.concat(out) if out else pl.DataFrame()


def tod_profile(segments: pl.DataFrame, inputs: pl.DataFrame, min_adv: float = 1e7) -> pl.DataFrame:
    """Effective half-spread in each segment relative to the day's (dollar-weighted, so the
    profile averages to 1 over the day's trading), for stocks over `min_adv`: per segment start
    minute, the multiplier tactical-cube applies to a bar starting there."""
    d = (segments.join(inputs.select("date", "security_id", "eff_bps", "adv", "class"), on=["date", "security_id"])
         .filter(pl.col("class") == "stock", pl.col("adv") >= min_adv, pl.col("eff_bps") > 0, pl.col("mdv") > 0,
                 pl.col("eff").is_not_null()))
    return (d.group_by("seg").agg(((pl.col("eff") * pl.col("mdv")).sum() / (pl.col("eff_bps") * pl.col("mdv")).sum())
                                  .alias("multiplier"), pl.len().alias("rows"))
            .sort("seg").rename({"seg": "start_minute"}))


@dataclass
class Calibration:
    """Everything `AdaptationMatrix.calibrate` measured (see decision 0017)."""

    spread: FittedSpread
    validation: pl.DataFrame          # held-out period, per ADV bucket
    close: pl.DataFrame
    impact: pl.DataFrame
    tod: pl.DataFrame
    test_start: date | None = None
    notes: list[str] = field(default_factory=list)

    def tod_pairs(self) -> tuple[tuple[int, float], ...]:
        """tactical-cube IntradayCosts.tod: (minutes, multiplier) pairs, a bar starting before
        `minutes` takes the multiplier (the segment's end, since the profile is by start)."""
        starts = self.tod["start_minute"].to_list()
        ends = starts[1:] + [TOD_EDGES[-1]]
        return tuple((int(e), round(float(m), 2)) for e, m in zip(ends, self.tod["multiplier"].to_list(), strict=True))

    def to_json(self) -> str:
        return json.dumps({"spread": asdict(self.spread), "tod": self.tod_pairs(),
                           "test_start": str(self.test_start) if self.test_start else None,
                           "validation": self.validation.to_dicts(), "close": self.close.to_dicts(),
                           "impact": self.impact.to_dicts(), "notes": self.notes}, indent=1, default=str)
