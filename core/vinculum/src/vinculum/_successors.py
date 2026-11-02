"""Successors: the same company continuing under a new security or a new CIK.

Two kinds of link, both 1:1 continuations of the shareholders' position:

- kind "security": the ticker moves to a new security within LINK_DAYS (holding-company
  reorganizations such as ExxonMobil in July 2026, BlackRock in October 2024, re-domiciles).
  Accepted when the two securities share a CIK or their names match, and the first close of
  the new security is within PRICE_RATIO of the last close of the old one (a 1:1 exchange;
  mergers at another ratio are not successors). Securities that traded for PHANTOM_DAYS or
  less (placeholder listings on the switch day) are skipped.
- kind "cik": the same security moves to a new CIK (Alphabet, Broadcom, Medtronic), from the
  CIK map.

The new CIK usually has no share count for a few weeks and no trailing-twelve-month flows for
up to a year. Share counts, public floats and fundamentals of the predecessor fill in until the
successor's own are available (see _build and Vinculum.fundamentals).
"""

from __future__ import annotations

from datetime import timedelta

import polars as pl

from ._cikmap import GOOD
from ._names import names_match

LINK_DAYS = 7
PHANTOM_DAYS = 5
PRICE_RATIO = (0.8, 1.25)
DEPTH = 2  # predecessors followed back from a security and CIK

SUCCESSOR_SCHEMA = {
    "security_id": pl.String, "cik": pl.String, "predecessor_id": pl.String, "predecessor_cik": pl.String,
    "link_date": pl.Date, "kind": pl.String, "ticker": pl.String, "price_ratio": pl.Float64,
    "evidence": pl.String,
}


def _good(cik_map: pl.DataFrame) -> pl.DataFrame:
    return cik_map.filter(pl.col("status").is_in(GOOD) & pl.col("cik").is_not_null())


def cik_links(cik_map: pl.DataFrame, equity_ids: list[str]) -> pl.DataFrame:
    """Same security, new CIK: one row per change of CIK between consecutive usable runs."""
    m = (_good(cik_map).filter(pl.col("security_id").is_in(equity_ids))
         .sort("security_id", "valid_from")
         .with_columns(pl.col("cik").shift(1).over("security_id").alias("predecessor_cik"))
         .filter(pl.col("predecessor_cik").is_not_null() & (pl.col("predecessor_cik") != pl.col("cik"))))
    return m.select("security_id", "cik", pl.col("security_id").alias("predecessor_id"), "predecessor_cik",
                    pl.col("valid_from").alias("link_date"), pl.lit("cik").alias("kind"),
                    pl.lit(None, pl.String).alias("ticker"), pl.lit(None, pl.Float64).alias("price_ratio"),
                    pl.lit("same security").alias("evidence"))


def handoff_candidates(secs: pl.DataFrame, equity_ids: list[str]) -> pl.DataFrame:
    """Pairs (old, new) where the old security's last ticker starts trading as the new security
    within LINK_DAYS of the old one's last trade."""
    s = secs.filter(pl.col("security_id").is_in(equity_ids) & pl.col("first_trade").is_not_null()
                    & ((pl.col("last_trade") - pl.col("first_trade")).dt.total_days() > PHANTOM_DAYS))
    old = s.filter(pl.col("last_trade") < s["last_trade"].max()).select(pl.col("security_id").alias("predecessor_id"),
                                             pl.col("last_ticker").alias("ticker"), "last_trade")
    new = s.select("security_id", "first_trade", pl.col("tickers").alias("__t")).explode("__t").rename({"__t": "ticker"})
    gap = (pl.col("first_trade") - pl.col("last_trade")).dt.total_days()
    return (old.join(new, on="ticker").filter((pl.col("security_id") != pl.col("predecessor_id")) & gap.is_between(1, LINK_DAYS))
            .sort("first_trade").unique("predecessor_id", keep="first").unique("security_id", keep="first"))


def security_links(cand: pl.DataFrame, cik_map: pl.DataFrame, attrs: pl.DataFrame, closes: pl.DataFrame) -> pl.DataFrame:
    """Accept handoff candidates with CIK or name evidence and a ~1:1 price (see module doc).

    closes: security_id, date, close (raw) around the candidates' switch dates."""
    if cand.is_empty():
        return pl.DataFrame(schema=SUCCESSOR_SCHEMA)
    good = _good(cik_map).sort("valid_from")
    last_cik = good.group_by("security_id").agg(pl.col("cik").last().alias("predecessor_cik"),
                                                pl.col("edgar_name").drop_nulls().last().alias("__ename_old"))
    first_cik = good.group_by("security_id").agg(pl.col("cik").first().alias("cik"),
                                                 pl.col("edgar_name").drop_nulls().first().alias("__ename_new"))
    names = attrs.group_by("security_id").agg(pl.col("name").drop_nulls().unique().alias("__names"))
    c = (cand.join(last_cik.rename({"security_id": "predecessor_id"}), on="predecessor_id", how="left")
         .join(first_cik, on="security_id", how="left")
         .join(names.rename({"security_id": "predecessor_id", "__names": "__names_old"}), on="predecessor_id", how="left")
         .join(names.rename({"__names": "__names_new"}), on="security_id", how="left"))
    cl = closes.sort("date")
    last_px = (c.select("predecessor_id", "last_trade").join(cl.rename({"security_id": "predecessor_id"}), on="predecessor_id")
               .filter(pl.col("date") <= pl.col("last_trade")).group_by("predecessor_id")
               .agg(pl.col("close").last().alias("__old_px")))
    first_px = (c.select("security_id", "first_trade").join(cl, on="security_id")
                .filter(pl.col("date") >= pl.col("first_trade")).group_by("security_id")
                .agg(pl.col("close").first().alias("__new_px")))
    c = (c.join(last_px, on="predecessor_id", how="left").join(first_px, on="security_id", how="left")
         .with_columns((pl.col("__new_px") / pl.col("__old_px")).alias("price_ratio")))
    rows = []
    for r in c.iter_rows(named=True):
        old_names = [*(r["__names_old"] or []), r["__ename_old"]]
        new_names = [*(r["__names_new"] or []), r["__ename_new"]]
        if r["cik"] is not None and r["cik"] == r["predecessor_cik"]:
            evidence = "same cik"
        elif any(names_match(a, b) for a in old_names if a for b in new_names if b):
            evidence = "name"
        else:
            continue
        ratio = r["price_ratio"]
        if ratio is None or not (PRICE_RATIO[0] <= ratio <= PRICE_RATIO[1]):
            continue
        rows.append({"security_id": r["security_id"], "cik": r["cik"], "predecessor_id": r["predecessor_id"],
                     "predecessor_cik": r["predecessor_cik"], "link_date": r["first_trade"], "kind": "security",
                     "ticker": r["ticker"], "price_ratio": ratio, "evidence": evidence})
    return pl.DataFrame(rows, schema=SUCCESSOR_SCHEMA)


def closes_around(store, cand: pl.DataFrame) -> pl.DataFrame:
    """Raw closes of candidate securities within LINK_DAYS + a week of their switch dates."""
    schema = {"security_id": pl.String, "date": pl.Date, "close": pl.Float64}
    if cand.is_empty():
        return pl.DataFrame(schema=schema)
    pad = timedelta(days=LINK_DAYS + 7)
    ids = list(set(cand["security_id"].to_list()) | set(cand["predecessor_id"].to_list()))
    start, end = cand["last_trade"].min() - pad, cand["first_trade"].max() + pad
    df = (store.bars(start=start, end=end, lazy=True).filter(pl.col("security_id").is_in(ids))
          .select("security_id", "date", "close").collect())
    win = pl.concat([cand.select(pl.col("predecessor_id").alias("security_id"), "last_trade"),
                     cand.select("security_id", pl.col("first_trade").alias("last_trade"))])
    return (df.join(win, on="security_id")
            .filter((pl.col("date") - pl.col("last_trade")).dt.total_days().abs() <= LINK_DAYS + 7)
            .select(list(schema)).unique().cast(schema))


def build_successors(store, secs: pl.DataFrame, cik_map: pl.DataFrame, equity_ids: list[str]) -> pl.DataFrame:
    cand = handoff_candidates(secs, equity_ids)
    sec = security_links(cand, cik_map, store.security_attributes(), closes_around(store, cand))
    out = pl.concat([sec, cik_links(cik_map, equity_ids)]).filter(
        pl.col("cik").is_not_null() & pl.col("predecessor_cik").is_not_null())
    return out.cast(SUCCESSOR_SCHEMA).sort("link_date", "security_id")


def lineage(successors: pl.DataFrame) -> pl.DataFrame:
    """For each (security_id, cik) with predecessors: the chain back, nearest first.
    Columns: security_id, cik, depth (1..DEPTH), predecessor_id, predecessor_cik, link_date."""
    edges = successors.select("security_id", "cik", "predecessor_id", "predecessor_cik", "link_date")
    out = [edges.with_columns(pl.lit(1).alias("depth"))]
    cur = edges
    for d in range(2, DEPTH + 1):
        nxt = (cur.select("security_id", "cik", pl.col("predecessor_id").alias("__s"), pl.col("predecessor_cik").alias("__c"),
                          pl.col("link_date").alias("__l"))
               .join(edges.rename({"security_id": "__s", "cik": "__c"}), on=["__s", "__c"])
               .filter(pl.col("link_date") < pl.col("__l"))
               .select("security_id", "cik", "predecessor_id", "predecessor_cik", "link_date"))
        if nxt.is_empty():
            break
        out.append(nxt.with_columns(pl.lit(d).alias("depth")))
        cur = nxt
    return (pl.concat(out).select("security_id", "cik", "depth", "predecessor_id", "predecessor_cik", "link_date")
            .unique(["security_id", "cik", "depth"], keep="first").sort("security_id", "cik", "depth"))


def inherited_counts(obs: pl.DataFrame, successors: pl.DataFrame, stale_days: int,
                     events: pl.DataFrame | None = None) -> pl.DataFrame:
    """Predecessor share counts known before each link, re-keyed to the successor (security_id,
    cik), so the successor has a market cap until its own first count. `obs`: security_id, cik,
    end, shares, filed, available_from, tag (EDGAR counts per security and CIK). `events`
    (security_id, execution_date, k): a predecessor security's splits between a count and the
    link are applied to it (the successor's own splits come after the link)."""
    if successors.is_empty() or obs.is_empty():
        return obs.clear()
    e = successors.select("security_id", "cik", pl.col("predecessor_id").alias("__ps"),
                          pl.col("predecessor_cik").alias("__pc"), "link_date")
    p = obs.rename({"security_id": "__ps", "cik": "__pc"})
    out = (e.join(p, on=["__ps", "__pc"])
           .filter((pl.col("available_from") <= pl.col("link_date"))
                   & (pl.col("end") >= pl.col("link_date") - pl.duration(days=stale_days)))
           .with_columns(pl.concat_str(pl.lit("successor:"), pl.col("tag")).alias("tag")))
    if events is not None and events.height and out.height:
        out = out.with_row_index("__r")
        k = (out.filter(pl.col("__ps") != pl.col("security_id")).select("__r", "__ps", "end", "link_date")
             .join(events.rename({"security_id": "__ps"}), on="__ps")
             .filter((pl.col("execution_date") > pl.col("end")) & (pl.col("execution_date") <= pl.col("link_date")))
             .group_by("__r").agg(pl.col("k").product().alias("__k")))
        out = (out.join(k, on="__r", how="left")
               .with_columns((pl.col("shares") * pl.col("__k").fill_null(1.0)).alias("shares")).drop("__r", "__k"))
    own = obs.select("security_id", "cik", "end")
    return out.join(own, on=["security_id", "cik", "end"], how="anti").select(obs.columns)
