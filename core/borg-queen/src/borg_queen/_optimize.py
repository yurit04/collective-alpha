"""Portfolio construction: expected returns from scores, a cost-aware optimizer on the risk model,
and a rebalance loop that carries holdings forward between rebalances."""

from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

import cvxpy as cp
import numpy as np
import polars as pl

from ._estimate import design
from ._model import ANNUAL, RiskModel, _day
from ._styles import FACTORS, SECTORS, STYLES

log = logging.getLogger("borg_queen")
SCALE = 1e4          # objective in basis points
S = 100.0            # solver weights in percent of capital
# Clarabel stops at 1e-8 by default and often reports "optimal_inaccurate" on these problems
# with the same answer; 1e-7 reaches "optimal".
SOLVER_OPTS = {"tol_gap_abs": 1e-7, "tol_gap_rel": 1e-7, "tol_feas": 1e-7}
TINY = 1e-6          # weights below this are dropped


@dataclass(frozen=True)
class Constraints:
    """Limits for Optimizer. Weights are fractions of capital."""

    long_short: bool = True                # net 0 (else long-only: fully invested, no shorts)
    gross: float = 2.0                     # sum of |w| (long-short)
    target_vol: float | None = 0.08        # annual forecast volatility cap; None: risk_aversion only
    risk_aversion: float = 0.0             # x forecast variance over the horizon, in the objective
    sector_net: float | None = 0.01        # |net weight| per sector (long-short); None: off
    beta: float | None = 0.05              # |predicted beta to the cap-weighted universe|; None: off
    styles: tuple[tuple[str, float], ...] = (("size", 0.1),)   # |style exposure| caps
    max_weight: float = 0.02               # |w| per name
    max_participation: float | None = 0.10  # |trade| x AUM <= this x ADV (only with an AUM)
    cost_aversion: float = 1.0             # x the expected trading cost in the objective
    horizon_days: int = 21                 # alpha and risk horizon (one month)

    @classmethod
    def long_only(cls, **kw) -> Constraints:
        base = {"long_short": False, "gross": 1.0, "target_vol": None, "sector_net": None, "beta": None,
                "styles": ()}
        return cls(**{**base, **kw})


@dataclass
class Solution:
    weights: pl.DataFrame                  # security_id, weight
    info: dict = field(default_factory=dict)


def alpha_from_score(panel: pl.DataFrame, col: str, risk: RiskModel, ic: float = 0.03,
                     horizon_days: int = 21) -> pl.DataFrame:
    """Expected return over the horizon from a score (Grinold): ic x z x specific vol, where z is
    the score standardized per date (oriented by neural-processor's direction for a raw signal;
    composites are +1) and the specific vol comes from the risk model of that date. Returns
    date, security_id, alpha, sector, issuer_market_cap."""
    from neural_processor import SIGNALS

    sign = SIGNALS[col].direction if col in SIGNALS else 1
    p = panel.filter(pl.col(col).is_not_null() & pl.col(col).is_finite())
    p = p.with_columns(((pl.col(col) - pl.col(col).mean().over("date")) / pl.col(col).std().over("date") * sign)
                       .clip(-3, 3).alias("__z"))
    out = []
    for (d,), g in p.group_by(["date"], maintain_order=True):
        try:
            sv = risk.specific_var(d).select("security_id", "spec_var")
        except ValueError:
            continue
        out.append(g.join(sv, on="security_id").select(
            "date", "security_id", (ic * pl.col("__z") * (pl.col("spec_var") * horizon_days).sqrt()).alias("alpha"),
            *[c for c in ("sector", "issuer_market_cap") if c in g.columns]))
    return pl.concat(out).sort("date", "security_id") if out else pl.DataFrame(
        schema={"date": pl.Date, "security_id": pl.String, "alpha": pl.Float64})


def _psd_root(F: np.ndarray) -> np.ndarray:
    """L with F = L L' (eigenvalues clipped at 0)."""
    vals, vecs = np.linalg.eigh((F + F.T) / 2)
    return vecs * np.sqrt(np.clip(vals, 0, None))


class Optimizer:
    """One rebalance: maximize alpha - cost_aversion x cost - borrow - risk_aversion x variance,
    subject to Constraints. Costs are unimatrix-zero's CostModel: half-spread + commission per
    unit traded, square-root impact (with an AUM) and general-collateral borrow on shorts."""

    def __init__(self, risk: RiskModel, costs=None, constraints: Constraints | None = None):
        from unimatrix_zero import CostModel

        self.risk = risk
        self.costs = costs if costs is not None else CostModel()
        self.c = constraints or Constraints()

    # ------------------------------------------------------------------ inputs
    def _inputs(self, on: date, alpha: pl.DataFrame, market: pl.DataFrame | None, nav: float):
        m = self.risk._month(on)
        ex = self.risk.exposures(m)
        sv = self.risk.specific_var(m).select("security_id", "spec_var")
        ex = ex.join(sv, on="security_id", how="left").join(alpha.select("security_id", "alpha"),
                                                             on="security_id", how="left")
        if market is not None:
            ex = ex.join(market.select("security_id", "adv", "vol"), on="security_id", how="left")
        else:
            ex = ex.with_columns(pl.lit(None, pl.Float64).alias("adv"), pl.lit(None, pl.Float64).alias("vol"))
        # Sorted after the joins (joins keep no row order): solve finds holdings by searchsorted.
        ex = ex.filter(pl.col("spec_var").is_not_null()).sort("security_id")
        costs = self.costs
        lin = ex.with_columns(costs.spread_cost().alias("c"))["c"].to_numpy()       # with_columns broadcasts
        eta = (ex.with_columns(costs.impact_cost(pl.lit(1.0), nav).alias("e"))["e"].to_numpy()
               if costs.aum else np.zeros(ex.height))
        X = design(ex["sector"].to_numpy(), ex.select(list(STYLES)).to_numpy())
        F = self.risk._cov_matrix(m)
        return m, ex, X, F, lin, eta

    def _beta(self, ex: pl.DataFrame, X: np.ndarray, F: np.ndarray) -> np.ndarray:
        """Predicted beta of each name to the cap-weighted universe."""
        wm = ex["issuer_market_cap"].to_numpy()
        wm = wm / wm.sum()
        d = ex["spec_var"].to_numpy()
        xm = X.T @ wm
        cov = X @ (F @ xm) + d * wm
        return cov / (xm @ F @ xm + (d * wm ** 2).sum())

    # ------------------------------------------------------------------ solve
    def solve(self, alpha: pl.DataFrame, on: Any, w0: pl.DataFrame | None = None, market: pl.DataFrame | None = None,
              nav: float = 1.0) -> Solution:
        """alpha: security_id, alpha (expected return over the horizon; missing names get 0).
        w0: current holdings (security_id, weight); names outside the model's universe are sold.
        market: security_id, adv, vol for costs (needed with an AUM). Returns target weights and
        info: status, relaxed, alpha, vol (annual forecast), cost, turnover, names, seconds."""
        t0 = time.time()
        d = _day(on)
        if market is None and (self.costs.aum or self.costs.spread == "volume"):
            raise ValueError("market (security_id, adv, vol) is needed for volume-based spreads and impact")
        m, ex, X, F, lin, eta = self._inputs(d, alpha, market, nav)
        ids = ex["security_id"].to_numpy()
        n = len(ids)
        a = ex["alpha"].fill_null(0.0).to_numpy()
        h0 = np.zeros(n)
        sold = 0.0
        if w0 is not None and w0.height:
            w0 = w0.group_by("security_id").agg(pl.col("weight").sum())
            inside = w0.filter(pl.col("security_id").is_in(ids))
            h0[np.searchsorted(ids, inside["security_id"].to_numpy())] = inside["weight"].to_numpy()
            sold = float(w0.filter(~pl.col("security_id").is_in(ids))["weight"].abs().sum())
        no_adv = ex["adv"].is_null().to_numpy() if self.costs.aum else np.zeros(n, bool)
        beta = self._beta(ex, X, F)
        L = _psd_root(F)
        sd = np.sqrt(ex["spec_var"].to_numpy())
        hz = self.c.horizon_days
        borrow = self.costs.borrow_bps / 1e4 * hz / ANNUAL
        part = None
        if self.costs.aum and self.c.max_participation:
            part = self.c.max_participation * ex["adv"].fill_null(0).to_numpy() / (self.costs.aum * nav)

        steps = [("none", self.c, part)]
        if part is not None:
            steps.append(("participation", self.c, None))
        wide = replace(self.c, sector_net=self.c.sector_net and self.c.sector_net * 3, beta=self.c.beta and self.c.beta * 3,
                       styles=tuple((k, v * 3) for k, v in self.c.styles))
        steps.append(("exposures x3", wide, None))
        steps.append(("exposures off", replace(self.c, sector_net=None, beta=None, styles=()), None))
        for relaxed, c, pt in steps:
            # Variables in percent of capital (S = 100) and the impact cost in its own cone: in
            # plain fractions Clarabel reports tiny impact terms as only approximately solved.
            v = cp.Variable(n)
            y = cp.Variable(len(FACTORS))
            tv = v - S * h0
            z = cp.Variable(n)
            imp = cp.Variable(n)
            risk = cp.norm(cp.hstack([L.T @ y, cp.multiply(sd, v)])) * np.sqrt(hz)      # in percent
            obj = (a @ v - c.cost_aversion * lin @ z) / S - c.cost_aversion * (eta / S ** 1.5) @ imp
            cons = [y == X.T @ v, cp.abs(v) <= c.max_weight * S, z >= cp.abs(tv), cp.power(z, 1.5) <= imp]
            if c.long_short:
                obj -= borrow * cp.sum(cp.neg(v)) / S
                cons += [cp.sum(v) == 0, cp.norm1(v) <= c.gross * S]
            else:
                cons += [cp.sum(v) == S, v >= 0]
            if c.risk_aversion:
                var = (cp.sum_squares(L.T @ y) + cp.sum_squares(cp.multiply(sd, v))) * hz / S ** 2
                obj -= c.risk_aversion * var
            if c.target_vol:
                cons.append(risk <= c.target_vol * np.sqrt(hz / ANNUAL) * S)
            if c.sector_net is not None and c.long_short:
                cons.append(cp.abs(y[1:1 + len(SECTORS)]) <= c.sector_net * S)
            if c.beta is not None:
                cons.append(cp.abs(beta @ v - (0 if c.long_short else S)) <= c.beta * S)
            for st, cap in c.styles:
                cons.append(cp.abs(y[FACTORS.index(st)]) <= cap * S)
            if pt is not None:
                cons.append(cp.abs(tv) <= np.maximum(pt, np.abs(h0)) * S)   # selling out is always allowed
            if no_adv.any():                                         # no volume: cannot cost a trade, so sell
                cons.append(v[np.flatnonzero(no_adv)] == 0)
            prob = cp.Problem(cp.Maximize(SCALE * obj), cons)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    prob.solve(solver="CLARABEL", **SOLVER_OPTS)
                except cp.SolverError as exc:
                    log.debug("%s (%s): %s", d, relaxed, exc)
                    continue
            if prob.status in ("optimal", "optimal_inaccurate") and v.value is not None:
                wv = v.value / S
                wv = np.where(np.abs(wv) < TINY, 0.0, wv)
                dt = wv - h0
                info = {"date": d, "model_date": m, "status": prob.status, "relaxed": relaxed,
                        "alpha": float(a @ wv), "vol": float(risk.value) / S * np.sqrt(ANNUAL / hz),
                        "cost": float(lin @ np.abs(dt) + eta @ np.abs(dt) ** 1.5),
                        "turnover": float(np.abs(dt).sum() + sold), "sold_outside": sold,
                        "names": int((wv != 0).sum()), "gross": float(np.abs(wv).sum()),
                        "beta": float(beta @ wv), "seconds": round(time.time() - t0, 2)}
                out = pl.DataFrame({"security_id": ids, "weight": wv}).filter(pl.col("weight") != 0)
                return Solution(out, info)
        log.warning("%s: no feasible portfolio, holding the current one", d)
        keep = pl.DataFrame({"security_id": ids, "weight": h0}).filter(pl.col("weight") != 0)
        return Solution(keep, {"date": d, "model_date": m, "status": "infeasible", "relaxed": "held",
                               "seconds": round(time.time() - t0, 2)})


class Portfolio:
    """Monthly (or any) rebalancing with Optimizer: each solve starts from the previous targets
    drifted with daily returns to the execution date (signal date + lag trading days), as the
    unimatrix-zero engine holds them."""

    def __init__(self, optimizer: Optimizer, engine=None):
        self.opt = optimizer
        self._engine = engine

    @property
    def engine(self):
        if self._engine is None:
            self._engine = self.opt.risk.engine
        return self._engine

    def build(self, alpha: pl.DataFrame, start: Any = None, end: Any = None, lag: int = 1
              ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """alpha: date (signal date), security_id, alpha. Returns (weights: date, security_id,
        weight, sector, issuer_market_cap, for Engine.run with the same lag and CostModel;
        diagnostics: one row per date with Solution.info)."""
        from unimatrix_zero import Engine

        s, e = _day(start), _day(end)
        a = alpha
        if s:
            a = a.filter(pl.col("date") >= s)
        if e:
            a = a.filter(pl.col("date") <= e)
        dates = sorted(a["date"].unique().to_list())
        if not dates:
            raise ValueError("no alpha in the period")
        ex = self.opt.risk.exposures().filter(pl.col("date") >= self.opt.risk._month(dates[0]))
        ids = sorted(set(ex["security_id"].to_list()) | set(a["security_id"].to_list()))
        r = self.engine.returns(ids, dates[0] - timedelta(days=10), e)   # a non-trading first date
        cal = sorted(r["date"].unique().to_list())
        exec_map = Engine.execution_dates(dates, cal, lag)           # the engine's rule, same lag
        rets = r.select("security_id", "date", "ret").sort("date")
        aux = r.select("security_id", "date", "adv", "vol").sort("date")
        held = pl.DataFrame(schema={"security_id": pl.String, "weight": pl.Float64})
        nav, prev_exec = 1.0, None
        weights, diags = [], []
        for d in dates:
            ex_day = exec_map.get(d)
            if ex_day is None:
                break
            if prev_exec is not None and held.height:
                held, growth = self._drift(held, rets, prev_exec, ex_day)
                nav *= growth
            mk = aux.filter(pl.col("date") <= d).group_by("security_id").agg(pl.all().sort_by("date").last())
            sol = self.opt.solve(a.filter(pl.col("date") == d), d, held, mk, nav)
            sol.info["nav"] = nav
            diags.append(sol.info)
            weights.append(sol.weights.with_columns(pl.lit(d).alias("date")))
            held, prev_exec = sol.weights, ex_day
            log.info("%s %s: %d names, vol %.1f%%, turnover %.2f", d, sol.info.get("status"),
                     sol.info.get("names", 0), 100 * (sol.info.get("vol") or 0), sol.info.get("turnover") or 0)
        w = pl.concat(weights) if weights else pl.DataFrame()
        tags = self.opt.risk.exposures().select("date", "security_id", "sector", "issuer_market_cap")
        w = (w.sort("date").join_asof(tags.sort("date").rename({"date": "m"}), left_on="date", right_on="m",
                                      by="security_id", strategy="backward", check_sortedness=False)
             .select("date", "security_id", "weight", "sector", "issuer_market_cap"))
        return w, pl.DataFrame(diags, infer_schema_length=None)

    @staticmethod
    def _drift(held: pl.DataFrame, rets: pl.DataFrame, a: date, b: date) -> tuple[pl.DataFrame, float]:
        """Holdings after the returns of the days in (a, b]; a name with no return on b (it
        stopped trading) is cash. Returns (drifted weights as fractions of the new capital,
        capital growth)."""
        g = (rets.filter((pl.col("date") > a) & (pl.col("date") <= b))
             .group_by("security_id").agg((1 + pl.col("ret").fill_null(0)).product().alias("G"),
                                          pl.col("date").max().alias("last")))
        h = held.join(g, on="security_id", how="left").with_columns(pl.col("G").fill_null(1.0))
        pnl = float((h["weight"] * (h["G"] - 1)).sum())
        growth = 1 + pnl
        live = h.filter(pl.col("last") == b)
        return live.select("security_id", (pl.col("weight") * pl.col("G") / growth).alias("weight")), growth
