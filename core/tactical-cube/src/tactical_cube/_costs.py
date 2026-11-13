"""Intraday trading costs on top of unimatrix-zero's CostModel."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import polars as pl
from unimatrix_zero import CostModel


@dataclass(frozen=True)
class IntradayCosts:
    """Costs per fill, as a fraction of traded value.

    base       unimatrix_zero.CostModel: the half-spread model (spread="volume" or a fixed bps),
               impact coefficient and borrow rates. Its commission_bps applies only without an
               AUM; its aum is ignored (use `aum` here).
    aum        capital in dollars. With it: square-root impact, the participation cap and
               per-share commissions; without it returns are per unit of capital and costless
               in size.
    tod        half-spread multipliers by the fill bar's start (minutes from the open): pairs
               (minutes, multiplier), the first pair whose minutes the bar starts before applies;
               1.0 after the last. The default is measured (adaptation-matrix, decision 0017):
               the effective half-spread of stocks over $10M a day relative to the day's
               dollar-weighted one, which CostModel's fitted spread predicts, on the last 60
               measured sessions: 3.7x in the first minute, 1.4x at 15-30 minutes, 0.6-0.8x in
               the last hour.
    auction_spread_bps  half-spread charged on opening and closing auction fills (one price
               for everyone: 0 by default; impact and commission still apply).
    commission_per_share, commission_min  IB fixed pricing (USD), with an AUM.
    """

    base: CostModel = field(default_factory=CostModel)
    aum: float | None = None
    tod: tuple[tuple[float, float], ...] = (
        (1, 3.71), (5, 2.58), (15, 1.75), (30, 1.39), (60, 1.2), (120, 1.02), (180, 0.92), (240, 0.87),
        (300, 0.82), (360, 0.78), (375, 0.7), (385, 0.72), (390, 0.6))
    auction_spread_bps: float = 0.0
    commission_per_share: float = 0.0035
    commission_min: float = 0.35

    def __post_init__(self):
        if self.base.spread == "estimate":
            raise ValueError("spread='estimate' needs daily high/low estimates; use 'volume' or a fixed bps")

    def tod_multiplier(self, start_minute: pl.Expr) -> pl.Expr:
        expr = pl.lit(1.0)
        for minutes, mult in reversed(self.tod):
            expr = pl.when(start_minute < minutes).then(pl.lit(float(mult))).otherwise(expr)
        return expr

    def apply(self, fills: pl.DataFrame, capital: float) -> pl.DataFrame:
        """Add spread_bps, impact_bps, commission_bps (per unit traded) and cost (fraction of
        capital) to fills with units, price, kind, start_minute, adv and vol columns.

        Impact and the commission minimum are per order, not per fill: the fills of one order
        (same date, security, kind, decision_time and side) pay the square-root impact of the
        whole order and share one commission, so splitting an order over bars does not make it
        cheaper."""
        if fills.is_empty():
            return fills.with_columns([pl.lit(None, pl.Float64).alias(c) for c in
                                       ("spread_bps", "impact_bps", "commission_bps", "cost")])
        no_comm = replace(self.base, commission_bps=0.0, aum=self.aum)
        dollars = (pl.col("units") * pl.col("price")).abs()
        order = [c for c in ("date", "security_id", "kind", "decision_time") if c in fills.columns] + ["_side"]
        spread = (pl.when(pl.col("kind") == "bar")
                  .then(no_comm.spread_cost(pl.col("date") if "date" in fills.columns else None)
                        * self.tod_multiplier(pl.col("start_minute")))
                  .otherwise(pl.lit(self.auction_spread_bps / 1e4)))
        impact = no_comm.impact_cost(pl.col("_order_dollars") / capital, 1.0)
        if self.aum:
            comm = pl.max_horizontal(pl.col("_order_units") * self.commission_per_share,
                                     pl.lit(self.commission_min)) / pl.col("_order_dollars")
        else:
            comm = pl.lit(self.base.commission_bps / 1e4)
        out = (fills.with_columns(_frac=dollars / capital, _side=pl.col("units").sign())
               .with_columns(_order_dollars=dollars.sum().over(order),
                             _order_units=pl.col("units").abs().sum().over(order))
               .with_columns(_sp=spread, _im=impact, _cm=comm)
               .with_columns(spread_bps=pl.col("_sp") * 1e4, impact_bps=pl.col("_im") * 1e4,
                             commission_bps=pl.col("_cm") * 1e4,
                             cost=pl.col("_frac") * (pl.col("_sp") + pl.col("_im") + pl.col("_cm")))
               .drop("_frac", "_side", "_order_dollars", "_order_units", "_sp", "_im", "_cm"))
        return out


NO_INTRADAY_COSTS = IntradayCosts(
    base=CostModel(commission_bps=0.0, spread=0.0, spread_floor_bps=0.0, borrow_bps=0.0, htb_bps=0.0),
    tod=(), commission_per_share=0.0, commission_min=0.0)
