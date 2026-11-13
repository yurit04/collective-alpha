"""Trading and holding costs, as fractions of traded or held value."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import polars as pl


@dataclass(frozen=True)
class SpreadFit:
    """Effective half-spread in bps, fitted on measured trades and NBBO quotes (adaptation-matrix,
    decision 0017): level(year) x (ADV / $100M) ^ adv_slope x product over knots of
    (max(ADV / knot, 1) / max($100M / knot, 1)) ^ change x (vol / 2%) ^ vol_slope, so a level is
    the half-spread at $100M ADV and 2% daily volatility. A Poisson fit, so it predicts the
    expected (mean) effective half-spread of a stock-day, not the median. Years before the first
    level take the first, years after the last take the last."""

    adv_slope: float
    vol_slope: float
    knots: tuple[tuple[float, float], ...]     # (ADV in dollars, slope change above it)
    levels: tuple[tuple[int, float], ...]      # (year, bps at $100M ADV and 2% daily volatility)

    def level(self, at: Any = None) -> pl.Expr:
        """The year's level (bps): `at` is None (the latest), a date or year, or a date expression."""
        years = dict(self.levels)
        first, last = min(years), max(years)
        if at is None:
            return pl.lit(years[last])
        if isinstance(at, pl.Expr):
            y = at.dt.year().clip(first, last)
            return y.replace_strict(years, default=years[last], return_dtype=pl.Float64)
        y = at.year if isinstance(at, (date, datetime)) else int(at)
        y = min(max(y, first), last)
        return pl.lit(years.get(y, years[last]))

    def bps(self, at: Any = None, adv: pl.Expr | None = None, vol: pl.Expr | None = None) -> pl.Expr:
        """Predicted half-spread (bps) for rows with `adv` (dollars) and `vol` (daily); a null
        volatility (fewer than 20 days of returns) counts as 3%, a null or zero ADV gives null."""
        adv = pl.col("adv") if adv is None else adv
        vol = pl.col("vol") if vol is None else vol
        a = pl.when(adv > 0).then(adv)
        eta = self.adv_slope * (a / 1e8).log() + self.vol_slope * (vol.fill_null(0.03).clip(lower_bound=1e-4) / 0.02).log()
        for knot, change in self.knots:
            eta = eta + change * ((a / knot).log().clip(lower_bound=0.0) - max(math.log(1e8 / knot), 0.0))
        return self.level(at) * eta.exp()


# Fitted by `adapt calibrate` on 424 measured days (2003-09 to 2026-11, 2.1M stock-days); held
# out on the last 6 months its dollar-weighted error is 5-15% in every ADV bucket, where the
# former volume rule undercharged names over $100M a day by 37-62%. See decision 0017.
SPREAD_FIT = SpreadFit(
    adv_slope=-0.3880, vol_slope=0.6244, knots=((1e6, -0.1122), (1e7, 0.1334)),
    levels=((2003, 3.5052), (2004, 3.2078), (2005, 3.1232), (2006, 3.1249), (2007, 2.7474), (2008, 3.474),
            (2009, 2.3179), (2010, 2.3981), (2011, 2.5651), (2012, 2.4597), (2013, 2.5653), (2014, 2.9181),
            (2015, 2.8649), (2016, 2.7549), (2017, 2.9604), (2018, 3.14), (2019, 2.8522), (2020, 3.2003),
            (2021, 3.3349), (2022, 3.2131), (2023, 2.9192), (2024, 3.074), (2025, 3.0682), (2026, 3.4011)))


@dataclass(frozen=True)
class CostModel:
    """Costs charged by the engine.

    Per trade (fraction of traded value): half-spread + commission + market impact.
      spread: "fitted" (default) is the effective half-spread measured from trades and quotes
        (SpreadFit, decision 0017) from 21-day dollar volume, 63-day volatility and the year:
        about 1.3 bp at $1B a day, 3.1 bp at $100M, 7.1 bp at $10M (2% volatility, 2025).
        "volume" is the former rule, spread_coef_bps x (ADV in $M) ^ -0.5 (1 bp at $1B, 3 bp at
        $100M, 10 bp at $10M: quoted spreads; it undercharges large names). "estimate" uses the
        Abdi-Ranaldo estimate from daily high/low/close (noisy: about half of large caps read
        0, the rest 10-40 bp). A number is a fixed half-spread in bps. All clipped to
        [spread_floor_bps, spread_cap_bps].
      auction: fills are market-on-close orders in the closing auction: no half-spread, because
        the official close is both the fill and the backtest's mark (measured: the close sits a
        median 0 bp from the pre-close midpoint in every size bucket). Commission and impact
        still apply. Off by default: without an AUM a closing-auction backtest pays only
        commission, however thin the auction (1-9% of daily volume under $10M a day).
      aum: starting capital in dollars; with it, square-root impact applies:
        impact_coef x daily volatility x sqrt(trade dollars / 21-day mean dollar volume).
    Per day on shorts (fraction of short value / 252): borrow_bps a year, htb_bps when short
      interest is above htb_threshold of the float (hard to borrow).
    warn_bps: a trade whose estimated cost per unit traded is above this is listed in
      Backtest.cost_warnings and reported once with a warning (None: off). Real trades in
      listed equities rarely cost 100 bp; one that does is usually a data problem (a wrong
      market cap or volume) or a position too large for the stock.
    """

    commission_bps: float = 1.0
    spread: str | float = "fitted"
    fit: SpreadFit = field(default=SPREAD_FIT, repr=False)
    auction: bool = False
    spread_coef_bps: float = 30.0
    spread_floor_bps: float = 0.5
    spread_cap_bps: float = 200.0
    aum: float | None = None
    impact_coef: float = 1.0
    borrow_bps: float = 25.0
    htb_bps: float = 300.0
    htb_threshold: float = 0.20
    warn_bps: float | None = 100.0

    def unit_cost(self, trade: pl.Expr, nav: float, at: Any = None) -> pl.Expr:
        """Cost per unit traded for a row with half_spread, vol and adv columns; `trade` is the
        traded fraction of capital, `nav` the capital multiple (1.0 at the start), `at` the
        trade date (see spread_cost)."""
        return self.spread_cost(at) + self.impact_cost(trade, nav)

    def impact_cost(self, trade: pl.Expr, nav: float) -> pl.Expr:
        """Square-root market impact per unit traded (0 without an AUM)."""
        if not self.aum:
            return pl.lit(0.0)
        dollars = trade.abs() * self.aum * nav
        impact = (self.impact_coef * pl.col("vol").fill_null(0.03)
                  * (dollars / pl.when(pl.col("adv") > 0).then(pl.col("adv")).otherwise(None)).sqrt())
        return impact.fill_null(self.spread_cap_bps / 1e4)

    def spread_cost(self, at: Any = None) -> pl.Expr:
        """Half-spread plus commission per unit traded. `at` (a date, year or date expression;
        None: the latest year) picks the fitted spread's year level."""
        lo, hi = self.spread_floor_bps / 1e4, self.spread_cap_bps / 1e4
        if self.auction:
            hs = pl.lit(0.0)
        elif self.spread == "fitted":
            hs = (self.fit.bps(at) / 1e4).fill_null(hi).clip(lo, hi)
        elif self.spread == "volume":
            adv_m = pl.when(pl.col("adv") > 0).then(pl.col("adv") / 1e6)
            hs = (self.spread_coef_bps / 1e4 * adv_m.pow(-0.5)).fill_null(hi).clip(lo, hi)
        elif self.spread == "estimate":
            hs = pl.col("half_spread").fill_null(hi).clip(lo, hi)
        else:
            hs = pl.lit(float(self.spread) / 1e4)
        return hs + self.commission_bps / 1e4

    def borrow_rate(self) -> pl.Expr:
        """Annual borrow rate for a row with si_pct_float (null: general collateral)."""
        htb = pl.col("si_pct_float").fill_null(0) > self.htb_threshold
        return pl.when(htb).then(self.htb_bps / 1e4).otherwise(self.borrow_bps / 1e4)


NO_COSTS = CostModel(commission_bps=0.0, spread=0.0, spread_floor_bps=0.0, borrow_bps=0.0, htb_bps=0.0)
