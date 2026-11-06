"""Catch strategies that read bars after their decision time."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl


class LookaheadError(Exception):
    """A strategy's targets at a bar changed when later bars were removed."""


def _norm(t: pl.DataFrame) -> pl.DataFrame:
    if "auction" not in t.columns:
        t = t.with_columns(auction=pl.lit(None, pl.String))
    return (t.select(pl.col("bar_end").cast(pl.Datetime("ns", "UTC")), pl.col("security_id").cast(pl.String),
                     pl.col("weight").cast(pl.Float64), pl.col("auction").cast(pl.String))
            .filter(pl.col("weight").is_not_null())
            .sort("bar_end", "security_id", "auction", nulls_last=True))


def _same(a: pl.DataFrame, b: pl.DataFrame, tol: float = 1e-9) -> bool:
    keys = [c for c in a.columns if c != "weight"]
    if a.select(keys).equals(b.select(keys)):
        d = (a["weight"] - b["weight"]).abs()
        return bool((d <= tol * (1 + a["weight"].abs())).all())
    return False


def check_lookahead(strategy: Callable[[date, pl.DataFrame], pl.DataFrame], day: date, bars: pl.DataFrame,
                    cuts: tuple[float, ...] = (0.25, 0.5, 0.75), targets: pl.DataFrame | None = None) -> None:
    """Run `strategy` on the day's bars cut after a few bars (fractions of the session) and check
    that every target decided up to the cut is the same as with the whole day. Raises
    LookaheadError naming the first difference. A strategy may use the clock columns
    (minutes_to_close, last) of bars it has seen; it must not use rows after its decision."""
    full = targets if targets is not None else strategy(day, bars)
    full = _norm(full) if full is not None and full.height else None
    n_bars = int(bars["bar"].max()) + 1 if bars.height else 0
    for f in cuts:
        cut = int(n_bars * f)
        if cut <= 0 or cut >= n_bars:
            continue
        seen = bars.filter(pl.col("bar") < cut)
        end = seen["bar_end"].max()
        part = strategy(day, seen)
        part = _norm(part).filter(pl.col("bar_end") <= end) if part is not None and part.height else None
        want = full.filter(pl.col("bar_end") <= end) if full is not None else None
        a = want if want is not None and want.height else None
        b = part if part is not None and part.height else None
        if a is None and b is None:
            continue
        if a is None or b is None or a.height != b.height or not _same(a, b):
            raise LookaheadError(
                f"{day}: targets up to {end} change when the bars after it are removed "
                f"({0 if a is None else a.height} rows with the whole day, {0 if b is None else b.height} without). "
                "The strategy reads bars after its decision time (e.g. a whole-day max, the last close, "
                "or a feature shifted backwards).")
