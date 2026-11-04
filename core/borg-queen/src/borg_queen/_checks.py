"""`queen check`: does the risk model forecast what happened?"""

from __future__ import annotations

import numpy as np
import polars as pl

from ._model import RiskModel, bias_stat
from ._styles import STYLES

FROM = "2012-12-31"           # leave two years for the covariance to settle
BIAS = (0.8, 1.2)             # whole-period bias statistic must lie in here
SPY_CORR = 0.95               # market factor against SPY, daily


def test_portfolios(rm: RiskModel, start: str = FROM, seed: int = 0) -> dict[str, pl.DataFrame]:
    """Monthly test portfolios on the model's universe: cap-weighted, random long-short books of
    50+50 and 2+2 names, and each style's top-minus-bottom quintile (equal weighted)."""
    ex = rm.exposures().filter(pl.col("date") >= pl.lit(start).str.to_date())
    out = {"cap-weighted universe": ex.select(
        "date", "security_id", (pl.col("issuer_market_cap") / pl.col("issuer_market_cap").sum().over("date")).alias("weight"))}
    rng = np.random.default_rng(seed)
    for k in (50, 2):
        rows = []
        for (d,), g in ex.group_by(["date"], maintain_order=True):
            pick = rng.choice(g["security_id"].to_numpy(), 2 * k, replace=False)
            rows.append(pl.DataFrame({"date": [d] * 2 * k, "security_id": pick, "weight": [1 / k] * k + [-1 / k] * k}))
        out[f"random {k}+{k} long-short"] = pl.concat(rows)
    for st in STYLES:
        q = ex.with_columns(pl.col(st).rank("ordinal").over("date").alias("rk"), pl.len().over("date").alias("n"))
        hi = q.filter(pl.col("rk") > pl.col("n") * 0.8)
        lo = q.filter(pl.col("rk") <= pl.col("n") * 0.2)
        out[f"{st} top minus bottom fifth"] = pl.concat([
            hi.select("date", "security_id", (1 / pl.len().over("date")).alias("weight")),
            lo.select("date", "security_id", (-1 / pl.len().over("date")).alias("weight"))])
    return out


def run_checks(rm: RiskModel, engine=None) -> list[tuple[str, bool, str]]:
    out = []
    fr = rm.factor_returns(FROM)
    if engine is not None:
        spy = engine.market(FROM).rename({"ret": "spy"})
        j = fr.join(spy, on="date")
        c = float(np.corrcoef(j["market"], j["spy"])[0, 1]) if j.height > 20 else float("nan")
        out.append((f"market factor tracks SPY (daily correlation >= {SPY_CORR})", c >= SPY_CORR, f"{c:.3f}"))
    r2 = float(fr["r2"].median())
    out.append(("median daily R2 between 0.15 and 0.6", 0.15 <= r2 <= 0.6, f"{r2:.3f}"))
    for name, w in test_portfolios(rm).items():
        b = rm.bias(w)
        s = bias_stat(b)
        roll = bias_stat(b, 12)
        inside = float(((roll["bias"] >= BIAS[0]) & (roll["bias"] <= BIAS[1])).mean())
        out.append((f"forecast risk {name}: bias in {BIAS[0]}-{BIAS[1]}", BIAS[0] <= s <= BIAS[1],
                    f"{s:.2f}; forecast {b['forecast_vol'].mean():.1%}, realized {b['realized_vol'].mean():.1%}; "
                    f"{inside:.0%} of 12-month windows inside"))
    return out
