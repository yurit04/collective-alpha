"""Per security and day: official prices and what a cost model may know before the open."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

ADV_WINDOW = 21      # days of dollar volume, as unimatrix-zero
VOL_WINDOW = 63      # days of returns for volatility, as unimatrix-zero
VOL_CLIP = 0.5
AUX_COLUMNS = ["date", "security_id", "ticker", "open", "close", "prev_close", "adv", "vol", "ret"]


def market_inputs(summaries: pl.DataFrame | pl.LazyFrame) -> pl.DataFrame:
    """nanoprobe daily summaries -> official open/close, prev_close (today's basis), the day's
    close-to-close total return `ret`, and adv / vol as of the previous day (21-day mean dollar
    volume, 63-day volatility of daily returns): known before the open, so usable for costs."""
    g = "security_id"
    return (
        summaries.lazy()
        .select("date", "security_id", "ticker", "open", "close", "prev_close", "volume")
        .sort(g, "date")
        .with_columns(ret=pl.col("close") / pl.col("prev_close") - 1, _dv=pl.col("close") * pl.col("volume"))
        .with_columns(adv=pl.col("_dv").rolling_mean(ADV_WINDOW, min_samples=1).shift(1).over(g),
                      vol=pl.col("ret").clip(-VOL_CLIP, VOL_CLIP).rolling_std(VOL_WINDOW, min_samples=20)
                      .shift(1).over(g))
        .select(AUX_COLUMNS)
        .collect()
    )


class AuxCache:
    """market_inputs for one calendar year at a time (the whole market, ~2.7M rows a year)."""

    def __init__(self, probe):
        self.probe = probe
        self._year: int | None = None
        self._days: dict[date, pl.DataFrame] = {}
        self._empty: pl.DataFrame | None = None

    def day(self, d: date) -> pl.DataFrame:
        if self._year != d.year:
            start = date(d.year, 1, 1) - timedelta(days=int(VOL_WINDOW * 1.6) + 10)
            raw = self.probe.summaries(start=start, end=date(d.year, 12, 31), lazy=True)
            frame = market_inputs(raw).filter(pl.col("date").dt.year() == d.year)
            self._days = {k[0]: g for k, g in frame.partition_by("date", as_dict=True).items()}
            self._empty = frame.clear()
            self._year = d.year
        return self._days.get(d, self._empty)
