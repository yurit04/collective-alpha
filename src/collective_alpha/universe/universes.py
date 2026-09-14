"""Point-in-time universe construction.

Membership on rebalance date D uses only data through the previous session (D-1):
lagged liquidity/price stats, and reference attributes from the latest snapshot on or before D.
Between rebalances membership is frozen except for delistings.
"""

from __future__ import annotations

import datetime as dt
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from collective_alpha.calendar import trading_days
from collective_alpha.config import REPO_ROOT, Settings
from collective_alpha.storage import layout
from collective_alpha.storage.parquet import write_parquet_atomic
from collective_alpha.universe.attributes import attributes_asof
from collective_alpha.universe.marketcap import shares_asof

log = logging.getLogger(__name__)

UNIVERSES_FILE = REPO_ROOT / "config" / "universes.toml"


@dataclass(frozen=True)
class UniverseSpec:
    name: str
    types: tuple[str, ...] = ("CS", "ADRC")
    exchanges: tuple[str, ...] = ("XNYS", "XNAS", "XASE")
    min_price: float = 5.0  # last close (D-1)
    min_adv: float = 1_000_000.0  # average daily dollar volume over adv_window sessions
    adv_window: int = 20
    min_history_days: int = 60  # sessions with a bar before D
    top_n: int | None = None  # entry rank threshold by ADV (None = no rank cap)
    exit_n: int | None = None  # stay while rank <= exit_n (defaults to 1.3 * top_n)
    one_class_per_issuer: bool = False  # keep the most liquid share class per CIK
    rebalance: str = "monthly"  # monthly | weekly
    rank_by: str = "adv"  # adv | cap  (cap needs SEC facts; securities without a share count are excluded)
    min_cap: float | None = None  # market cap floor in USD, evaluated with lagged close x latest filed shares

    @property
    def exit_rank(self) -> int | None:
        if self.top_n is None:
            return None
        return self.exit_n or int(self.top_n * 1.3)


def load_specs(path: Path = UNIVERSES_FILE) -> dict[str, UniverseSpec]:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    out = {}
    for name, cfg in raw.get("universe", {}).items():
        cfg = dict(cfg)
        for k in ("types", "exchanges"):
            if k in cfg:
                cfg[k] = tuple(cfg[k])
        out[name] = UniverseSpec(name=name, **cfg)
    return out


# ---------------------------------------------------------------- stats


def lagged_stats(panel: pl.DataFrame, adv_window: int) -> pl.DataFrame:
    """Per (security_id, date): stats known at the close of the *previous* session.
    panel columns: security_id, date, close, volume (one row per session with a bar)."""
    return (
        panel.sort(["security_id", "date"])
        .with_columns(dv=pl.col("close") * pl.col("volume"))
        .with_columns(
            adv=pl.col("dv").rolling_mean(adv_window, min_samples=adv_window).over("security_id"),
            hist=pl.int_range(1, pl.len() + 1).over("security_id"),
        )
        .with_columns(
            # shift by one session so that on date D we only see D-1
            lag_close=pl.col("close").shift(1).over("security_id"),
            lag_adv=pl.col("adv").shift(1).over("security_id"),
            lag_hist=pl.col("hist").shift(1).over("security_id"),
            lag_date=pl.col("date").shift(1).over("security_id"),
        )
        .select(["security_id", "date", "lag_date", "lag_close", "lag_adv", "lag_hist"])
    )


def rebalance_dates(sessions: list[dt.date], freq: str) -> list[dt.date]:
    firsts: dict[tuple, dt.date] = {}
    for d in sessions:
        key = (d.year, d.month) if freq == "monthly" else d.isocalendar()[:2]
        firsts.setdefault(key, d)
    return sorted(firsts.values())


# ---------------------------------------------------------------- build


def build_universe(
    spec: UniverseSpec,
    panel: pl.DataFrame,
    attrs: pl.DataFrame,
    last_trade: pl.DataFrame,
    sessions: list[dt.date],
    max_stale_sessions: int = 5,
    shares: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Daily membership table for one universe.

    panel: security_id, date, close, volume
    attrs: security_id, asof, type, primary_exchange, cik (point-in-time snapshots)
    last_trade: security_id, last_trade (from `securities`)
    sessions: all trading days covered by panel
    shares: optional (cik, filed, shares, shares_source, shares_end) series from marketcap.shares_series
    Returns rows (date, security_id, rebalance_date, rank, lag_adv, lag_close, cap, is_new).
    """
    if spec.rank_by == "cap" and shares is None:
        raise ValueError(f"universe {spec.name} ranks by cap but no shares series was provided")
    stats = lagged_stats(panel, spec.adv_window)
    # stats are indexed by sessions where the security has a bar; a rebalance date D needs the row
    # for the latest session <= D. Use an asof join per rebalance date.
    rdates = [d for d in rebalance_dates(sessions, spec.rebalance) if d >= sessions[0]]
    session_idx = {d: i for i, d in enumerate(sessions)}
    members: set[str] = set()
    out_periods: list[pl.DataFrame] = []
    stats_sorted = stats.sort(["security_id", "date"])
    sec_ids = stats_sorted["security_id"].unique()

    for i, rd in enumerate(rdates):
        period_end = rdates[i + 1] - dt.timedelta(days=1) if i + 1 < len(rdates) else sessions[-1]
        keys = pl.DataFrame({"security_id": sec_ids, "date": [rd] * len(sec_ids)})
        # latest stats row on or before rd (strictly, the row *for* the session <= rd; its lag_* fields are D-1)
        st = keys.sort(["security_id", "date"]).join_asof(
            stats_sorted, on="date", by="security_id", strategy="backward", check_sortedness=False
        )
        # a stale row means the security has not traded recently -> not eligible
        st = st.with_columns(
            stale=(
                pl.lit(session_idx.get(rd, 0))
                - pl.col("date").map_elements(lambda d: session_idx.get(d, -(10**6)), return_dtype=pl.Int64)
            )
        )
        st = st.filter(pl.col("stale") <= max_stale_sessions)
        st = attributes_asof(attrs, st.rename({"date": "stat_date"}).with_columns(date=pl.lit(rd)), "date")
        if shares is not None and "cik" in st.columns:
            st = shares_asof(shares, st, "date").with_columns(cap=pl.col("shares") * pl.col("lag_close"))
        else:
            st = st.with_columns(cap=pl.lit(None, dtype=pl.Float64))
        elig = st.filter(
            pl.col("type").is_in(list(spec.types))
            & pl.col("primary_exchange").is_in(list(spec.exchanges))
            & (pl.col("lag_close") >= spec.min_price)
            & (pl.col("lag_adv") >= spec.min_adv)
            & (pl.col("lag_hist") >= spec.min_history_days)
        )
        if spec.min_cap is not None:
            elig = elig.filter(pl.col("cap") >= spec.min_cap)
        if spec.rank_by == "cap":
            elig = elig.filter(pl.col("cap").is_not_null())
        rank_col = "cap" if spec.rank_by == "cap" else "lag_adv"
        if spec.one_class_per_issuer:
            elig = (
                elig.sort(["cik", "lag_adv"], descending=[False, True])
                .with_columns(cls_rank=pl.int_range(1, pl.len() + 1).over("cik"))
                .filter(pl.col("cik").is_null() | (pl.col("cls_rank") == 1))
                .drop("cls_rank")
            )
        elig = elig.sort(rank_col, descending=True).with_columns(rank=pl.int_range(1, pl.len() + 1))
        if spec.top_n is not None:
            stay = elig.filter(pl.col("security_id").is_in(list(members)) & (pl.col("rank") <= spec.exit_rank))
            enter = elig.filter(~pl.col("security_id").is_in(list(members)) & (pl.col("rank") <= spec.top_n))
            chosen = pl.concat([stay, enter])
        else:
            chosen = elig
        new_members = set(chosen["security_id"].to_list())
        is_new = chosen.with_columns(is_new=~pl.col("security_id").is_in(list(members)))
        members = new_members

        # expand to daily rows until period_end, cut at each security's last trade (delisting)
        days = [d for d in sessions if rd <= d <= period_end]
        daily = is_new.select("security_id", "rank", "lag_adv", "lag_close", "cap", "is_new").join(
            pl.DataFrame({"date": days}), how="cross"
        )
        daily = daily.join(last_trade, on="security_id", how="left").filter(
            pl.col("last_trade").is_null() | (pl.col("date") <= pl.col("last_trade"))
        )
        out_periods.append(daily.with_columns(rebalance_date=pl.lit(rd)).drop("last_trade"))

    out = pl.concat(out_periods) if out_periods else pl.DataFrame()
    return out.sort(["date", "rank"]) if out.height else out


def universe_stats(univ: pl.DataFrame) -> pl.DataFrame:
    """Members per rebalance and turnover (entries / members)."""
    return (
        univ.filter(pl.col("date") == pl.col("rebalance_date"))
        .group_by("rebalance_date")
        .agg(members=pl.len(), entries=pl.col("is_new").sum())
        .with_columns(turnover=pl.col("entries") / pl.col("members"))
        .sort("rebalance_date")
    )


# ---------------------------------------------------------------- persistence


def universe_dir(settings: Settings, name: str) -> Path:
    return layout.curated_table_dir(settings, "universes") / f"name={name}"


def write_universe(settings: Settings, name: str, univ: pl.DataFrame) -> int:
    d = universe_dir(settings, name)
    for stale in d.glob("year=*/data.parquet"):  # a rebuild replaces the whole universe
        stale.unlink()
        if not any(stale.parent.iterdir()):
            stale.parent.rmdir()
    n = 0
    for (year,), part in univ.with_columns(year=pl.col("date").dt.year()).group_by("year"):
        n += write_parquet_atomic(part.drop("year"), d / f"year={year}" / "data.parquet")
    return n


def load_universe(settings: Settings, name: str) -> pl.DataFrame:
    return pl.read_parquet(str(universe_dir(settings, name) / "year=*" / "data.parquet"), hive_partitioning=True).drop(
        "year"
    )


def security_panel(bars: pl.DataFrame, master: pl.DataFrame) -> pl.DataFrame:
    """Daily bars keyed by security_id, one row per (security_id, date). When a security has more
    than one row on a day (two tickers trading concurrently, or a vendor duplicate) keep the row with
    the largest volume; never sum, so an exact duplicate cannot double a day's volume."""
    from collective_alpha.universe.security_master import map_to_security

    panel = map_to_security(bars, master).filter(pl.col("security_id").is_not_null())
    return (
        panel.sort(["security_id", "date", "volume"], descending=[False, False, True])
        .unique(subset=["security_id", "date"], keep="first", maintain_order=True)
        .select("security_id", "date", "ticker", "close", "volume")
    )


def build_and_write(
    settings: Settings, spec: UniverseSpec, master: pl.DataFrame, attrs: pl.DataFrame, securities: pl.DataFrame
) -> dict:
    bars = pl.scan_parquet(str(settings.curated_dir / "day_aggs" / "**" / "*.parquet"), hive_partitioning=True)
    panel = security_panel(bars.select("ticker", "date", "close", "volume").collect(), master)
    sessions = trading_days(panel["date"].min(), panel["date"].max())
    last_trade = securities.select("security_id", "last_trade")
    shares = None
    try:
        from collective_alpha.universe.marketcap import load_sec_facts, load_ticker_details, shares_series

        shares = shares_series(load_sec_facts(settings), load_ticker_details(settings))
    except RuntimeError:
        if spec.rank_by == "cap" or spec.min_cap is not None:
            raise
    univ = build_universe(spec, panel, attrs, last_trade, sessions, shares=shares)
    n = write_universe(settings, spec.name, univ)
    st = universe_stats(univ)
    return {
        "rows": n,
        "rebalances": st.height,
        "members_first": int(st["members"][0]) if st.height else 0,
        "members_last": int(st["members"][-1]) if st.height else 0,
        "mean_turnover": round(float(st["turnover"].mean()), 4) if st.height else None,
        "securities_ever": univ["security_id"].n_unique() if univ.height else 0,
    }
