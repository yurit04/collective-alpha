"""Daily security panel: unadjusted bars keyed by security_id plus split/dividend-aware returns.

Per (security_id, date):
  open/high/low/close/volume/transactions   unadjusted, as traded
  split_ratio      new shares per old share effective this session (1.0 if none)
  dividend         cash per (pre-split) share going ex this session (0.0 if none)
  prev_close       previous session close for this security (may span a gap)
  gap_sessions     sessions since the previous bar (1 = consecutive)
  ret              total return:  (close * split_ratio + dividend) / prev_close - 1
  ret_px           price return:  (close * split_ratio) / prev_close - 1
  adj_factor       backward cumulative split factor; adj_close = close / adj_factor is comparable over time
  adj_close        split-adjusted close (today's share basis)
  tr_index         total-return index, 1.0 on the first bar of each security
  is_last_trade    last bar of the security in the store (delisting or data end)
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.config import Settings, get_settings
from collective_alpha.panel.corporate_actions import clean_dividends, clean_splits, map_events_to_security
from collective_alpha.storage import layout
from collective_alpha.storage.parquet import write_parquet_atomic
from collective_alpha.universe.builder import load_master
from collective_alpha.universe.security_master import map_to_security

log = logging.getLogger(__name__)

BAR_COLS = ["open", "high", "low", "close", "volume", "transactions"]


def security_bars(bars: pl.DataFrame, master: pl.DataFrame) -> pl.DataFrame:
    """One row per (security_id, date); concurrent when-issued lines resolved by max volume."""
    p = map_to_security(bars, master).filter(pl.col("security_id").is_not_null())
    return (
        p.sort(["security_id", "date", "volume"], descending=[False, False, True])
        .unique(subset=["security_id", "date"], keep="first", maintain_order=True)
        .select(["security_id", "date", "ticker", *BAR_COLS])
    )


def build_panel(
    bars: pl.DataFrame,
    splits: pl.DataFrame,
    dividends: pl.DataFrame,
    sessions: list[dt.date],
    max_div_yield: float = 0.5,
) -> pl.DataFrame:
    """bars: (security_id, date, ticker, open..transactions); splits: (security_id, date, split_ratio);
    dividends: (security_id, date, dividend). sessions: trading calendar covering bars."""
    idx = pl.DataFrame({"date": sessions}).with_row_index("sidx").with_columns(pl.col("sidx").cast(pl.Int64))
    sp = splits.group_by(["security_id", "date"]).agg(pl.col("split_ratio").product())
    dv = dividends.group_by(["security_id", "date"]).agg(pl.col("dividend").sum())
    df = (
        bars.join(idx, on="date", how="left")
        .join(sp, on=["security_id", "date"], how="left")
        .join(dv, on=["security_id", "date"], how="left")
        .with_columns(split_ratio=pl.col("split_ratio").fill_null(1.0), dividend=pl.col("dividend").fill_null(0.0))
        .sort(["security_id", "date"])
        .with_columns(
            prev_close=pl.col("close").shift(1).over("security_id"),
            gap_sessions=(pl.col("sidx") - pl.col("sidx").shift(1).over("security_id")),
            is_last_trade=pl.col("date") == pl.col("date").max().over("security_id"),
        )
    )
    # implausible dividends (data errors on thin names) are dropped, not applied
    bad_div = (pl.col("dividend") > max_div_yield * pl.col("prev_close")) & pl.col("prev_close").is_not_null()
    n_bad = df.filter(bad_div).height
    if n_bad:
        log.warning("dropping %d dividends larger than %.0f%% of the previous close", n_bad, 100 * max_div_yield)
    df = df.with_columns(dividend=pl.when(bad_div).then(0.0).otherwise(pl.col("dividend")))

    df = df.with_columns(
        ret=(pl.col("close") * pl.col("split_ratio") + pl.col("dividend")) / pl.col("prev_close") - 1,
        ret_px=(pl.col("close") * pl.col("split_ratio")) / pl.col("prev_close") - 1,
    ).with_columns(
        # backward cumulative split factor: product of ratios strictly after this row
        adj_factor=(
            pl.col("split_ratio").log().cum_sum(reverse=True).over("security_id") - pl.col("split_ratio").log()
        ).exp(),
        tr_index=(pl.col("ret").fill_null(0.0) + 1).log().cum_sum().over("security_id").exp(),
    )
    df = df.with_columns(adj_close=pl.col("close") / pl.col("adj_factor"))
    # A whole-day level shift by a large factor (open and close both far from the previous close, in
    # the same direction) with no split on record is almost always a split the vendor missed or a
    # price-basis change. Flag it; consumers decide whether to drop the return.
    open_f = pl.col("open") / pl.col("prev_close")
    close_f = pl.col("close") * pl.col("split_ratio") / pl.col("prev_close")
    df = df.with_columns(
        suspect_split=(
            pl.col("prev_close").is_not_null()
            & (pl.col("split_ratio") == 1.0)
            & (
                # both open and close shifted >= 5x in the same direction, or
                ((close_f >= 5) & (open_f >= 5))
                | ((close_f <= 0.2) & (open_f <= 0.2))
                # a 2.5x-5x shift where the intraday move is small relative to the shift
                | (
                    ((close_f >= 2.5) | (close_f <= 0.4))
                    & ((open_f >= 2.5) | (open_f <= 0.4))
                    & ((open_f / close_f).log().abs() < 0.35)
                )
            )
        ).fill_null(False)
    )
    return df.select(
        [
            "security_id",
            "date",
            "ticker",
            *BAR_COLS,
            "split_ratio",
            "dividend",
            "prev_close",
            "gap_sessions",
            "ret",
            "ret_px",
            "adj_factor",
            "adj_close",
            "tr_index",
            "is_last_trade",
            "suspect_split",
        ]
    ).sort(["date", "security_id"])


# ---------------------------------------------------------------- persistence


def panel_dir(settings: Settings) -> Path:
    return layout.curated_table_dir(settings, "panel")


def write_panel(settings: Settings, panel: pl.DataFrame) -> int:
    d = panel_dir(settings)
    for stale in d.glob("year=*/data.parquet"):
        stale.unlink()
    n = 0
    for (year,), part in panel.with_columns(year=pl.col("date").dt.year()).group_by("year"):
        n += write_parquet_atomic(part.drop("year").sort(["date", "security_id"]), d / f"year={year}" / "data.parquet")
    return n


def load_panel(
    settings: Settings | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    universe: str | None = None,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    """Panel rows, optionally restricted to a date range and to members of a universe on each date."""
    s = settings or get_settings()
    lf = pl.scan_parquet(str(panel_dir(s) / "year=*" / "data.parquet"), hive_partitioning=True).drop("year")
    if start:
        lf = lf.filter(pl.col("date") >= start)
    if end:
        lf = lf.filter(pl.col("date") <= end)
    if universe:
        from collective_alpha.universe.universes import load_universe

        u = load_universe(universe, s).select("security_id", "date", "rank").lazy()
        lf = lf.join(u, on=["security_id", "date"], how="inner")
    if columns:
        lf = lf.select(["security_id", "date", *[c for c in columns if c not in ("security_id", "date")]])
    return lf.collect()


def to_wide(panel: pl.DataFrame, value: str = "ret") -> pl.DataFrame:
    """date x security_id matrix of one column (polars; .to_pandas().set_index('date') for pandas)."""
    return panel.select("date", "security_id", value).pivot(on="security_id", index="date", values=value).sort("date")


# ---------------------------------------------------------------- orchestration


def build_and_write(settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    master = load_master(s)
    bars = (
        pl.scan_parquet(str(s.curated_dir / "day_aggs" / "**" / "*.parquet"), hive_partitioning=True)
        .select(["ticker", "date", *BAR_COLS])
        .collect()
    )
    sb = security_bars(bars, master)
    sessions = trading_days(sb["date"].min(), sb["date"].max())

    def latest(table: str) -> pl.DataFrame:
        parts = sorted(p for p in layout.curated_table_dir(s, table).glob("asof=*") if p.is_dir())
        return pl.read_parquet(parts[-1] / "data.parquet")

    sp = map_events_to_security(clean_splits(latest("splits")), master)
    dv = map_events_to_security(clean_dividends(latest("dividends")), master)
    panel = build_panel(sb, sp, dv, sessions)
    n = write_panel(s, panel)
    return {
        "rows": n,
        "securities": panel["security_id"].n_unique(),
        "dates": panel["date"].n_unique(),
        "splits_applied": int((panel["split_ratio"] != 1.0).sum()),
        "dividends_applied": int((panel["dividend"] > 0).sum()),
        "extreme_returns_gt_100pct": int((panel["ret"].abs() > 1.0).sum()),
        "suspect_splits": int(panel["suspect_split"].sum()),
    }


def check_panel(panel: pl.DataFrame) -> dict:
    """Sanity report for `ca panel check`."""
    r = panel.filter(pl.col("ret").is_not_null())
    return {
        "rows": panel.height,
        "ret_null": panel["ret"].null_count(),
        "ret_mean_bp": round(float(r["ret"].mean()) * 1e4, 2),
        "ret_median_bp": round(float(r["ret"].median()) * 1e4, 2),
        "abs_ret_gt_50pct": int((r["ret"].abs() > 0.5).sum()),
        "abs_ret_gt_100pct": int((r["ret"].abs() > 1.0).sum()),
        "gap_gt_1_session": int((panel["gap_sessions"] > 1).sum()),
        "zero_volume_rows": int((panel["volume"] <= 0).sum()),
        "nonpositive_close": int((panel["close"] <= 0).sum()),
        "suspect_splits": int(panel["suspect_split"].sum()),
    }


def extreme_returns(panel: pl.DataFrame, threshold: float = 1.0, n: int = 20) -> pl.DataFrame:
    return (
        panel.filter(pl.col("ret").abs() > threshold)
        .select(
            "date",
            "ticker",
            "security_id",
            "prev_close",
            "open",
            "close",
            "split_ratio",
            "dividend",
            "ret",
            "suspect_split",
        )
        .sort("ret", descending=True)
        .head(n)
    )
