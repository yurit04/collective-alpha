"""Strategies for the intraday studies: each is f(day, bars) -> targets for tactical_cube.Cube.run.

Every function reads only bars up to its decision time (tactical-cube re-runs them on cut-off
bars to check), so they can be passed to Cube.run as they are.
"""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

import polars as pl

__all__ = ["breakout", "config", "momentum", "overnight", "reversal", "stats_by_period"]

CONFIG = Path(__file__).resolve().parents[2] / "configs" / "trials.toml"
EMPTY = pl.DataFrame(schema={"bar_end": pl.Datetime("ns", "UTC"), "security_id": pl.String,
                             "weight": pl.Float64, "auction": pl.String})


def config(path: Path | str = CONFIG) -> dict:
    return tomllib.loads(Path(path).read_text())


def momentum(ticker: str = "SPY", enter_minutes_to_close: int = 30):
    """Intraday momentum (Gao, Han, Li and Zhou 2018): the return from the previous close to
    10:00 sets the side for the last half hour; enter at the bar ending `enter_minutes_to_close`
    before the close, exit in the closing auction (run with flat_by='close').
    Needs features gap and ret_open."""
    def strategy(day: date, bars: pl.DataFrame) -> pl.DataFrame:
        b = bars.filter(pl.col("ticker") == ticker)
        at10 = b.filter(pl.col("minutes_from_open") == 30)
        entry = b.filter(pl.col("minutes_to_close") == enter_minutes_to_close)
        if at10.is_empty() or entry.is_empty() or at10["gap"][0] is None or at10["ret_open"][0] is None:
            return EMPTY
        r = (1 + at10["gap"][0]) * (1 + at10["ret_open"][0]) - 1
        if r == 0:
            return EMPTY
        return entry.select("bar_end", "security_id", weight=pl.lit(1.0 if r > 0 else -1.0),
                            auction=pl.lit(None, pl.String))
    return strategy


def overnight(ticker: str = "SPY", hold: str = "overnight", decide_minutes_to_close: int = 15):
    """Hold only overnight (buy in the closing auction, sell in the next opening auction) or only
    during the day (buy at the open, sell at the close). Run with flat_by=None."""
    if hold not in ("overnight", "intraday"):
        raise ValueError("hold must be 'overnight' or 'intraday'")
    at_close, at_open = (1.0, 0.0) if hold == "overnight" else (0.0, 1.0)

    def strategy(day: date, bars: pl.DataFrame) -> pl.DataFrame:
        b = bars.filter((pl.col("ticker") == ticker) & (pl.col("minutes_to_close") == decide_minutes_to_close))
        if b.is_empty():
            return EMPTY
        return pl.concat([b.select("bar_end", "security_id", weight=pl.lit(at_close), auction=pl.lit("close")),
                          b.select("bar_end", "security_id", weight=pl.lit(at_open), auction=pl.lit("open"))])
    return strategy


def breakout(tickers: tuple[str, ...] = ("SPY", "QQQ"), or_minutes: int = 30):
    """Opening range breakout: after the first `or_minutes`, the first bar closing above the range
    goes long, below it short, 1/n of capital per name, held to the closing auction.
    Needs feature or_break (with the same or_minutes)."""
    def strategy(day: date, bars: pl.DataFrame) -> pl.DataFrame:
        b = bars.filter(pl.col("ticker").is_in(list(tickers)) & (pl.col("or_break").fill_null(0) != 0)
                        & ~pl.col("stale"))
        if b.is_empty():
            return EMPTY
        first = b.sort("bar").group_by("security_id").agg(pl.col("bar_end").first(), pl.col("or_break").first())
        w = 1.0 / len(tickers)
        rows = []
        for t in sorted(first["bar_end"].unique().to_list()):   # each new entry restates the whole book
            book = first.filter(pl.col("bar_end") <= t)
            rows.append(book.select(pl.lit(t).alias("bar_end"), "security_id",
                                    weight=pl.col("or_break").cast(pl.Float64) * w, auction=pl.lit(None, pl.String)))
        return pl.concat(rows).with_columns(pl.col("bar_end").cast(pl.Datetime("ns", "UTC")))
    return strategy


def reversal(at_minutes: int = 30, fraction: float = 0.1, min_names: int = 100,
             min_dollar_volume: float = 1e6):
    """Cross-sectional first-half-hour reversal: at `at_minutes` after the open, long the
    `fraction` of names that fell most since the open and short those that rose most, half the
    capital each side, held to the closing auction. Names that traded less than
    `min_dollar_volume` since the open are left out (ADRs that trade mostly at home, and any
    name a size universe ranks wrongly). Needs feature ret_open."""
    def strategy(day: date, bars: pl.DataFrame) -> pl.DataFrame:
        traded = (bars.filter(pl.col("minutes_from_open") <= at_minutes)
                  .group_by("security_id").agg(pl.col("dollar_volume").sum().alias("_traded")))
        at = (bars.filter((pl.col("minutes_from_open") == at_minutes) & pl.col("ret_open").is_not_null()
                          & ~pl.col("stale"))
              .join(traded, on="security_id").filter(pl.col("_traded") >= min_dollar_volume))
        if at.height < min_names:
            return EMPTY
        r = at.with_columns(q=pl.col("ret_open").rank("ordinal") / pl.len())
        lo, hi = r.filter(pl.col("q") <= fraction), r.filter(pl.col("q") > 1 - fraction)
        return pl.concat([lo.select("bar_end", "security_id", weight=pl.lit(0.5 / lo.height), auction=pl.lit(None, pl.String)),
                          hi.select("bar_end", "security_id", weight=pl.lit(-0.5 / hi.height), auction=pl.lit(None, pl.String))])
    return strategy


def stats_by_period(daily: pl.DataFrame, years: int = 5) -> pl.DataFrame:
    """Annualized gross and net return, net Sharpe and t-stat per block of `years` calendar years."""
    d = daily.with_columns(period=pl.col("date").dt.year() // years * years)
    out = []
    for (p,), g in sorted(d.group_by("period")):
        n = g.height
        sd = g["net"].std() or float("nan")
        out.append({"period": f"{p}-{p + years - 1}", "days": n,
                    "gross_ann": float(g["gross"].mean()) * 252, "net_ann": float(g["net"].mean()) * 252,
                    "net_sharpe": float(g["net"].mean()) / sd * 252 ** 0.5,
                    "net_t": float(g["net"].mean()) / sd * n ** 0.5})
    return pl.DataFrame(out)
