"""Follow a company across a security handoff (vinculum successors of kind "security").

Look-back signals (momentum, volatility, growth vs a year earlier) of a company that moved to a
new security (ExxonMobil's holding company in July 2026) would otherwise restart from nothing.
Rows of the predecessor dated before the link are treated as rows of the successor.
"""

from __future__ import annotations

import polars as pl

LINK_SCHEMA = {"predecessor_id": pl.String, "security_id": pl.String, "link_date": pl.Date}


def security_links(successors: pl.DataFrame | None) -> pl.DataFrame:
    """predecessor_id, security_id, link_date of kind-"security" successors, oldest first."""
    if successors is None or successors.is_empty():
        return pl.DataFrame(schema=LINK_SCHEMA)
    return (successors.filter(pl.col("kind") == "security")
            .select(list(LINK_SCHEMA)).cast(LINK_SCHEMA).unique().sort("link_date"))


def predecessors(ids: list[str], links: pl.DataFrame) -> list[str]:
    """`ids` plus every security they succeeded, followed back through chains."""
    out, todo = set(ids), list(ids)
    back = {}
    for p, s in links.select("predecessor_id", "security_id").iter_rows():
        back.setdefault(s, []).append(p)
    while todo:
        for p in back.get(todo.pop(), []):
            if p not in out:
                out.add(p)
                todo.append(p)
    return sorted(out)


def relabel(df: pl.DataFrame | pl.LazyFrame, links: pl.DataFrame, date_col: str = "date"):
    """Give predecessor rows dated before each link the successor's security_id (chains are
    followed in link order: A -> B -> C ends as C)."""
    for p, s, d in links.iter_rows():
        df = df.with_columns(pl.when((pl.col("security_id") == p) & (pl.col(date_col) < d))
                             .then(pl.lit(s)).otherwise(pl.col("security_id")).alias("security_id"))
    return df
