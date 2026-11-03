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
from ._names import adr_ratio, looks_adr, looks_converted, looks_equity
from ._shares import ADR_TYPES, adjust, basis_ratios, split_events
from ._successors import build_successors, inherited_counts

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
FLOAT_SCHEMA = {"float_fraction": pl.Float64, "float_date": pl.Date, "float_market_cap": pl.Float64,
                "issuer_float_market_cap": pl.Float64}
FLOAT_TABLE_SCHEMA = {
    "cik": pl.String, "float_date": pl.Date, "public_float": pl.Float64, "available_from": pl.Date,
    "market_cap_date": pl.Date, "issuer_market_cap": pl.Float64, "ratio": pl.Float64, "unit_factor": pl.Float64,
    "float_fraction": pl.Float64, "status": pl.String,
}
FLOAT_STALE_DAYS = 400      # a public float is used until this long after its 10-K became usable
FLOAT_RANGE = (0.01, 1.1)   # plausible public float / market cap; 1-1.1 is read as 1 (all public)
FLOAT_FIX_RANGE = (0.05, 1.1)  # a ratio moved by 1000^n must land here to count as a unit slip
PLAUSIBLE = (10 ** -5.5, 3.0)   # median daily turnover outside this: market cap withheld
# Penny ADRs (raw close under $2) whose market cap cannot be right: a penny price with little
# trading means too many shares, i.e. a wrong ADS ratio (NCNA at $0.81 and $37k a day shown as a
# $6.2B company). Real large ADRs under $2 (LYG, ABEV, SAN) trade tens of millions of dollars,
# roughly 1e-4 to 1e-3 of their value, a day.
ADR_PENNY = (2.0, 10 ** -4.5)   # price; median turnover of an ADR's penny days, whole history
ADR_THIN = (1e9, 1e6)           # a penny ADR shown above $1B on under $1M a day (60-day median)
# ... and only when shown at more than 10x Massive's current market cap: a wrong ratio inflates a
# micro-cap hundreds to tens of thousands of times (NCNA $6.2B, $4M today; BDRX $50B, $0.8M),
# while real ADRs that trade mostly at home stay near today's value when they dip under $2
# (Enel Chile ENIC, Tuya TUYA: about 1-2x).
ADR_ANCHOR = 10.0
PENNY_MIN_DAYS = 20             # penny (or thin) days needed before an ADR is judged as a whole
# An ADR shown above $50B whose US trading is under 1e-5 of that a day (60-day median): no real
# company that size trades that little even as an ADR (NTT DOCOMO at $106B: 3e-5; Westpac at
# $75B: 4.5e-5), while a stale ratio on a reorganized share count does (LATAM's old ADS after
# its 2022 bankruptcy: $1.7T on $7M a day).
ADR_GIANT = (5e10, 1e-5)


# Massive's when-issued suffixes: a lowercase w (HONw) or .WD / .WI (DHR.WD). Never a bare
# "WI": BBWI, AWI and POWI are ordinary tickers.
WHEN_ISSUED = r"(w|\.WD|\.WI)$"


def equity_securities(securities: pl.DataFrame) -> pl.DataFrame:
    """Common stock and ADRs (and untyped old securities whose name looks like equity). Baby
    bonds, warrants and units typed CS are left out by name, when-issued listings (HONw,
    DHR.WD) by ticker."""
    ok = pl.Series([looks_equity(n, t) for n, t in zip(securities["name"], securities["type"], strict=True)],
                   dtype=pl.Boolean)
    wi = pl.col("tickers").list.eval(pl.element().str.contains(WHEN_ISSUED)).list.all()
    return (securities.with_columns(ok.fill_null(True).alias("__eq"))
            .filter((pl.col("type").is_in(EQUITY_TYPES) | pl.col("type").is_null()) & pl.col("__eq") & ~wi)
            .select("security_id", "type", "name"))


def adr_flags(attrs: pl.DataFrame, equity: pl.DataFrame) -> pl.DataFrame:
    """Per equity security: adr (Massive typed it ADRC/GDR/NYRS in any month-end snapshot, or
    any of its names reads like a depositary receipt) and adr_now (it has not converted: its
    latest snapshot is not typed CS under a name that says ordinary shares or common stock).
    Massive types some ADSs as common stock (Taomee, iSoftStone, Youku) and an ADS that converted
    to ordinary shares by what it is today (Fanhua: ADRC until 2025, CS after), so the latest
    type alone misses both."""
    a = (attrs.select("security_id", "as_of", "name", "type").join(equity.select("security_id"), on="security_id")
         .with_columns((pl.col("type").is_in(ADR_TYPES)
                        | pl.col("name").map_elements(looks_adr, return_dtype=pl.Boolean).fill_null(False))
                       .alias("__adr")))
    last = (a.sort("as_of").group_by("security_id")
            .agg(pl.col("__adr").any().alias("adr"), pl.col("type").last().alias("__t"), pl.col("name").last().alias("__n")))
    last = last.with_columns(
        (pl.col("adr") & ~((pl.col("__t") == "CS")
                           & pl.col("__n").map_elements(looks_converted, return_dtype=pl.Boolean).fill_null(False)))
        .alias("adr_now"))
    out = equity.select("security_id", "type").join(last, on="security_id", how="left").with_columns(
        (pl.col("adr").fill_null(False) | pl.col("type").is_in(ADR_TYPES)).alias("adr"),
        (pl.col("adr_now").fill_null(False) | pl.col("type").is_in(ADR_TYPES)).alias("adr_now"))
    return out.select("security_id", "adr", "adr_now")


def adr_name_ratios(attrs: pl.DataFrame, ids: list[str]) -> pl.DataFrame:
    """security_id, as_of, ratio: ordinary shares per ADS stated in a month-end snapshot's name
    (only snapshots whose name states one)."""
    a = (attrs.filter(pl.col("security_id").is_in(ids)).select("security_id", "as_of", "name")
         .with_columns(pl.col("name").map_elements(adr_ratio, return_dtype=pl.Float64).alias("ratio")))
    return a.filter(pl.col("ratio").is_not_null()).select("security_id", "as_of", "ratio").sort("as_of")


def name_ratio_as_of(ratios: pl.DataFrame, when: pl.DataFrame) -> pl.DataFrame:
    """security_id, name_ratio: the ratio of the last ratio-stating name on or before `when`
    (security_id, date), else the first one after. A ratio belongs to its period: Honda's ADS was
    1 share until its 2023 3-for-1 split and 3 after, and its SEC counts stop in 2020."""
    if ratios.is_empty() or when.is_empty():
        return pl.DataFrame(schema={"security_id": pl.String, "name_ratio": pl.Float64})
    w = when.sort("date")
    back = w.join_asof(ratios.rename({"as_of": "__d"}), left_on="date", right_on="__d", by="security_id",
                       strategy="backward", check_sortedness=False).select("security_id", pl.col("ratio").alias("__b"))
    fwd = w.join_asof(ratios.rename({"as_of": "__d"}), left_on="date", right_on="__d", by="security_id",
                      strategy="forward", check_sortedness=False).select("security_id", pl.col("ratio").alias("__f"))
    return back.join(fwd, on="security_id").select("security_id", pl.coalesce("__b", "__f").alias("name_ratio"))


def load_basis_overrides(path: Path = BASIS_OVERRIDES) -> pl.DataFrame:
    schema = {"security_id": pl.String, "basis_ratio": pl.Float64, "note": pl.String}
    if not path.exists():
        return pl.DataFrame(schema=schema)
    df = pl.read_csv(path, comment_prefix="#", schema_overrides=schema)
    return df.select(list(schema)) if df.height else pl.DataFrame(schema=schema)


def calibrate(store, counts: pl.DataFrame, cik_map: pl.DataFrame, equity: pl.DataFrame,
              name_ratios: pl.DataFrame | None = None) -> pl.DataFrame:
    """Basis ratio per equity security from Massive's latest ticker-details snapshot; for ADRs
    without one, from `name_ratios` (adr_name_ratios) as of the security's latest SEC count."""
    if name_ratios is not None and "name_ratio" not in equity.columns:
        latest_count = counts.group_by("security_id").agg(pl.col("available_from").max().alias("date"))
        equity = equity.join(name_ratio_as_of(name_ratios, latest_count), on="security_id", how="left")
    try:
        det = store.ticker_details()
    except FileNotFoundError:
        log.warning("no Massive ticker_details yet: ADRs get no market cap, stocks assume ratio 1 "
                    "(run: subspace backfill rest ticker_details)")
        det = pl.DataFrame()
    flags = [c for c in ("adr", "adr_now", "name_ratio") if c in equity.columns]
    latest = equity.select("security_id", "type", *flags)
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
    b = basis_ratios(latest.select("security_id", "type", "massive_shares", "shares_now", "classes", *flags))
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


def daily(store, start: date, end: date, equity: pl.DataFrame, cik_map: pl.DataFrame, counts: pl.DataFrame,
          basis: pl.DataFrame) -> pl.DataFrame:
    """Unscreened market cap per equity security and trading day from `start` to `end`."""
    lf = store.bars(start=start, end=end, adjust="splits", lazy=True)
    df = (lf.select("date", "ticker", "security_id", "close", "volume", "split_factor")
          .join(equity.lazy().select("security_id", "type", *(["adr"] if "adr" in equity.columns else [])),
                on="security_id").collect())
    if "adr" not in df.columns:
        df = df.with_columns(pl.col("type").is_in(ADR_TYPES).alias("adr"))
    if df.is_empty():
        return df
    df = cik_on(df, cik_map)
    sc = (counts.select("security_id", "cik", "available_from", "end", "filed", "shares_now", "tag", "jump")
          .sort("available_from"))
    df = df.sort("date").join_asof(sc, left_on="date", right_on="available_from", by=["security_id", "cik"],
                                   strategy="backward", check_sortedness=False)
    fresh = pl.col("end") >= pl.col("date") - pl.duration(days=STALE_DAYS)
    df = df.with_columns(*[pl.when(fresh).then(pl.col(c)).otherwise(None).alias(c)
                           for c in ("shares_now", "end", "filed", "tag", "jump")])
    df = df.join(basis.select("security_id", "basis_ratio", "basis", "massive_market_cap"), on="security_id",
                 how="left")
    df = df.with_columns(
        (pl.col("close") * pl.col("shares_now") * pl.col("basis_ratio")).alias("market_cap"),
        (pl.col("shares_now") * pl.col("split_factor") * pl.col("basis_ratio")).alias("shares"),
        (pl.col("close") / pl.col("split_factor")).alias("raw_close"),
        (pl.col("volume") * pl.col("split_factor")).alias("raw_volume"),
        (pl.col("close") * pl.col("volume")).alias("dollar_volume"))
    return df.sort("security_id", "date").with_columns(
        pl.col("dollar_volume").rolling_median(DV_WINDOW, min_samples=1).over("security_id").alias("dv60"))


def _inflated(cap: pl.Expr) -> pl.Expr:
    """`cap` above ADR_ANCHOR x Massive's current market cap (true when Massive has none)."""
    return (pl.col("massive_market_cap").is_null() | (cap > ADR_ANCHOR * pl.col("massive_market_cap")))


def _thin_penny() -> pl.Expr:
    """A day an ADR's market cap cannot be right: under $2, shown above $1B on under $1M a day,
    and far above what Massive says the company is worth today."""
    return (pl.col("adr") & (pl.col("raw_close") < ADR_PENNY[0])
            & (pl.col("market_cap") > ADR_THIN[0]) & (pl.col("dv60") < ADR_THIN[1])
            & _inflated(pl.col("market_cap")))


def penny_basis(store, first: int, last: int, equity: pl.DataFrame, cik_map: pl.DataFrame,
                counts: pl.DataFrame, basis: pl.DataFrame) -> pl.DataFrame:
    """The basis table with, per ADR over every year: penny_days (days under ADR_PENNY[0] with a
    market cap), penny_turnover and penny_market_cap (their medians) and thin_days (_thin_penny
    days). The basis becomes "implausible" when either is damning: at least PENNY_MIN_DAYS penny
    days with turnover under ADR_PENNY[1] and a median market cap above ADR_ANCHOR x Massive's
    current one, or at least PENNY_MIN_DAYS thin days. The ratio is one number per security,
    so it is wrong on its other days too. Computed from all years whatever a build rewrites, so a partial rebuild
    judges an ADR the same way. Overrides are never marked."""
    stats = {"security_id": pl.String, "penny_days": pl.UInt32, "penny_turnover": pl.Float64,
             "penny_market_cap": pl.Float64, "thin_days": pl.UInt32}
    adrs = equity.filter(pl.col("adr") if "adr" in equity.columns else pl.col("type").is_in(ADR_TYPES))
    df = daily(store, date(first, 1, 1), date(last, 12, 31), adrs, cik_map, counts, basis)
    if df.is_empty():
        st = pl.DataFrame(schema=stats)
    else:
        st = (df.filter(pl.col("market_cap").is_not_null() & (pl.col("raw_close") < ADR_PENNY[0]))
              .group_by("security_id")
              .agg(pl.len().alias("penny_days"),
                   (pl.col("dollar_volume") / pl.col("market_cap")).median().alias("penny_turnover"),
                   pl.col("market_cap").median().alias("penny_market_cap"),
                   _thin_penny().sum().alias("thin_days"))
              .cast(stats))
    bad = (((pl.col("penny_days") >= PENNY_MIN_DAYS) & (pl.col("penny_turnover") < ADR_PENNY[1])
            & _inflated(pl.col("penny_market_cap")))
           | (pl.col("thin_days") >= PENNY_MIN_DAYS))
    out = basis.drop(*[c for c in stats if c != "security_id"], strict=False).join(st, on="security_id", how="left")
    return out.with_columns(pl.when(bad.fill_null(False) & (pl.col("basis") != "override"))
                            .then(pl.lit("implausible")).otherwise(pl.col("basis")).alias("basis"))


def screen(df: pl.DataFrame) -> pl.DataFrame:
    """Safety net: no market cap where it cannot be right. Four rules set `implausible`:
    - any security, while one SEC count is in use (security_id, end, filed): median turnover
      outside PLAUSIBLE (the count or the ratio is wrong);
    - ADRs whose basis is "implausible" (penny_basis): every day;
    - any ADR day under $2 shown above $1B on under $1M a day (ADR_THIN) and above ADR_ANCHOR x
      Massive's current market cap, however few;
    - any ADR day shown above $50B on a 60-day median turnover under 1e-5 (ADR_GIANT).
    `penny_adr` marks days withheld by the ADR rules alone (the turnover rule passed them).
    df: daily() columns (security_id, type, end, filed, basis, raw_close, dollar_volume, dv60,
    market_cap)."""
    seg = ["security_id", "end", "filed"]
    to = (df.filter(pl.col("market_cap").is_not_null())
          .group_by(seg).agg((pl.col("dollar_volume") / pl.col("market_cap")).median().alias("__to")))
    general = ~pl.col("__to").is_between(*PLAUSIBLE)
    giant = (pl.col("adr") & (pl.col("market_cap") > ADR_GIANT[0])
             & (pl.col("dv60") < ADR_GIANT[1] * pl.col("market_cap")))
    penny = ((pl.col("basis") == "implausible") | _thin_penny() | giant) & pl.col("market_cap").is_not_null()
    return (df.join(to, on=seg, how="left", nulls_equal=False)
            .with_columns(general.fill_null(False).alias("__g"), penny.fill_null(False).alias("__p"))
            .with_columns((pl.col("__g") | pl.col("__p")).alias("implausible"),
                          (pl.col("__p") & ~pl.col("__g")).alias("penny_adr"))
            .with_columns(pl.when(pl.col("implausible")).then(None).otherwise(pl.col("market_cap")).alias("market_cap"))
            .drop("__to", "__g", "__p"))


def year_panel(store, year: int, equity: pl.DataFrame, cik_map: pl.DataFrame, counts: pl.DataFrame,
               basis: pl.DataFrame, stats: dict[str, int] | None = None) -> pl.DataFrame:
    """One row per equity security and trading day of `year`. `stats` (optional) collects
    penny_adr_days: security-days whose market cap only the ADR penny rule withheld."""
    start, end = date(year, 1, 1), date(year, 12, 31)
    df = daily(store, start - timedelta(days=130), end, equity, cik_map, counts, basis)
    if df.is_empty():
        return pl.DataFrame(schema=PANEL_SCHEMA)
    df = screen(df)
    # Primary class per issuer and day: the most traded (median dollar volume, 60 trading days).
    # Securities without a CIK are their own company.
    prim = (df.filter(pl.col("cik").is_not_null())
            .sort("dv60", "security_id", descending=[True, False], nulls_last=True)
            .unique(["date", "cik"], keep="first")
            .select("date", "cik", pl.col("security_id").alias("__p"), pl.col("market_cap").alias("issuer_market_cap")))
    df = (df.join(prim, on=["date", "cik"], how="left")
          .with_columns(pl.when(pl.col("cik").is_null()).then(True)
                        .otherwise(pl.col("security_id") == pl.col("__p")).fill_null(False).alias("is_primary"),
                        pl.when(pl.col("cik").is_null()).then(pl.col("market_cap"))
                        .otherwise(pl.col("issuer_market_cap")).alias("issuer_market_cap")))
    df = df.filter(pl.col("date").is_between(start, end))
    if stats is not None:
        stats["penny_adr_days"] = stats.get("penny_adr_days", 0) + int(df["penny_adr"].sum())
    df = df.select(
        "date", "security_id", "ticker", "type", "cik", "cik_status", pl.col("raw_close").alias("close"),
        pl.col("raw_volume").alias("volume"), "dollar_volume", "split_factor", "shares",
        pl.col("end").alias("shares_end"), pl.col("filed").alias("shares_filed"), pl.col("tag").alias("shares_tag"),
        pl.col("jump").alias("shares_jump"), "basis_ratio", "basis", "market_cap", "issuer_market_cap", "is_primary",
        "implausible")
    return df.cast(PANEL_SCHEMA).sort("date", "security_id")


def scan_panels(files: list[Path]) -> pl.LazyFrame:
    """Lazy frame over market-cap year files. Files written before the float columns existed
    (a partial rebuild) read them as null."""
    schema = {**PANEL_SCHEMA, **FLOAT_SCHEMA}
    if not files:
        return pl.LazyFrame(schema=schema)
    return pl.scan_parquet(files, schema=schema, missing_columns="insert", extra_columns="ignore")


def float_table(edgar, primary: pl.LazyFrame) -> pl.DataFrame:
    """Public float per company (10-K cover, `dei:EntityPublicFloat`) as a fraction of its
    market cap on the float date.

    `primary`: date, cik, issuer_market_cap of the companies' primary classes (the built panel).
    The fraction divides the dollar float by the issuer market cap on the float date (the last
    trading day on or before it, within a week), so it needs no share count of its own.
    Statuses: ok; unit_fixed (off by a power of 1000, as in share counts); no_market_cap;
    implausible (outside FLOAT_RANGE even after the unit fix). Only USD floats are used.
    """
    caps = (primary.filter(pl.col("cik").is_not_null() & pl.col("issuer_market_cap").is_not_null())
            .select("date", "cik", "issuer_market_cap").collect().sort("date"))
    ciks = caps["cik"].unique().to_list()
    s = edgar.series(["public_float"], ciks) if ciks else None
    if s is None or s.is_empty():
        return pl.DataFrame(schema=FLOAT_TABLE_SCHEMA)
    s = (s.filter(pl.col("unit") == "USD").select("cik", pl.col("end").alias("float_date"),
                                                pl.col("value").alias("public_float"), "available_from")
         .sort("float_date"))
    df = s.join_asof(caps.rename({"date": "market_cap_date"}), left_on="float_date", right_on="market_cap_date",
                     by="cik", strategy="backward", tolerance="7d", check_sortedness=False)
    df = df.with_columns((pl.col("public_float") / pl.col("issuer_market_cap")).alias("ratio"))
    lo, hi = FLOAT_RANGE
    plausible = pl.col("ratio").is_between(lo, hi)
    factor = pl.lit(None, pl.Float64)
    for k in (1e-9, 1e9, 1e-6, 1e6, 1e-3, 1e3):  # the range spans < 1000x: at most one fits
        factor = pl.when((pl.col("ratio") * k).is_between(*FLOAT_FIX_RANGE)).then(pl.lit(k)).otherwise(factor)
    df = df.with_columns(pl.when(pl.col("ratio").is_null()).then(None).when(plausible).then(pl.lit(1.0))
                         .otherwise(factor).alias("unit_factor"))
    df = df.with_columns(
        (pl.col("ratio") * pl.col("unit_factor")).clip(upper_bound=1.0).alias("float_fraction"),
        pl.when(pl.col("ratio").is_null()).then(pl.lit("no_market_cap"))
        .when(pl.col("unit_factor").is_null()).then(pl.lit("implausible"))
        .when(pl.col("unit_factor") != 1).then(pl.lit("unit_fixed"))
        .otherwise(pl.lit("ok")).alias("status"))
    return df.select(list(FLOAT_TABLE_SCHEMA)).cast(FLOAT_TABLE_SCHEMA).sort("cik", "float_date")


def inherited_floats(floats: pl.DataFrame, successors: pl.DataFrame) -> pl.DataFrame:
    """Predecessor floats filed before each link, re-keyed to the successor CIK: the successor
    files its first 10-K up to a year later."""
    links = (successors.filter(pl.col("cik") != pl.col("predecessor_cik"))
             .select("cik", pl.col("predecessor_cik").alias("__pc"), "link_date").unique(["cik", "__pc"]))
    if links.is_empty():
        return floats.clear()
    return (links.join(floats.rename({"cik": "__pc"}), on="__pc")
            .filter(pl.col("available_from") <= pl.col("link_date"))
            .select(list(FLOAT_TABLE_SCHEMA)))


def add_float(panel: pl.DataFrame, floats: pl.DataFrame) -> pl.DataFrame:
    """Float columns per security and day: the company's latest usable public-float fraction
    (usable the day after its 10-K, for FLOAT_STALE_DAYS), applied to every class."""
    f = floats.filter(pl.col("float_fraction").is_not_null())
    # A later filing about an older float date must not replace a newer one.
    f = (f.sort("cik", "available_from", "float_date")
         .with_columns(pl.col("float_date").cum_max().over("cik").alias("__m"))
         .filter(pl.col("float_date") >= pl.col("__m"))
         .select("cik", "available_from", "float_date", "float_fraction").sort("available_from"))
    out = panel.sort("date").join_asof(f, left_on="date", right_on="available_from", by="cik",
                                       strategy="backward", check_sortedness=False)
    fresh = pl.col("date") <= pl.col("available_from") + pl.duration(days=FLOAT_STALE_DAYS)
    out = out.with_columns(*[pl.when(fresh).then(pl.col(c)).otherwise(None).alias(c)
                             for c in ("float_fraction", "float_date")])
    out = out.with_columns((pl.col("market_cap") * pl.col("float_fraction")).alias("float_market_cap"),
                           (pl.col("issuer_market_cap") * pl.col("float_fraction")).alias("issuer_float_market_cap"))
    return out.select(*PANEL_SCHEMA, *FLOAT_SCHEMA).cast({**PANEL_SCHEMA, **FLOAT_SCHEMA}).sort("date", "security_id")


def build(store, edgar, layout: Layout, start_year: int = 2003, years: list[int] | None = None) -> dict[str, Any]:
    """Rebuild every table. `years` limits which market-cap years are rewritten."""
    t0 = time.time()
    layout.ensure()
    secs = store.securities()
    equity = equity_securities(secs)
    attrs = store.security_attributes()
    equity = equity.join(adr_flags(attrs, equity), on="security_id", how="left")
    log.info("ADRs by type or name: %d (%d typed otherwise today)", int(equity["adr"].sum()),
             int((equity["adr"] & ~equity["type"].is_in(ADR_TYPES).fill_null(False)).sum()))
    log.info("cik map: checking Massive CIKs against SEC names")
    cik_map = build_cik_map(attrs, edgar.profiles(), edgar.names())
    write_atomic(cik_map, layout.file("cik_map"))

    successors = build_successors(store, secs, cik_map, equity["security_id"].to_list())
    write_atomic(successors, layout.file("successors"))
    log.info("successors: %s", dict(successors.group_by("kind").len().iter_rows()))

    pairs = (cik_map.filter(pl.col("status").is_in(GOOD) & pl.col("cik").is_not_null())
             .select("security_id", "cik").unique().join(equity.select("security_id"), on="security_id"))
    log.info("share counts: %d security/CIK pairs", pairs.height)
    sec = edgar.shares(pairs["cik"].unique().to_list())
    obs = pairs.join(sec, on="cik")
    # Until a successor files its own count, the predecessor's last counts carry its market cap.
    events = split_events(store.security_actions("splits"))
    obs = pl.concat([obs, inherited_counts(obs, successors, STALE_DAYS, events)])
    adrs = equity.filter(pl.col("adr"))["security_id"].to_list()
    liquidity = liquidity_table(store, start_year, secs["last_trade"].max().year, equity)
    multi = multi_class(pairs, secs)
    counts, attribution = adjust(obs, events, adrs, liquidity, multi,
                                 first_trade=secs.select("security_id", "first_trade", "last_trade"))
    write_atomic(counts, layout.file("share_counts"))
    write_atomic(attribution, layout.file("split_attribution"))

    last = secs["last_trade"].max()
    basis = calibrate(store, counts, cik_map, equity, adr_name_ratios(attrs, adrs))
    basis = penny_basis(store, start_year, last.year, equity, cik_map, counts, basis)
    write_atomic(basis, layout.file("basis"))
    log.info("penny ADRs: %d with an implausible basis", int((basis["basis"] == "implausible").sum()))

    todo = years or list(range(start_year, last.year + 1))
    rows = implausible = 0
    stats: dict[str, int] = {}
    for y in todo:
        panel = year_panel(store, y, equity, cik_map, counts, basis, stats)
        write_atomic(panel, layout.market_cap_file(y))
        rows += panel.height
        implausible += panel.filter(pl.col("implausible")).select("security_id", "shares_end").n_unique()
        log.info("market cap %d: %s rows, %.0f%% with market cap", y, f"{panel.height:,}",
                 100 * panel["market_cap"].is_not_null().mean() if panel.height else 0)

    # Public float needs the issuer market cap on each float date, so it follows the panels.
    files = layout.market_cap_files()
    primary = scan_panels(files).filter(pl.col("is_primary"))
    floats = float_table(edgar, primary)
    write_atomic(floats, layout.file("public_float"))
    with_inherited = pl.concat([floats, inherited_floats(floats, successors)])
    for y in todo:
        path = layout.market_cap_file(y)
        if path.exists():
            write_atomic(add_float(pl.read_parquet(path).select(list(PANEL_SCHEMA)), with_inherited), path)
    log.info("public float: %d filings, %s", floats.height, dict(floats.group_by("status").len().iter_rows()))
    all_years = [int(p.parent.name.split("=")[1]) for p in layout.market_cap_files()]
    info = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"), "seconds": round(time.time() - t0, 1),
        "through": last.isoformat(), "years": [min(all_years), max(all_years)], "rewritten": [min(todo), max(todo)],
        "rows": rows,
        "cik_status": dict(cik_map.group_by("status").len().iter_rows()),
        "basis": dict(basis.group_by("basis").len().iter_rows()),
        "share_counts": counts.height, "share_jumps": int(counts["jump"].sum()),
        "unit_fixed": int(counts["unit_fixed"].sum()),
        "implausible_counts": implausible,
        "penny_adr_days": stats.get("penny_adr_days", 0),
        "splits_not_applied": int((~attribution["applies"]).sum()) if attribution.height else 0,
        "public_float": dict(floats.group_by("status").len().iter_rows()),
        "successors": dict(successors.group_by("kind").len().iter_rows()),
        "inherited_counts": int(obs["tag"].str.starts_with("successor:").sum()),
    }
    layout.write_build({**layout.read_build(), **info})
    return info
