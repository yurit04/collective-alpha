"""Vectorised daily backtest of target weights against the panel.

Timing: a target set on signal date D is executed at the close `delay` sessions later (default 1),
so it first earns the return of session D + delay + 1. Between rebalances weights drift with
prices. A security whose last bar is reached is liquidated the next session at `delist_return`.

Costs: turnover x (commission + half-spread + slippage) in bps of traded notional, plus an annual
borrow rate on the short leg, plus an optional square-root impact term on ADV participation.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np
import polars as pl


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 0.5
    half_spread_bps: float = 3.0  # fallback when no per-name estimate is supplied
    slippage_bps: float = 2.0
    borrow_rate: float = 0.005  # per year, on short notional
    impact_coef: float = 0.0  # cost (fraction) = coef * sqrt(trade_notional / ADV); 0 disables

    @property
    def linear_bps(self) -> float:
        return self.commission_bps + self.half_spread_bps + self.slippage_bps

    @property
    def fixed_bps(self) -> float:
        """Everything except the spread, which may vary by name."""
        return self.commission_bps + self.slippage_bps


@dataclass(frozen=True)
class BacktestConfig:
    delay: int = 1
    delist_return: float = 0.0
    capital: float = 1_000_000.0  # only used for impact (trade notional vs ADV)
    costs: CostModel = field(default_factory=CostModel)


@dataclass
class BacktestResult:
    daily: pl.DataFrame  # date, ret_gross, ret_net, cost, borrow, turnover, gross, net, n_long, n_short
    config: BacktestConfig
    holdings: pl.DataFrame | None = None

    def summary(self, periods_per_year: int = 252) -> dict:
        d = self.daily
        r = d["ret_net"].to_numpy()
        g = d["ret_gross"].to_numpy()
        cum = np.cumprod(1 + r)
        dd = cum / np.maximum.accumulate(cum) - 1
        ann = periods_per_year
        mean, std = r.mean(), r.std(ddof=1)
        out = {
            "start": str(d["date"][0]),
            "end": str(d["date"][-1]),
            "days": d.height,
            "ann_return_net": round(mean * ann, 4),
            "ann_return_gross": round(g.mean() * ann, 4),
            "ann_vol": round(std * math.sqrt(ann), 4),
            "sharpe_net": round(mean / std * math.sqrt(ann), 3) if std > 0 else float("nan"),
            "max_drawdown": round(float(dd.min()), 4),
            "total_return_net": round(float(cum[-1] - 1), 4),
            "avg_daily_turnover": round(float(d["turnover"].mean()), 4),
            "ann_cost_drag": round(float((d["cost"] + d["borrow"]).mean() * ann), 4),
            "avg_gross": round(float(d["gross"].mean()), 3),
            "avg_net": round(float(d["net"].mean()), 3),
            "avg_n_long": round(float(d["n_long"].mean()), 1),
            "avg_n_short": round(float(d["n_short"].mean()), 1),
            "hit_rate": round(float((r > 0).mean()), 3),
        }
        out["calmar"] = (
            round(out["ann_return_net"] / abs(out["max_drawdown"]), 3) if out["max_drawdown"] < 0 else float("nan")
        )
        return out

    def by_year(self) -> pl.DataFrame:
        return (
            self.daily.group_by(pl.col("date").dt.year().alias("year"))
            .agg(
                ret_net=((1 + pl.col("ret_net")).product() - 1).round(4),
                vol=(pl.col("ret_net").std() * math.sqrt(252)).round(4),
                turnover=pl.col("turnover").mean().round(4),
                cost=((pl.col("cost") + pl.col("borrow")).sum()).round(4),
            )
            .sort("year")
        )

    def to_markdown(self) -> str:
        s = self.summary()
        lines = [
            f"Backtest {s['start']} .. {s['end']} ({s['days']} sessions)",
            f"net ann. return {s['ann_return_net']}  gross {s['ann_return_gross']}  vol {s['ann_vol']}  Sharpe {s['sharpe_net']}  max DD {s['max_drawdown']}  Calmar {s['calmar']}",
            f"turnover/day {s['avg_daily_turnover']}  cost drag/yr {s['ann_cost_drag']}  gross {s['avg_gross']}  net {s['avg_net']}  names L/S {s['avg_n_long']}/{s['avg_n_short']}",
            "by year: " + ", ".join(f"{r['year']}={r['ret_net']}" for r in self.by_year().to_dicts()),
        ]
        return "\n".join(lines)


def _wide(df: pl.DataFrame, value: str, dates: list[dt.date], ids: list[str]) -> np.ndarray:
    """(T x N) float matrix with NaN where absent."""
    date_idx = {d: i for i, d in enumerate(dates)}
    id_idx = {s: j for j, s in enumerate(ids)}
    out = np.full((len(dates), len(ids)), np.nan)
    di = np.fromiter((date_idx[d] for d in df["date"].to_list()), dtype=np.int64, count=df.height)
    si = np.fromiter((id_idx[s] for s in df["security_id"].to_list()), dtype=np.int64, count=df.height)
    out[di, si] = df[value].to_numpy()
    return out


def run_backtest(
    targets: pl.DataFrame,
    panel: pl.DataFrame,
    config: BacktestConfig | None = None,
    adv: pl.DataFrame | None = None,
    keep_holdings: bool = False,
    spreads: pl.DataFrame | None = None,
) -> BacktestResult:
    """targets: (security_id, date, weight) on rebalance dates (signal dates).
    panel: (security_id, date, ret, is_last_trade) for every session a security traded.
    adv: optional (security_id, date, adv) dollar ADV for the impact model.
    spreads: optional (security_id, date, spread_bps), the *round-trip* spread in basis points.
    Half of it is charged per trade; names without an estimate fall back to costs.half_spread_bps."""
    cfg = config or BacktestConfig()
    dates = sorted(set(panel["date"].to_list()))
    ids = sorted(set(panel["security_id"].to_list()) | set(targets["security_id"].to_list()))
    T, N = len(dates), len(ids)
    R = _wide(panel, "ret", dates, ids)  # NaN when no bar or on a security's first bar
    last = _wide(panel.filter(pl.col("is_last_trade")).with_columns(x=1.0), "x", dates, ids)  # 1 on last bar
    has_bar = ~np.isnan(_wide(panel.with_columns(x=1.0), "x", dates, ids))
    # a first bar has no return (no previous close): a position there earns 0 that day
    R0 = np.where(has_bar, np.nan_to_num(R, nan=0.0), np.nan)
    W_t = _wide(targets, "weight", dates, ids)  # NaN when no target that day
    target_days = ~np.all(np.isnan(W_t), axis=1)
    ADV = _wide(adv, "adv", dates, ids) if adv is not None else None
    if spreads is not None:
        HS = _wide(spreads, "spread_bps", dates, ids) / 2.0
        HS = np.where(np.isnan(HS), cfg.costs.half_spread_bps, HS)
    else:
        HS = None

    w = np.zeros(N)  # weights at start of day (before day's return)
    pending: dict[int, np.ndarray] = {}  # execution day -> target weights
    rows = []
    for i, d in enumerate(dates):
        if target_days[i]:
            exec_day = i + cfg.delay
            if exec_day < T:
                pending[exec_day] = np.nan_to_num(W_t[i], nan=0.0)
        # 1) earn today's return on positions held since yesterday's close
        r_day = np.where(has_bar[i], R0[i], 0.0)
        # positions whose security did not trade today but has traded before and is not dead keep weight (return 0)
        ret_gross = float(np.dot(w, r_day))
        # drift weights
        w = w * (1 + r_day) / (1 + ret_gross) if (1 + ret_gross) != 0 else w * 0
        # 2) delisting: the day after a security's last bar it is liquidated at delist_return
        if i > 0:
            dead = last[i - 1] == 1
            if dead.any():
                liq = w[dead]
                ret_gross += float(np.sum(liq * cfg.delist_return))
                w[dead] = 0.0
        # 3) rebalance at today's close if a pending target lands today
        turnover = 0.0
        cost = 0.0
        if i in pending:
            tgt = pending.pop(i)
            # cannot trade names without a bar today: keep current weight
            tgt = np.where(has_bar[i], tgt, w)
            trade = tgt - w
            turnover = float(np.abs(trade).sum())
            if HS is None:
                cost = turnover * cfg.costs.linear_bps / 1e4
            else:
                cost = float(np.sum(np.abs(trade) * (cfg.costs.fixed_bps + HS[i]))) / 1e4
            if ADV is not None and cfg.costs.impact_coef > 0:
                notional = np.abs(trade) * cfg.capital
                adv_i = ADV[i]
                ok = ~np.isnan(adv_i) & (adv_i > 0) & (notional > 0)
                part = np.zeros(N)
                part[ok] = notional[ok] / adv_i[ok]
                cost += float(np.sum(np.abs(trade[ok]) * cfg.costs.impact_coef * np.sqrt(part[ok])))
            w = tgt
        short = w[w < 0].sum()
        borrow = float(-short * cfg.costs.borrow_rate / 252)
        ret_net = ret_gross - cost - borrow
        rows.append(
            {
                "date": d,
                "ret_gross": ret_gross,
                "ret_net": ret_net,
                "cost": cost,
                "borrow": borrow,
                "turnover": turnover,
                "gross": float(np.abs(w).sum()),
                "net": float(w.sum()),
                "n_long": int((w > 0).sum()),
                "n_short": int((w < 0).sum()),
            }
        )
        if keep_holdings and i % 21 == 0:
            nz = np.nonzero(w)[0]
            rows[-1]["holdings"] = [(ids[j], float(w[j])) for j in nz]
    daily = pl.DataFrame([{k: v for k, v in r.items() if k != "holdings"} for r in rows])
    # the book only exists from the first execution; drop the idle prefix so averages are meaningful
    first = int(np.argmax(target_days)) + cfg.delay if target_days.any() else 0
    daily = daily.slice(first)
    holdings = None
    if keep_holdings:
        hs = [(r["date"], sid, wt) for r in rows if "holdings" in r for sid, wt in r["holdings"]]
        holdings = pl.DataFrame(hs, schema=["date", "security_id", "weight"], orient="row")
    return BacktestResult(daily=daily, config=cfg, holdings=holdings)
