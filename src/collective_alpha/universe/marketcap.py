"""Point-in-time shares outstanding and market cap from SEC company facts.

A share count is usable on date D only if it was *filed* on or before D. Precedence when several
concepts exist: cover-page `shares_outstanding` (dei) > balance-sheet `common_shares` (us-gaap, the
only one multi-class issuers report undimensioned) > `wavg_shares_basic`.

ADRs: SEC filers report ordinary shares while the traded price is per depositary share. The vendor's
current ADS count calibrates the (static) ADS ratio so that history stays point-in-time.
"""

from __future__ import annotations

import polars as pl

from collective_alpha.config import Settings
from collective_alpha.storage import layout

SHARE_CONCEPTS = ["shares_outstanding", "common_shares", "wavg_shares_basic"]


def _latest_snapshot(settings: Settings, table: str, hint: str) -> pl.DataFrame:
    d = layout.curated_table_dir(settings, table)
    parts = sorted(p for p in d.glob("asof=*") if p.is_dir())
    if not parts:
        raise RuntimeError(f"run `{hint}` first")
    return pl.read_parquet(parts[-1] / "data.parquet")


def load_sec_facts(settings: Settings) -> pl.DataFrame:
    return _latest_snapshot(settings, "sec_facts", "ca sync sec-facts")


def load_ticker_details(settings: Settings) -> pl.DataFrame:
    return _latest_snapshot(settings, "ticker_details", "ca sync details")


def _snap_ratio(r: float, tol: float = 0.15) -> float:
    """ADS ratios are round numbers (5 ordinary shares per ADS, 1/4 ...). Snap when within tol."""
    if r >= 1:
        n = round(r)
        return float(n) if n > 0 and abs(r / n - 1) < tol else r
    inv = 1 / r
    n = round(inv)
    return 1 / n if n > 0 and abs(inv / n - 1) < tol else r


def ads_ratios(latest_sec: pl.DataFrame, ticker_details: pl.DataFrame, tol: float = 0.15) -> pl.DataFrame:
    """Ordinary shares per depositary share, per CIK, for issuers listed as ADRs.

    latest_sec: (cik, shares) latest filed count. ticker_details: vendor snapshot with type, cik,
    share_class_shares_outstanding (ADS-denominated for ADRs)."""
    adr = (
        ticker_details.filter(
            (pl.col("type") == "ADRC") & pl.col("cik").is_not_null() & (pl.col("share_class_shares_outstanding") > 0)
        )
        .group_by("cik")
        .agg(pl.col("share_class_shares_outstanding").max().alias("ads"))
    )
    j = adr.join(latest_sec, on="cik", how="inner").with_columns(raw=pl.col("shares") / pl.col("ads"))
    return j.with_columns(
        ads_ratio=pl.col("raw").map_elements(lambda r: _snap_ratio(r, tol), return_dtype=pl.Float64)
    ).select("cik", "ads_ratio")


def shares_series(
    facts: pl.DataFrame,
    ticker_details: pl.DataFrame | None = None,
    plausibility_band: float = 20.0,
) -> pl.DataFrame:
    """One row per (cik, filed): the best share count known from that filing date onward, in
    traded-share units (ordinary shares / ADS ratio for ADRs).

    Within a filing date the highest-precedence concept wins; across time the latest filing wins
    (handled by the as-of join). Values more than `plausibility_band` times away from the issuer's
    latest filing are dropped as filing errors (e.g. a 1000x unit slip)."""
    prec = {c: i for i, c in enumerate(SHARE_CONCEPTS)}
    base = facts.filter(
        pl.col("concept").is_in(SHARE_CONCEPTS)
        & (pl.col("unit") == "shares")
        & pl.col("val").is_not_null()
        & (pl.col("val") > 0)
    ).with_columns(prec=pl.col("concept").replace_strict(prec, return_dtype=pl.Int8))
    # one concept per issuer: the one reported in the most filings (ties -> precedence). Mixing
    # concepts across filings mixes units for issuers whose cover page counts ADSs but whose
    # balance sheet counts ordinary shares (e.g. Alibaba).
    chosen = (
        base.group_by(["cik", "concept", "prec"])
        .agg(pl.col("filed").n_unique().alias("n"))
        .sort(["cik", "n", "prec"], descending=[False, True, False])
        .unique(subset=["cik"], keep="first", maintain_order=True)
        .select("cik", "concept")
    )
    df = (
        base.join(chosen, on=["cik", "concept"], how="inner")
        .sort(["cik", "filed", "end"], descending=[False, False, True])
        .unique(subset=["cik", "filed"], keep="first", maintain_order=True)
        .select(
            "cik",
            "filed",
            pl.col("val").alias("shares_reported"),
            pl.col("concept").alias("shares_source"),
            pl.col("end").alias("shares_end"),
        )
        .sort(["cik", "filed"])
    )
    latest = df.group_by("cik").agg(pl.col("shares_reported").last().alias("latest"))
    df = df.join(latest, on="cik", how="left").filter(
        (pl.col("shares_reported") <= pl.col("latest") * plausibility_band)
        & (pl.col("shares_reported") >= pl.col("latest") / plausibility_band)
    )
    if ticker_details is not None:
        ratios = ads_ratios(latest.rename({"latest": "shares"}), ticker_details)
        df = df.join(ratios, on="cik", how="left")
    else:
        df = df.with_columns(ads_ratio=pl.lit(None, dtype=pl.Float64))
    return (
        df.with_columns(ads_ratio=pl.col("ads_ratio").fill_null(1.0))
        .with_columns(shares=pl.col("shares_reported") / pl.col("ads_ratio"))
        .select("cik", "filed", "shares", "shares_reported", "ads_ratio", "shares_source", "shares_end")
        .sort(["cik", "filed"])
    )


def shares_asof(
    series: pl.DataFrame, keys: pl.DataFrame, date_col: str = "date", max_age_days: int = 400
) -> pl.DataFrame:
    """Attach `shares` to rows (cik, date): the latest filing on or before date, if its period end is
    within max_age_days of the date (else null)."""
    out = keys.sort(["cik", date_col]).join_asof(
        series, left_on=date_col, right_on="filed", by="cik", strategy="backward", check_sortedness=False
    )
    stale = (pl.col(date_col) - pl.col("shares_end")).dt.total_days() > max_age_days
    return out.with_columns(
        shares=pl.when(stale).then(None).otherwise(pl.col("shares")),
        shares_source=pl.when(stale).then(None).otherwise(pl.col("shares_source")),
    ).drop(["filed", "shares_end", "shares_reported", "ads_ratio"])
