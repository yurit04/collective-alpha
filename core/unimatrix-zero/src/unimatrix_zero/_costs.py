"""Trading and holding costs, as fractions of traded or held value."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class CostModel:
    """Costs charged by the engine.

    Per trade (fraction of traded value): half-spread + commission + market impact.
      spread: "volume" (default) models the half-spread from 21-day mean dollar volume,
        spread_coef_bps x (ADV in $M) ^ -0.5: about 1 bp at $1B a day, 3 bp at $100M, 10 bp
        at $10M, in line with US quoted spreads since decimalization. "estimate" uses the
        Abdi-Ranaldo estimate from daily high/low/close (noisy: about half of large caps read
        0, the rest 10-40 bp). A number is a fixed half-spread in bps. All clipped to
        [spread_floor_bps, spread_cap_bps].
      aum: starting capital in dollars; with it, square-root impact applies:
        impact_coef x daily volatility x sqrt(trade dollars / 21-day mean dollar volume).
    Per day on shorts (fraction of short value / 252): borrow_bps a year, htb_bps when short
      interest is above htb_threshold of the float (hard to borrow).
    """

    commission_bps: float = 1.0
    spread: str | float = "volume"
    spread_coef_bps: float = 30.0
    spread_floor_bps: float = 0.5
    spread_cap_bps: float = 200.0
    aum: float | None = None
    impact_coef: float = 1.0
    borrow_bps: float = 25.0
    htb_bps: float = 300.0
    htb_threshold: float = 0.20

    def unit_cost(self, trade: pl.Expr, nav: float) -> pl.Expr:
        """Cost per unit traded for a row with half_spread, vol and adv columns; `trade` is the
        traded fraction of capital, `nav` the capital multiple (1.0 at the start)."""
        lo, hi = self.spread_floor_bps / 1e4, self.spread_cap_bps / 1e4
        if self.spread == "volume":
            adv_m = pl.when(pl.col("adv") > 0).then(pl.col("adv") / 1e6)
            hs = (self.spread_coef_bps / 1e4 * adv_m.pow(-0.5)).fill_null(hi).clip(lo, hi)
        elif self.spread == "estimate":
            hs = pl.col("half_spread").fill_null(hi).clip(lo, hi)
        else:
            hs = pl.lit(float(self.spread) / 1e4)
        cost = hs + self.commission_bps / 1e4
        if self.aum:
            dollars = trade.abs() * self.aum * nav
            impact = (self.impact_coef * pl.col("vol").fill_null(0.03)
                      * (dollars / pl.when(pl.col("adv") > 0).then(pl.col("adv")).otherwise(None)).sqrt())
            cost = cost + impact.fill_null(self.spread_cap_bps / 1e4)
        return cost

    def borrow_rate(self) -> pl.Expr:
        """Annual borrow rate for a row with si_pct_float (null: general collateral)."""
        htb = pl.col("si_pct_float").fill_null(0) > self.htb_threshold
        return pl.when(htb).then(self.htb_bps / 1e4).otherwise(self.borrow_bps / 1e4)


NO_COSTS = CostModel(commission_bps=0.0, spread=0.0, spread_floor_bps=0.0, borrow_bps=0.0, htb_bps=0.0)
