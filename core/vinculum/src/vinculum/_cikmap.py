"""CIK per security over time, checked against the SEC's own name history.

Massive's point-in-time reference records carry a CIK, but some are wrong (one bogus CIK is
shared by Taser, Quality Systems, Dawson and others in old snapshots; Tenet Healthcare points to
"Hockey Co"). A wrong CIK silently attaches another company's fundamentals, so every
(security, CIK) run is checked:

verified     the SEC knows this CIK under a name matching Massive's (any name in its history)
ticker       names differ, but the SEC lists the security's ticker for this CIK today
unverified   the SEC has no profile for this CIK (funds, very old filers); kept
filled       Massive had no CIK in these snapshots; taken from the same security's next run
repaired     Massive's CIK was missing, rejected or in conflict; found by name in the SEC's
             history (unique match, the company filed periodic reports in that period)
override     from cik_overrides.csv
rejected     wrong (name mismatch, or an unknown CIK shared by unrelated securities): no CIK
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from ._names import looks_equity, similarity, tokens

MATCH = 0.6           # name similarity needed to verify a CIK
REPAIR_MATCH = 0.85   # fuzzy name similarity needed to repair one
SLACK = timedelta(days=365)
EQUITY_TYPES = ("CS", "ADRC", "OS", "GDR", "NYRS")
EPOCH = date(1900, 1, 1)
GOOD = ("verified", "ticker", "unverified", "filled", "repaired", "override")
OVERRIDES = Path(__file__).with_name("cik_overrides.csv")

CIKMAP_SCHEMA = {
    "security_id": pl.String, "valid_from": pl.Date, "valid_to": pl.Date, "cik": pl.String,
    "status": pl.String, "massive_cik": pl.String, "massive_name": pl.String, "edgar_name": pl.String,
    "similarity": pl.Float64,
}


def runs_of(attrs: pl.DataFrame) -> pl.DataFrame:
    """Consecutive month-end snapshots of a security with the same CIK (null included)."""
    a = attrs.sort("security_id", "as_of").with_columns(
        pl.col("cik").fill_null("").alias("__c"))
    a = a.with_columns((pl.col("__c") != pl.col("__c").shift(1).over("security_id")).fill_null(True)
                       .cum_sum().over("security_id").alias("run"))
    return (a.group_by("security_id", "run")
            .agg(pl.col("cik").first(), pl.col("as_of").min().alias("start"), pl.col("as_of").max().alias("end"),
                 pl.col("name").drop_nulls().unique().alias("names"),
                 pl.col("ticker").drop_nulls().unique().alias("tickers"),
                 pl.col("type").drop_nulls().last().alias("type"))
            .sort("security_id", "start"))


class _Edgar:
    """SEC names and profiles, indexed for matching."""

    def __init__(self, profiles: pl.DataFrame, names: pl.DataFrame):
        self.names: dict[str, list[tuple[str, tuple, date | None, date | None]]] = defaultdict(list)
        self.by_key: dict[str, set[str]] = defaultdict(set)
        self.by_first: dict[str, list[tuple[str, tuple]]] = defaultdict(list)
        for cik, name, vf, vt in names.select("cik", "name", "valid_from", "valid_to").iter_rows():
            tok = tokens(name)
            self.names[cik].append((name, tok, vf, vt))
            if tok:
                self.by_key[" ".join(tok)].add(cik)
                self.by_first[tok[0]].append((cik, tok))
        self.profile = {r["cik"]: r for r in profiles.select(
            "cik", "tickers", "first_periodic", "last_periodic").iter_rows(named=True)}

    def best(self, mnames: list[str], cik: str) -> tuple[float | None, str | None]:
        """Best similarity between Massive's names and the SEC's names for a CIK (None: unknown CIK)."""
        if cik not in self.names:
            return None, None
        best, best_name = 0.0, self.names[cik][-1][0]
        for m in mnames:
            mt = tokens(m)
            for name, tok, _, _ in self.names[cik]:
                s = similarity(mt, tok)
                if s > best:
                    best, best_name = s, name
        return best, best_name

    def filed_during(self, cik: str, start: date, end: date) -> bool:
        p = self.profile.get(cik)
        if not p or p["first_periodic"] is None:
            return False
        return p["first_periodic"] <= end + SLACK and p["last_periodic"] >= start - SLACK

    def find(self, mnames: list[str], start: date, end: date) -> str | None:
        """A unique CIK whose SEC name matches during [start, end] and that filed then."""
        found: set[str] = set()
        for m in mnames:
            mt = tokens(m)
            if not mt or (len(mt) == 1 and len(mt[0]) < 4):
                continue
            cands = set(self.by_key.get(" ".join(mt), set()))
            if not cands:
                cands = {c for c, tok in self.by_first.get(mt[0], []) if similarity(mt, tok) >= REPAIR_MATCH}
            for c in cands:
                valid = any(similarity(mt, tok) >= REPAIR_MATCH
                            and (vf is None or vf <= end + SLACK) and (vt is None or vt >= start - SLACK)
                            for _, tok, vf, vt in self.names[c])
                if valid and self.filed_during(c, start, end):
                    found.add(c)
        return found.pop() if len(found) == 1 else None


def _equity(runs: pl.DataFrame) -> pl.Expr:
    return ((pl.col("type").is_null() | pl.col("type").is_in(EQUITY_TYPES))
            & pl.col("names").list.eval(pl.element().map_elements(looks_equity, return_dtype=pl.Boolean)).list.all())


def _conflicts(runs: pl.DataFrame) -> set[tuple[str, int]]:
    """Unknown CIKs held at the same time by stocks whose names do not match each other (fund
    trusts legitimately share one CIK across many ETFs, so only equity is checked)."""
    out: set[tuple[str, int]] = set()
    rows = runs.filter((pl.col("status") == "unverified") & _equity(runs)).select(
        "security_id", "run", "cik", "start", "end", "names").to_dicts()
    by_cik: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cik[r["cik"]].append(r)
    for group in by_cik.values():
        if len({r["security_id"] for r in group}) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a["security_id"] == b["security_id"] or a["end"] < b["start"] or b["end"] < a["start"]:
                    continue
                if not any(similarity(tokens(x), tokens(y)) >= MATCH for x in a["names"] for y in b["names"]):
                    out.update({(a["security_id"], a["run"]), (b["security_id"], b["run"])})
    return out


def load_overrides(path: Path = OVERRIDES) -> pl.DataFrame:
    schema = {"security_id": pl.String, "valid_from": pl.Date, "cik": pl.String, "note": pl.String}
    if not path.exists():
        return pl.DataFrame(schema=schema)
    df = pl.read_csv(path, comment_prefix="#", schema_overrides={"cik": pl.String, "security_id": pl.String},
                     try_parse_dates=True)
    if df.is_empty():
        return pl.DataFrame(schema=schema)
    return df.with_columns(pl.col("cik").str.zfill(10), pl.col("valid_from").cast(pl.Date)).select(list(schema))


def build_cik_map(attrs: pl.DataFrame, profiles: pl.DataFrame, names: pl.DataFrame,
                  overrides: pl.DataFrame | None = None, today: date | None = None) -> pl.DataFrame:
    """One row per security and validity window: the CIK to use and how it was decided."""
    today = today or date.today()
    edgar = _Edgar(profiles, names)
    runs = runs_of(attrs)

    sims, enames, status = [], [], []
    for r in runs.select("cik", "names", "tickers", "end").iter_rows(named=True):
        if r["cik"] is None:
            sims.append(None)
            enames.append(None)
            status.append("missing")
            continue
        sim, ename = edgar.best(r["names"], r["cik"])
        sims.append(sim)
        enames.append(ename)
        if sim is None:
            status.append("unverified")
        elif sim >= MATCH:
            status.append("verified")
        elif (r["end"] >= today - timedelta(days=730)
              and set(r["tickers"]) & set(edgar.profile.get(r["cik"], {}).get("tickers") or [])):
            status.append("ticker")
        else:
            status.append("mismatch")
    runs = runs.with_columns(pl.Series("similarity", sims, pl.Float64), pl.Series("edgar_name", enames, pl.String),
                             pl.Series("status", status, pl.String))
    bad = _conflicts(runs)
    if bad:
        runs = runs.with_columns(
            pl.when(pl.struct("security_id", "run").map_elements(lambda s: (s["security_id"], s["run"]) in bad,
                                                                 return_dtype=pl.Boolean))
            .then(pl.lit("conflict")).otherwise(pl.col("status")).alias("status"))

    # Repair: equity securities whose CIK is missing, wrong or in conflict.
    need = runs.filter(pl.col("status").is_in(["missing", "mismatch", "conflict"]) & _equity(runs))
    fixes: dict[tuple[str, int], str] = {}
    for r in need.select("security_id", "run", "names", "start", "end").iter_rows(named=True):
        c = edgar.find(r["names"], r["start"], r["end"])
        if c:
            fixes[(r["security_id"], r["run"])] = c
    repaired = pl.DataFrame([(s, n, c) for (s, n), c in fixes.items()],
                            schema={"security_id": pl.String, "run": pl.UInt32, "__fix": pl.String}, orient="row")
    runs = (runs.with_columns(pl.col("run").cast(pl.UInt32)).join(repaired, on=["security_id", "run"], how="left")
            .with_columns(pl.col("cik").alias("massive_cik"),
                          pl.when(pl.col("__fix").is_not_null()).then(pl.lit("repaired"))
                          .when(pl.col("status").is_in(["mismatch", "conflict"])).then(pl.lit("rejected"))
                          .otherwise(pl.col("status")).alias("status"))
            .with_columns(pl.when(pl.col("status") == "repaired").then(pl.col("__fix"))
                          .when(pl.col("status") == "rejected").then(None)
                          .otherwise(pl.col("cik")).alias("cik"))
            .drop("__fix"))

    # Snapshots without a CIK inside a security with one: same security, so the same issuer.
    nxt = pl.col("cik").backward_fill().over("security_id", order_by="start")
    prv = pl.col("cik").forward_fill().over("security_id", order_by="start")
    runs = runs.with_columns(
        pl.when(pl.col("status") == "missing").then(pl.coalesce(nxt, prv)).otherwise(pl.col("cik")).alias("__f"))
    runs = runs.with_columns(
        pl.when((pl.col("status") == "missing") & pl.col("__f").is_not_null()).then(pl.lit("filled"))
        .otherwise(pl.col("status")).alias("status"), pl.col("__f").alias("cik")).drop("__f")

    out = runs.select(
        "security_id", pl.col("start").alias("valid_from"), "cik", "status", "massive_cik",
        pl.col("names").list.first().alias("massive_name"), "edgar_name", "similarity")
    ov = overrides if overrides is not None else load_overrides()
    if ov.height:
        ov_rows = ov.select("security_id", "valid_from", "cik", pl.lit("override").alias("status"),
                            pl.lit(None, pl.String).alias("massive_cik"), pl.col("note").alias("massive_name"),
                            pl.lit(None, pl.String).alias("edgar_name"), pl.lit(None, pl.Float64).alias("similarity"))
        out = pl.concat([out.join(ov.select("security_id", "valid_from"), on=["security_id", "valid_from"], how="anti"),
                         ov_rows])
    # Windows: each run applies from its first snapshot until the next run; the first run also
    # covers the days before the first snapshot.
    out = out.sort("security_id", "valid_from").with_columns(
        pl.when(pl.col("valid_from") == pl.col("valid_from").min().over("security_id"))
        .then(pl.lit(EPOCH)).otherwise(pl.col("valid_from")).alias("valid_from"))
    out = out.with_columns((pl.col("valid_from").shift(-1).over("security_id") - pl.duration(days=1)).alias("valid_to"))
    return out.select(list(CIKMAP_SCHEMA))


def cik_on(df: pl.DataFrame | pl.LazyFrame, cik_map: pl.DataFrame, date_col: str = "date"):
    """Attach `cik` and `cik_status` to rows with security_id and a date (good statuses only)."""
    m = (cik_map.filter(pl.col("status").is_in(GOOD) & pl.col("cik").is_not_null())
         .select("security_id", pl.col("valid_from").alias("__vf"), "valid_to", "cik",
                 pl.col("status").alias("cik_status"))
         .sort("__vf"))
    lazy = isinstance(df, pl.LazyFrame)
    lf = df.lazy() if not lazy else df
    out = (lf.sort(date_col).join_asof(m.lazy(), left_on=date_col, right_on="__vf", by="security_id",
                                       strategy="backward", check_sortedness=False)
           .with_columns(pl.when(pl.col("valid_to").is_null() | (pl.col(date_col) <= pl.col("valid_to")))
                         .then(pl.col("cik")).otherwise(None).alias("cik"),
                         pl.when(pl.col("valid_to").is_null() | (pl.col(date_col) <= pl.col("valid_to")))
                         .then(pl.col("cik_status")).otherwise(None).alias("cik_status"))
           .drop("__vf", "valid_to"))
    return out if lazy else out.collect()
