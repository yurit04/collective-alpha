"""Point-in-time rules, as pure functions on the long fact table.

1. A fact is usable from the day after it was filed (`available_from = filed + 1 day`).
2. Per period, the first-filed value (policy="first") or the latest restatement ("latest").
3. A value is treated as missing once its period ended more than STALE_DAYS earlier.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from ._concepts import SHARE_TAGS, Concept

STALE_DAYS = 400
QUARTER = (80, 100)     # days in a fiscal quarter (52/53-week years included)
HALF = (170, 195)
YEAR = (350, 380)
MAX_SHARE_JUMP = 20.0   # share counts this far from the latest are filing errors

SERIES_SCHEMA = {
    "cik": pl.String, "item": pl.String, "unit": pl.String, "period": pl.String,
    "start": pl.Date, "end": pl.Date, "value": pl.Float64, "filed": pl.Date, "available_from": pl.Date,
    "tag": pl.String, "source": pl.String, "ttm": pl.Float64, "ttm_available_from": pl.Date,
}


def _tag_col() -> pl.Expr:
    return pl.concat_str(["taxonomy", "tag"], separator=":")


def unit_filter(unit: str) -> pl.Expr:
    if unit == "currency":
        return pl.col("unit").str.contains(r"^[A-Z]{3}$")
    if unit == "currency/shares":
        return pl.col("unit").str.contains(r"^[A-Z]{3}/shares$")
    return pl.col("unit") == unit


def choose(df: pl.DataFrame, policy: str = "first") -> pl.DataFrame:
    """One value per (company, tag, unit, period): the first-filed or the latest one."""
    if policy not in ("first", "latest"):
        raise ValueError("policy must be 'first' or 'latest'")
    keys = ["cik", "taxonomy", "tag", "unit", "start", "end"]
    return (df.sort("filed", "accn")
            .unique(keys, keep="first" if policy == "first" else "last", maintain_order=True))


def resolve_tags(df: pl.DataFrame, tags: tuple[str, ...], keys: list[str], policy: str = "first") -> pl.DataFrame:
    """One value per company and period across tags: the one known first (policy="first") or
    last ("latest"); tag priority only breaks ties within the same filing date. (A company that
    switched tags reports old periods under the new tag only in later filings' comparison
    columns; preferring the new tag would date those periods a year late.)"""
    rank = {t: i for i, t in enumerate(tags)}
    return (df.with_columns(_tag_col().alias("__tag"))
            .with_columns(pl.col("__tag").replace_strict(rank, default=len(tags), return_dtype=pl.Int32).alias("__rank"))
            .sort("filed", "__rank", descending=[policy == "latest", False])
            .unique(keys, keep="first", maintain_order=True)
            .drop("__rank"))


def available(expr: pl.Expr) -> pl.Expr:
    return expr + pl.duration(days=1)


def _days() -> pl.Expr:
    return (pl.col("end") - pl.col("start")).dt.total_days()


def periodize(flows: pl.DataFrame, span: tuple[int, int] | None = None, by_tag: bool = True) -> pl.DataFrame:
    """Discrete periods of length `span` (default a quarter) from reported periods, year-to-date
    differences and annual minus the year-to-date before it (Q4 = FY - 9M, H2 = FY - H1).

    Differences are taken within one tag (`__tag`), so a nine-month figure under one tag is
    never subtracted from an annual figure under another. Returns periods per tag.
    """
    span = span or QUARTER
    if not by_tag:  # one series per company whatever the tag (fallback across tag changes)
        flows = flows.with_columns(pl.col("__tag").alias("__real_tag"), pl.lit("*").alias("__tag"))
    d = flows.with_columns(_days().alias("__dur")).filter(pl.col("__dur").is_between(QUARTER[0], YEAR[1]))
    direct = d.filter(pl.col("__dur").is_between(*span)).with_columns(pl.lit("reported").alias("source"))
    g = ["cik", "unit", "__tag", "start"]
    prev = d.sort("cik", "unit", "__tag", "start", "end").with_columns(
        pl.col("end").shift(1).over(g).alias("__pend"),
        pl.col("val").shift(1).over(g).alias("__pval"),
        pl.col("filed").shift(1).over(g).alias("__pfiled"))
    derived = (prev.filter((pl.col("__dur") > span[1]) & pl.col("__pend").is_not_null())
               .with_columns((pl.col("__pend") + pl.duration(days=1)).alias("start"),
                             (pl.col("val") - pl.col("__pval")).alias("val"),
                             pl.max_horizontal("filed", "__pfiled").alias("filed"),
                             pl.lit("derived").alias("source"))
               .filter(_days().is_between(span[0] - 1, span[1])))
    cols = ["cik", "unit", "start", "end", "val", "filed", "__tag", "source"] + (["__real_tag"] if not by_tag else [])
    q = pl.concat([direct.select(cols), derived.select(cols)])
    q = (q.sort("cik", "unit", "__tag", "end", pl.col("source") != "reported")
         .unique(["cik", "unit", "__tag", "end"], keep="first", maintain_order=True)
         .sort("cik", "unit", "end"))
    return q.with_columns(pl.col("__real_tag").alias("__tag")).drop("__real_tag") if not by_tag else q


def quarterize(flows: pl.DataFrame) -> pl.DataFrame:
    return periodize(flows, QUARTER)


def add_ttm(q: pl.DataFrame, n: int = 4) -> pl.DataFrame:
    """Trailing twelve months from n consecutive periods (4 quarters or 2 halves), usable once
    the last of them was filed."""
    g = ["cik", "unit"]
    gap = (pl.col("start") - pl.col("end").shift(1).over(g)).dt.total_days()
    q = q.sort("cik", "unit", "end").with_columns(gap.alias("__gap"))
    ok = pl.lit(True)
    for k in range(n - 1):
        ok = ok & pl.col("__gap").shift(k).over(g).is_between(0, 7)
    span = (pl.col("end") - pl.col("start").shift(n - 1).over(g)).dt.total_days()
    ttm = pl.col("val") + sum(pl.col("val").shift(k).over(g) for k in range(1, n))
    filed = pl.max_horizontal(pl.col("filed"), *[pl.col("filed").shift(k).over(g) for k in range(1, n)])
    valid = ok & span.is_between(*YEAR)
    return q.with_columns(pl.when(valid).then(ttm).otherwise(None).alias("ttm"),
                          pl.when(valid).then(available(filed)).otherwise(None).alias("ttm_available_from")
                          ).drop("__gap")


def _pick_tag(per_tag: pl.DataFrame, tags: tuple[str, ...], nonnegative: bool, policy: str = "first") -> pl.DataFrame:
    """One value per company and period end across tags: known first (or last for "latest"),
    then reported before derived, then tag priority."""
    if nonnegative:
        # A company that restates between its 10-Q and 10-K (e.g. discontinued operations)
        # gives year-to-date figures on two bases; their difference is meaningless.
        per_tag = per_tag.filter((pl.col("source") == "reported") | (pl.col("val") >= 0))
    rank = {t: i for i, t in enumerate(tags)}
    return (per_tag.with_columns(pl.col("__tag").replace_strict(rank, default=len(tags), return_dtype=pl.Int32).alias("__rank"),
                                 (pl.col("source") != "reported").alias("__derived"))
            .sort("filed", "__derived", "__rank", descending=[policy == "latest", False, False])
            .unique(["cik", "end"], keep="first", maintain_order=True).drop("__rank", "__derived"))


def build_series(facts: pl.DataFrame, item: str, concept: Concept, policy: str = "first") -> pl.DataFrame:
    """One item's point-in-time series for every company in `facts`."""
    tags = concept.tags
    f = facts.filter(_tag_col().is_in(list(tags)) & unit_filter(concept.unit))
    if f.is_empty():
        return pl.DataFrame(schema=SERIES_SCHEMA)
    f = choose(f, policy)
    if concept.kind == "instant":
        f = resolve_tags(f.filter(pl.col("start").is_null()), tags, ["cik", "end"], policy)
        out = f.with_columns(pl.lit("instant").alias("period"), pl.lit("reported").alias("source"),
                             pl.lit(None, pl.Float64).alias("ttm"), pl.lit(None, pl.Date).alias("ttm_available_from"))
    elif concept.kind == "per_share":
        f = resolve_tags(f.filter(pl.col("start").is_not_null()), tags, ["cik", "start", "end"], policy)
        dur = _days()
        out = (f.filter(dur.is_between(*QUARTER) | dur.is_between(*YEAR))
               .with_columns(pl.when(dur.is_between(*QUARTER)).then(pl.lit("quarter")).otherwise(pl.lit("annual")).alias("period"),
                             pl.lit("reported").alias("source"), pl.lit(None, pl.Float64).alias("ttm"),
                             pl.lit(None, pl.Date).alias("ttm_available_from")))
    else:
        f = f.filter(pl.col("start").is_not_null()).with_columns(_tag_col().alias("__tag"))
        resolved = resolve_tags(f.drop("__tag"), tags, ["cik", "start", "end"], policy)

        def periods(span: tuple[int, int]) -> pl.DataFrame:
            # Within one tag first (priority order); where that is impossible (a company that
            # changed tags between its 10-Q and 10-K, as many did in 2018), across tags.
            same = _pick_tag(periodize(f, span), tags, concept.nonnegative, policy)
            mixed = _pick_tag(periodize(resolved, span, by_tag=False), tags, concept.nonnegative, policy)
            return pl.concat([same, mixed.join(same.select("cik", "end"), on=["cik", "end"], how="anti")])

        quarters = add_ttm(periods(QUARTER), 4).with_columns(pl.lit("quarter").alias("period"))
        # Half years for semi-annual reporters: only where no quarter ends on the same day (a
        # quarterly filer's six-month year-to-date figure is not a separate period).
        halves = add_ttm(periods(HALF), 2).with_columns(pl.lit("half").alias("period")).join(
            quarters.select("cik", "end"), on=["cik", "end"], how="anti")
        f = resolved
        # Reported fiscal years too (annual-only filers such as many 20-F filers have no quarters).
        annual = (f.filter(_days().is_between(*YEAR))
                  .with_columns(pl.lit("annual").alias("period"), pl.lit("reported").alias("source"),
                                pl.col("val").alias("ttm"), available(pl.col("filed")).alias("ttm_available_from")))
        cols = ["cik", "unit", "start", "end", "val", "filed", "__tag", "source", "ttm", "ttm_available_from", "period"]
        out = pl.concat([quarters.select(cols), halves.select(cols), annual.select(cols)])
    return (out.with_columns(pl.lit(item).alias("item"), pl.col("val").alias("value"),
                             available(pl.col("filed")).alias("available_from"), pl.col("__tag").alias("tag"))
            .select(list(SERIES_SCHEMA)).sort("cik", "end"))


SHARES_SCHEMA = {"cik": pl.String, "end": pl.Date, "shares": pl.Float64, "filed": pl.Date,
                 "available_from": pl.Date, "tag": pl.String}


def build_shares(facts: pl.DataFrame, policy: str = "first") -> pl.DataFrame:
    """Shares outstanding per company: one concept per company, outliers removed."""
    f = facts.filter(_tag_col().is_in(list(SHARE_TAGS)) & (pl.col("unit") == "shares")).with_columns(
        _tag_col().alias("__tag"))
    if f.is_empty():
        return pl.DataFrame(schema=SHARES_SCHEMA)
    # A concept with several values for the same filing and period is per share class
    # (dimension dropped): not usable as a company total.
    amb = (f.group_by("cik", "__tag", "accn", "start", "end").agg(pl.col("val").n_unique().alias("n"))
           .filter(pl.col("n") > 1).select("cik", "__tag").unique())
    f = f.join(amb, on=["cik", "__tag"], how="anti")
    rank = {t: i for i, t in enumerate(SHARE_TAGS)}
    pick = (f.group_by("cik", "__tag").agg(pl.col("accn").n_unique().alias("filings"))
            .with_columns(pl.col("__tag").replace_strict(rank, return_dtype=pl.Int32).alias("__rank"))
            .sort("filings", "__rank", descending=[True, False])
            .unique("cik", keep="first", maintain_order=True).select("cik", "__tag"))
    s = choose(f.join(pick, on=["cik", "__tag"]), policy)
    # Weighted averages come in several durations per period end: keep the shortest.
    s = (s.with_columns(_days().fill_null(0).alias("__dur")).sort("__dur")
         .unique(["cik", "end"], keep="first", maintain_order=True).sort("cik", "end"))
    latest = pl.col("val").last().over("cik")
    ratio = pl.col("val") / latest
    s = s.filter((pl.col("val") > 0) & ratio.is_between(1 / MAX_SHARE_JUMP, MAX_SHARE_JUMP))
    return s.select("cik", "end", pl.col("val").alias("shares"), "filed",
                    available(pl.col("filed")).alias("available_from"), pl.col("__tag").alias("tag"))


def latest_as_of(df: pl.DataFrame, on: date, keys: list[str], stale_days: int = STALE_DAYS) -> pl.DataFrame:
    """The newest row per key that was usable on `on` and is not stale."""
    order = ["end", "available_from"]
    if "period" in df.columns:  # periods ending the same day: prefer quarter, then half, then year
        df = df.with_columns(pl.col("period").replace_strict({"quarter": 2, "half": 1}, default=0,
                                                             return_dtype=pl.Int8).alias("__q"))
        order.append("__q")
    out = (df.filter((pl.col("available_from") <= on) & (pl.col("end") >= on - timedelta(days=stale_days)))
           .sort(order)
           .unique(keys, keep="last", maintain_order=True))
    return out.drop("__q") if "__q" in out.columns else out
