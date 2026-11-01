"""`vinculum build`: CIK map -> share counts per security -> basis ratios -> daily market cap.

Everything is derived from the Massive and EDGAR stores (no downloads), so a full rebuild is
cheap enough to run nightly after both data syncs.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from ._cikmap import EQUITY_TYPES, GOOD, build_cik_map, cik_on
from ._layout import Layout, write_atomic
from ._names import looks_equity
from ._shares import ADR_TYPES, adjust, basis_ratios, split_events

log = logging.getLogger("vinculum")
STALE_DAYS = 400          # an SEC count older than this (period end) is not used
DV_WINDOW = 60            # trading days of dollar volume used to pick an issuer's primary class
BASIS_OVERRIDES = Path(__file__).with_name("basis_overrides.csv")

PANEL_SCHEMA = {
    "date": pl.Date, "security_id": pl.String, "ticker": pl.String, "type": pl.String, "cik": pl.String,
    "cik_status": pl.String, "close": pl.Float64, "volume": pl.Float64, "dollar_volume": pl.Float64,
    "split_factor": pl.Float64, "shares": pl.Float64, "shares_end": pl.Date, "shares_filed": pl.Date,
    "shares_tag": pl.String, "shares_jump": pl.Boolean, "basis_ratio": pl.Float64, "basis": pl.String,
    "market_cap": pl.Float64, "issuer_market_cap": pl.Float64, "is_primary": pl.Boolean,
    "implausible": pl.Boolean,
}
PLAUSIBLE = (10 ** -5.5, 3.0)   # median daily turnover outside this: market cap withheld


WHEN_ISSUED = r"(w|\.WD|\.WI|WI)$"


def equity_securities(securities: pl.DataFrame) -> pl.DataFrame:
    """Common stock and ADRs (and untyped old securities whose name looks like equity). Baby
    bonds, warrants and units typed CS are left out by name, when-issued listings (HONw,
    DHR.WD) by ticker."""
    ok = securities["name"].map_elements(looks_equity, return_dtype=pl.Boolean, skip_nulls=False)
    wi = pl.col("tickers").list.eval(pl.element().str.contains(WHEN_ISSUED)).list.all()
    return (securities.with_columns(ok.fill_null(True).alias("__eq"))
            .filter((pl.col("type").is_in(EQUITY_TYPES) | pl.col("type").is_null()) & pl.col("__eq") & ~wi)
            .select("security_id", "type", "name"))


def load_basis_overrides(path: Path = BASIS_OVERRIDES) -> pl.DataFrame:
    schema = {"security_id": pl.String, "basis_ratio": pl.Float64, "note": pl.String}
    if not path.exists():
        return pl.DataFrame(schema=schema)
    df = pl.read_csv(path, comment_prefix="#", schema_overrides=schema)
    return df.select(list(schema)) if df.height else pl.DataFrame(schema=schema)


def calibrate(store, counts: pl.DataFrame, cik_map: pl.DataFrame, equity: pl.DataFrame) -> pl.DataFrame:
    """Basis ratio per equity security from Massive's latest ticker-details snapshot."""
    try:
        det = store.ticker_details()
    except FileNotFoundError:
        log.warning("no Massive ticker_details yet: ADRs get no market cap, stocks assume ratio 1 "
                    "(run: subspace backfill rest ticker_details)")
        det = pl.DataFrame()
    latest = equity.select("security_id", "type")
    if det.height:
        day = det["as_of"].max()
        det = det.filter(pl.col("as_of") == day)
        trade_day = store.trading_days(day - timedelta(days=10), day)[-1]
        trade_day = trade_day if isinstance(trade_day, date) else trade_day.date()
        ids = store.resolve(det["ticker"].to_list(), on=trade_day).select("ticker", "security_id")
        det = (det.join(ids, on="ticker").filter(pl.col("security_id").is_not_null())
               .with_columns(pl.coalesce("weighted_shares_outstanding", "share_class_shares_outstanding")
                             .alias("massive_shares"))
               .unique("security_id").select("security_id", "massive_shares", pl.col("market_cap").alias("massive_market_cap")))
        cur = cik_on(det.select("security_id").with_columns(pl.lit(trade_day).alias("date")), cik_map)
        fresh = (counts.filter((pl.col("available_from") <= trade_day)
                               & (pl.col("end") >= trade_day - timedelta(days=STALE_DAYS)))
                 .sort("end").group_by("security_id", "cik").agg(pl.col("shares_now").last()))
        cur = cur.join(fresh, on=["security_id", "cik"], how="left")
        classes = (cur.filter(pl.col("cik").is_not_null()).group_by("cik")
                   .agg(pl.col("security_id").n_unique().alias("classes")))
        cur = cur.join(classes, on="cik", how="left").with_columns(pl.col("classes").fill_null(1))
        latest = latest.join(det, on="security_id", how="left").join(
            cur.select("security_id", "shares_now", "classes"), on="security_id", how="left")
    else:
        latest = latest.with_columns(pl.lit(None, pl.Float64).alias("massive_shares"),
                                     pl.lit(None, pl.Float64).alias("massive_market_cap"),
                                     pl.lit(None, pl.Float64).alias("shares_now"), pl.lit(1).alias("classes"))
    latest = latest.with_columns(pl.col("classes").fill_null(1))
    b = basis_ratios(latest.select("security_id", "type", "massive_shares", "shares_now", "classes"))
    out = latest.select("security_id", "type", "massive_shares", "massive_market_cap",
                        pl.col("shares_now").alias("sec_shares")).join(b, on="security_id")
    ov = load_basis_overrides()
    if ov.height:
        out = (out.join(ov.select("security_id", pl.col("basis_ratio").alias("__ov")), on="security_id", how="left")
               .with_columns(pl.coalesce("__ov", "basis_ratio").alias("basis_ratio"),
                             pl.when(pl.col("__ov").is_not_null()).then(pl.lit("override"))
                             .otherwise(pl.col("basis")).alias("basis")).drop("__ov"))
    return out


def multi_class(pairs: pl.DataFrame, securities: pl.DataFrame) -> list[str]:
    """Securities sharing their CIK with another equity security that traded at the same time."""
    p = pairs.join(securities.select("security_id", "first_trade", "last_trade"), on="security_id")
    j = p.join(p, on="cik", suffix="_o").filter(
        (pl.col("security_id") != pl.col("security_id_o"))
        & (pl.col("first_trade") <= pl.col("last_trade_o")) & (pl.col("first_trade_o") <= pl.col("last_trade")))
    return j["security_id"].unique().to_list()


def liquidity_table(store, first: int, last: int, equity: pl.DataFrame) -> pl.DataFrame:
    """Median daily dollar volume and split-adjusted close per equity security and year."""
    lf = store.bars(start=date(first, 1, 1), end=date(last, 12, 31), adjust="splits", lazy=True)
    return (lf.join(equity.lazy().select("security_id"), on="security_id")
            .group_by("security_id", pl.col("date").dt.year().alias("year"))
            .agg((pl.col("close") * pl.col("volume")).median().alias("dv_med"), pl.col("close").median().alias("close_med"),
                 pl.len().alias("days"))
            .filter(pl.col("days") >= 20).drop("days")
            .collect())


def year_panel(store, year: int, equity: pl.DataFrame, cik_map: pl.DataFrame, counts: pl.DataFrame,
               basis: pl.DataFrame) -> pl.DataFrame:
    """One row per equity security and trading day of `year`."""
    start, end = date(year, 1, 1), date(year, 12, 31)
    lf = store.bars(start=start - timedelta(days=130), end=end, adjust="splits", lazy=True)
    df = (lf.select("date", "ticker", "security_id", "close", "volume", "split_factor")
          .join(equity.lazy().select("security_id", "type"), on="security_id").collect())
    if df.is_empty():
        return pl.DataFrame(schema=PANEL_SCHEMA)
    df = cik_on(df, cik_map)
    sc = (counts.select("security_id", "cik", "available_from", "end", "filed", "shares_now", "tag", "jump")
          .sort("available_from"))
    df = df.sort("date").join_asof(sc, left_on="date", right_on="available_from", by=["security_id", "cik"],
                                   strategy="backward", check_sortedness=False)
    fresh = pl.col("end") >= pl.col("date") - pl.duration(days=STALE_DAYS)
    df = df.with_columns(*[pl.when(fresh).then(pl.col(c)).otherwise(None).alias(c)
                           for c in ("shares_now", "end", "filed", "tag", "jump")])
    df = df.join(basis.select("security_id", "basis_ratio", "basis"), on="security_id", how="left")
    df = df.with_columns(
        (pl.col("close") * pl.col("shares_now") * pl.col("basis_ratio")).alias("market_cap"),
        (pl.col("shares_now") * pl.col("split_factor") * pl.col("basis_ratio")).alias("shares"),
        (pl.col("close") / pl.col("split_factor")).alias("raw_close"),
        (pl.col("volume") * pl.col("split_factor")).alias("raw_volume"),
        (pl.col("close") * pl.col("volume")).alias("dollar_volume"))
    # Safety net: while one SEC count is in use, an impossible median turnover means the count
    # (or ratio) is wrong: no market cap for those days.
    seg = ["security_id", "end", "filed"]
    to = (df.filter(pl.col("market_cap").is_not_null())
          .group_by(seg).agg((pl.col("dollar_volume") / pl.col("market_cap")).median().alias("__to")))
    df = (df.join(to, on=seg, how="left", nulls_equal=False)
          .with_columns((~pl.col("__to").is_between(*PLAUSIBLE)).fill_null(False).alias("implausible"))
          .with_columns(pl.when(pl.col("implausible")).then(None).otherwise(pl.col("market_cap")).alias("market_cap"))
          .drop("__to"))
    # Primary class per issuer and day: the most traded (median dollar volume, 60 trading days).
    df = df.sort("security_id", "date").with_columns(
        pl.col("dollar_volume").rolling_median(DV_WINDOW, min_samples=1).over("security_id").alias("__dv"))
    # Securities without a CIK are their own company.
    prim = (df.filter(pl.col("cik").is_not_null())
            .sort("__dv", "security_id", descending=[True, False], nulls_last=True)
            .unique(["date", "cik"], keep="first")
            .select("date", "cik", pl.col("security_id").alias("__p"), pl.col("market_cap").alias("issuer_market_cap")))
    df = (df.join(prim, on=["date", "cik"], how="left")
          .with_columns(pl.when(pl.col("cik").is_null()).then(True)
                        .otherwise(pl.col("security_id") == pl.col("__p")).fill_null(False).alias("is_primary"),
                        pl.when(pl.col("cik").is_null()).then(pl.col("market_cap"))
                        .otherwise(pl.col("issuer_market_cap")).alias("issuer_market_cap")))
    df = df.filter(pl.col("date").is_between(start, end)).select(
        "date", "security_id", "ticker", "type", "cik", "cik_status", pl.col("raw_close").alias("close"),
        pl.col("raw_volume").alias("volume"), "dollar_volume", "split_factor", "shares",
        pl.col("end").alias("shares_end"), pl.col("filed").alias("shares_filed"), pl.col("tag").alias("shares_tag"),
        pl.col("jump").alias("shares_jump"), "basis_ratio", "basis", "market_cap", "issuer_market_cap", "is_primary",
        "implausible")
    return df.cast(PANEL_SCHEMA).sort("date", "security_id")


def build(store, edgar, layout: Layout, start_year: int = 2003, years: list[int] | None = None) -> dict[str, Any]:
    """Rebuild every table. `years` limits which market-cap years are rewritten."""
    t0 = time.time()
    layout.ensure()
    secs = store.securities()
    equity = equity_securities(secs)
    log.info("cik map: checking Massive CIKs against SEC names")
    cik_map = build_cik_map(store.security_attributes(), edgar.profiles(), edgar.names())
    write_atomic(cik_map, layout.file("cik_map"))

    pairs = (cik_map.filter(pl.col("status").is_in(GOOD) & pl.col("cik").is_not_null())
             .select("security_id", "cik").unique().join(equity.select("security_id"), on="security_id"))
    log.info("share counts: %d security/CIK pairs", pairs.height)
    sec = edgar.shares(pairs["cik"].unique().to_list())
    obs = pairs.join(sec, on="cik")
    adrs = equity.filter(pl.col("type").is_in(ADR_TYPES))["security_id"].to_list()
    liquidity = liquidity_table(store, start_year, secs["last_trade"].max().year, equity)
    multi = multi_class(pairs, secs)
    counts, attribution = adjust(obs, split_events(store.security_actions("splits")), adrs, liquidity, multi)
    write_atomic(counts, layout.file("share_counts"))
    write_atomic(attribution, layout.file("split_attribution"))

    basis = calibrate(store, counts, cik_map, equity)
    write_atomic(basis, layout.file("basis"))

    last = secs["last_trade"].max()
    todo = years or list(range(start_year, last.year + 1))
    rows = implausible = 0
    for y in todo:
        panel = year_panel(store, y, equity, cik_map, counts, basis)
        write_atomic(panel, layout.market_cap_file(y))
        rows += panel.height
        implausible += panel.filter(pl.col("implausible")).select("security_id", "shares_end").n_unique()
        log.info("market cap %d: %s rows, %.0f%% with market cap", y, f"{panel.height:,}",
                 100 * panel["market_cap"].is_not_null().mean() if panel.height else 0)
    info = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"), "seconds": round(time.time() - t0, 1),
        "through": last.isoformat(), "years": [min(todo), max(todo)], "rows": rows,
        "cik_status": dict(cik_map.group_by("status").len().iter_rows()),
        "basis": dict(basis.group_by("basis").len().iter_rows()),
        "share_counts": counts.height, "share_jumps": int(counts["jump"].sum()),
        "unit_fixed": int(counts["unit_fixed"].sum()),
        "implausible_counts": implausible,
        "splits_not_applied": int((~attribution["applies"]).sum()) if attribution.height else 0,
    }
    layout.write_build({**layout.read_build(), **info})
    return info
