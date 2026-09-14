"""Point-in-time fundamentals from SEC company facts.

Flows (revenue, net income, cash flow) are reported as durations: quarters in 10-Qs and the full
year in 10-Ks. We rebuild a clean quarterly series per issuer (Q4 = FY - Q1 - Q2 - Q3 when the
filer does not report Q4 separately), then trailing-twelve-month sums, each available from the
*filed* date of its latest constituent. Stocks (equity, assets, ...) are instants and used as-of
their filed date directly.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from collective_alpha.features.base import FeatureContext

FLOW_CONCEPTS = ["revenue", "revenue_asc606", "net_income", "cfo"]
STOCK_CONCEPTS = ["equity", "assets", "liabilities", "cash", "lt_debt"]


def quarterly_flows(facts: pl.DataFrame, concept: str) -> pl.DataFrame:
    """(cik, end, filed, val): one row per fiscal quarter for a duration concept.

    Filers report flows either as discrete quarters (income statement) or year-to-date from the
    fiscal-year start (cash-flow statement, and the 10-K annual total). Every period is treated as
    year-to-date within its (cik, start) group and differenced against the previous period in the
    same group; a direct quarter (~90 days) is its own value. The 10-K's annual figure minus the
    three reported quarters yields Q4 when no 9-month YTD exists."""
    f = (
        facts.filter((pl.col("concept") == concept) & pl.col("start").is_not_null() & pl.col("end").is_not_null())
        .with_columns(span=(pl.col("end") - pl.col("start")).dt.total_days())
        .filter((pl.col("span") >= 80) & (pl.col("span") <= 380))
        # earliest filing of each (cik, start, end) is the point-in-time value; restatements ignored
        .sort(["cik", "start", "end", "filed"])
        .unique(subset=["cik", "start", "end"], keep="first", maintain_order=True)
    )
    direct = f.filter(pl.col("span") <= 100).select("cik", "end", "filed", "val", pl.lit(0).alias("prio"))
    # YTD differencing: previous period with the same start, one quarter earlier
    ytd = f.sort(["cik", "start", "end"]).with_columns(
        prev_end=pl.col("end").shift(1).over(["cik", "start"]),
        prev_val=pl.col("val").shift(1).over(["cik", "start"]),
    )
    gap = (pl.col("end") - pl.col("prev_end")).dt.total_days()
    diffed = ytd.filter((pl.col("span") > 100) & pl.col("prev_end").is_not_null() & (gap >= 80) & (gap <= 100)).select(
        "cik", "end", "filed", (pl.col("val") - pl.col("prev_val")).alias("val"), pl.lit(1).alias("prio")
    )
    # annual total minus the three direct quarters inside the fiscal year (quarterly-only filers)
    fy = f.filter(pl.col("span") >= 350).select(
        "cik",
        pl.col("start").alias("fy_start"),
        pl.col("end").alias("fy_end"),
        pl.col("filed").alias("fy_filed"),
        pl.col("val").alias("fy_val"),
    )
    inside = (
        direct.select("cik", "end", "val")
        .join(fy, on="cik", how="inner")
        .filter((pl.col("end") > pl.col("fy_start")) & (pl.col("end") < pl.col("fy_end")))
        .group_by(["cik", "fy_end"])
        .agg(pl.col("val").sum().alias("q_sum"), pl.len().alias("nq"))
    )
    q4 = (
        fy.join(inside, on=["cik", "fy_end"], how="inner")
        .filter(pl.col("nq") == 3)
        .select(
            "cik",
            pl.col("fy_end").alias("end"),
            pl.col("fy_filed").alias("filed"),
            (pl.col("fy_val") - pl.col("q_sum")).alias("val"),
            pl.lit(2).alias("prio"),
        )
    )
    out = pl.concat([direct, diffed, q4], how="vertical").sort(["cik", "end", "prio"])
    return (
        out.unique(subset=["cik", "end"], keep="first", maintain_order=True)
        .select("cik", "end", "filed", "val")
        .sort(["cik", "end"])
    )


def ttm(quarters: pl.DataFrame, max_gap_days: int = 120) -> pl.DataFrame:
    """(cik, filed, ttm) trailing sum of 4 consecutive quarters; available at the max filed date of
    the four. Consecutive means each quarter end is within max_gap_days of the previous."""
    q = quarters.sort(["cik", "end"]).with_columns(
        [pl.col("val").shift(i).over("cik").alias(f"v{i}") for i in range(1, 4)]
        + [pl.col("filed").shift(i).over("cik").alias(f"f{i}") for i in range(1, 4)]
        + [pl.col("end").shift(3).over("cik").alias("end3")]
    )
    ok = ((pl.col("end") - pl.col("end3")).dt.total_days() <= 3 * max_gap_days) & pl.col("v3").is_not_null()
    return (
        q.filter(ok)
        .with_columns(
            ttm=pl.col("val") + pl.col("v1") + pl.col("v2") + pl.col("v3"),
            avail=pl.max_horizontal("filed", "f1", "f2", "f3"),
        )
        .select("cik", pl.col("avail").alias("filed"), "end", "ttm")
        .sort(["cik", "filed", "end"])
        .unique(subset=["cik", "filed"], keep="last", maintain_order=True)
    )


def latest_instant(facts: pl.DataFrame, concept: str) -> pl.DataFrame:
    """(cik, filed, end, val): point-in-time series of an instant concept (first filing per end)."""
    f = facts.filter((pl.col("concept") == concept) & pl.col("end").is_not_null())
    return (
        f.sort(["cik", "end", "filed"])
        .unique(subset=["cik", "end"], keep="first", maintain_order=True)
        .sort(["cik", "filed", "end"])
        .unique(subset=["cik", "filed"], keep="last", maintain_order=True)
        .select("cik", "filed", "end", "val")
    )


def _asof(keys: pl.DataFrame, series: pl.DataFrame, value: str, out: str, max_age_days: int = 400) -> pl.DataFrame:
    ser = series.sort(["cik", "filed"]).rename({value: out, "end": f"__end_{out}"})
    j = keys.sort(["cik", "date"]).join_asof(
        ser, left_on="date", right_on="filed", by="cik", strategy="backward", check_sortedness=False
    )
    stale = (pl.col("date") - pl.col(f"__end_{out}")).dt.total_days() > max_age_days
    return j.with_columns(pl.when(stale).then(None).otherwise(pl.col(out)).alias(out)).drop(["filed", f"__end_{out}"])


def fundamental_features(facts: pl.DataFrame, keys: pl.DataFrame, cap: pl.DataFrame | None = None) -> pl.DataFrame:
    """keys: (security_id, date, cik). cap: optional (security_id, date, cap) for valuation ratios."""
    k = keys.filter(pl.col("cik").is_not_null())
    rev = (
        pl.concat([quarterly_flows(facts, "revenue"), quarterly_flows(facts, "revenue_asc606")])
        .sort(["cik", "end", "filed"])
        .unique(subset=["cik", "end"], keep="first", maintain_order=True)
    )
    out = k
    out = _asof(out, ttm(rev), "ttm", "revenue_ttm")
    out = _asof(out, ttm(quarterly_flows(facts, "net_income")), "ttm", "net_income_ttm")
    out = _asof(out, ttm(quarterly_flows(facts, "cfo")), "ttm", "cfo_ttm")
    for c in STOCK_CONCEPTS:
        out = _asof(out, latest_instant(facts, c), "val", c)
    # asset growth: assets vs the instant ~1 year earlier (as-of 365 days before)
    prev = out.select(
        "security_id", pl.col("date").alias("date_prev"), pl.col("assets").alias("assets_prev")
    ).with_columns(date=pl.col("date_prev") + pl.duration(days=365))
    out = out.sort(["security_id", "date"]).join_asof(
        prev.sort(["security_id", "date"]).select("security_id", "date", "assets_prev"),
        on="date",
        by="security_id",
        strategy="backward",
        check_sortedness=False,
    )
    out = out.with_columns(
        asset_growth=pl.col("assets") / pl.col("assets_prev") - 1,
        roe=pl.col("net_income_ttm") / pl.col("equity"),
        roa=pl.col("net_income_ttm") / pl.col("assets"),
        leverage=pl.col("liabilities") / pl.col("assets"),
        cash_to_assets=pl.col("cash") / pl.col("assets"),
        accruals=(pl.col("net_income_ttm") - pl.col("cfo_ttm")) / pl.col("assets"),
        profit_margin=pl.col("net_income_ttm") / pl.col("revenue_ttm"),
    ).drop("assets_prev")
    if cap is not None:
        out = (
            out.join(cap, on=["security_id", "date"], how="left")
            .with_columns(
                earnings_yield=pl.col("net_income_ttm") / pl.col("cap"),
                book_to_market=pl.col("equity") / pl.col("cap"),
                sales_to_price=pl.col("revenue_ttm") / pl.col("cap"),
                cfo_yield=pl.col("cfo_ttm") / pl.col("cap"),
            )
            .drop("cap")
        )
    return out.drop("cik")


def build(ctx: FeatureContext) -> pl.DataFrame:
    from collective_alpha.features.size import size_features

    cap = size_features(ctx.panel, ctx.security_cik, ctx.shares).select("security_id", "date", "cap")
    return fundamental_features(ctx.sec_facts, ctx.security_cik, cap)


__all__ = ["quarterly_flows", "ttm", "latest_instant", "fundamental_features", "build", "dt"]
