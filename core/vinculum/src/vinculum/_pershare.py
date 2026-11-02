"""Per-share items (EPS) per security, in the security's units on each day.

EDGAR reports EPS as filed: a quarter filed before a split is in pre-split units, a filing
after it restates earlier periods. EDGAR alone cannot sum quarters across a split, but the
security's split history can: each quarter is brought to a common basis with the splits
executed up to its filing (w = value x C(filed), C = cumulative split product), summed, and
divided by C(day) when read. At fiscal year ends the reported annual figure is used.
"""

from __future__ import annotations

import polars as pl

QUARTER_GAP = (0, 7)     # days between a period's start and the previous period's end
YEAR = (350, 380)        # span of four quarters (two halves)
N = {"quarter": 4, "half": 2}


def cum_splits(events: pl.DataFrame) -> pl.DataFrame:
    """security_id, execution_date, c: product of split ratios up to and including that day."""
    return (events.sort("security_id", "execution_date")
            .select("security_id", "execution_date", pl.col("k").cum_prod().over("security_id").alias("c")))


def factor_at(df: pl.DataFrame, col: str, cum: pl.DataFrame, name: str) -> pl.DataFrame:
    """Add `name` = cumulative split product of the row's security on date `col` (1 before any
    split)."""
    if cum.is_empty():
        return df.with_columns(pl.lit(1.0).alias(name))
    df = df.with_row_index("__r")
    f = (df.select("__r", "security_id", col).drop_nulls(col).sort(col)
         .join_asof(cum.rename({"execution_date": "__x"}), left_on=col, right_on="__x", by="security_id",
                    strategy="backward", check_sortedness=False)
         .select("__r", pl.col("c").fill_null(1.0).alias(name)))
    return df.join(f, on="__r", how="left").with_columns(pl.col(name).fill_null(1.0)).drop("__r")


def per_share_ttm(rows: pl.DataFrame, pairs: pl.DataFrame, cum: pl.DataFrame) -> pl.DataFrame:
    """Trailing-twelve-month per-share values per security on a split-free basis.

    rows: EdgarStore.series rows of one per-share item (cik, unit, period, start, end, value,
    filed). pairs: security_id, cik. Returns security_id, end, unit, w (divide by the
    cumulative split product on the day of use), available (the day after the last filing).
    """
    out_schema = {"security_id": pl.String, "end": pl.Date, "unit": pl.String, "w": pl.Float64,
                  "available": pl.Date}
    r = rows.join(pairs, on="cik")
    if r.is_empty():
        return pl.DataFrame(schema=out_schema)
    r = factor_at(r, "filed", cum, "__c").with_columns((pl.col("value") * pl.col("__c")).alias("w"))
    annual = r.filter(pl.col("period") == "annual").select(
        "security_id", "end", "unit", "w", (pl.col("filed") + pl.duration(days=1)).alias("available"))
    sums = []
    for period, n in N.items():
        g = ["security_id", "cik", "unit"]
        p = r.filter(pl.col("period") == period).sort(*g, "end")
        gap = (pl.col("start") - pl.col("end").shift(1).over(g)).dt.total_days()
        p = p.with_columns(gap.alias("__gap"))
        ok = pl.lit(True)
        for k in range(n - 1):
            ok = ok & pl.col("__gap").shift(k).over(g).is_between(*QUARTER_GAP)
        span = (pl.col("end") - pl.col("start").shift(n - 1).over(g)).dt.total_days()
        total = pl.sum_horizontal(*[pl.col("w").shift(k).over(g) for k in range(n)])
        last = pl.max_horizontal(*[pl.col("filed").shift(k).over(g) for k in range(n)])
        p = p.with_columns(total.alias("__w"), (last + pl.duration(days=1)).alias("available"),
                           (ok & span.is_between(*YEAR)).alias("__ok"))
        sums.append(p.filter("__ok").select("security_id", "end", "unit", pl.col("__w").alias("w"), "available"))
    s = pl.concat(sums).join(annual.select("security_id", "end"), on=["security_id", "end"], how="anti")
    return (pl.concat([annual, s]).unique(["security_id", "end", "unit"], keep="first")
            .select(list(out_schema)).cast(out_schema).sort("security_id", "end"))
