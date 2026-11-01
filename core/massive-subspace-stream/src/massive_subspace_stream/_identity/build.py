"""Build the security master from daily bars, reference snapshots, dated lookups and overrides.

Terms
-----
episode   a ticker's run of trading days; a gap of more than GAP_DAYS starts a new one
anchor    a reference record (snapshot or dated lookup) that falls inside an episode
segment   part of an episode with one identity; the security master has one row per segment
security  segments linked together (renames, gaps); gets one `security_id`

`segment_all` is pure and returns the segments plus the dated lookups it still needs; the
caller fetches those and calls it again until nothing is missing (`final=True` resolves what is
left from prices). `link_and_assign` then links segments into securities.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

GAP_DAYS = 30          # calendar days without a bar that start a new episode
HANDOFF_DAYS = 7       # max calendar days between a ticker's last bar and its successor's first
MAX_JUMP = math.log(1.5)  # larger unexplained open/close jumps mean a different security
ID_FIELDS = ("composite_figi", "share_class_figi", "cik")
FUND_TYPES = {"ETF", "ETN", "ETV", "ETS", "ETD", "FUND", "UNIT"}  # never linked by CIK hand-off
ATTR_FIELDS = ("name", "type", "primary_exchange", "cik", "composite_figi", "share_class_figi")
TEST_SYMBOL = re.compile(r"^(Z[A-Z]ZZ[A-Z]|[A-Z]?TEST[A-Z]?|ZBZX|ZEXIT|ZIEXT|ZXIET|ZVV|CBO|CBX|IGZ)$")
EPOCH = date(1970, 1, 1)


def to_day(d: date) -> int:
    return (d - EPOCH).days


def from_day(n: int) -> date:
    return EPOCH + timedelta(days=int(n))


# ---------------------------------------------------------------------------- bar arrays
@dataclass
class BarIndex:
    """Daily bars as flat numpy arrays, sorted by ticker then date."""
    pos: dict[str, tuple[int, int]]  # ticker -> (first row, last row)
    days: np.ndarray                 # int32 days since 1970-01-01
    open: np.ndarray
    close: np.ndarray
    end_day: int                     # last day in the bars (open windows end here)

    @classmethod
    def from_bars(cls, bars: pl.DataFrame) -> BarIndex:
        bars = bars.select("ticker", "date", "open", "close").sort("ticker", "date")
        spans = (bars.with_row_index("i").group_by("ticker", maintain_order=True)
                 .agg(pl.col("i").min().alias("i0"), pl.col("i").max().alias("i1")))
        pos = {t: (int(a), int(b)) for t, a, b in spans.iter_rows()}
        days = bars["date"].to_physical().to_numpy().astype(np.int32)
        return cls(pos, days, bars["open"].fill_null(np.nan).to_numpy(),
                   bars["close"].fill_null(np.nan).to_numpy(), int(days.max()) if len(days) else 0)

    def jump(self, prev_i: int, next_i: int, split_ratio: float = 1.0) -> float | None:
        """|log| of the open-to-previous-close ratio, after removing a known split ratio."""
        c0 = self.close[prev_i]
        o1 = self.open[next_i]
        if not (o1 > 0):
            o1 = self.close[next_i]
        if not (c0 > 0 and o1 > 0 and split_ratio > 0):
            return None
        return abs(math.log(o1 / c0 / split_ratio))


def episodes(bars: pl.DataFrame) -> pl.DataFrame:
    """ticker, ep, first, last, i0, i1 (row positions in BarIndex arrays)."""
    b = bars.select("ticker", "date").sort("ticker", "date").with_row_index("i")
    gap = (pl.col("date").diff().dt.total_days().over("ticker") > GAP_DAYS).fill_null(True)
    return (b.with_columns(gap.cast(pl.Int32).cum_sum().over("ticker").alias("ep"))
            .group_by("ticker", "ep")
            .agg(pl.col("date").min().alias("first"), pl.col("date").max().alias("last"),
                 pl.col("i").min().cast(pl.Int64).alias("i0"), pl.col("i").max().cast(pl.Int64).alias("i1"))
            .sort("ticker", "ep"))


def split_ratios(splits: pl.DataFrame) -> dict[tuple[str, int], float]:
    """(ticker, execution day) -> expected price ratio (split_from / split_to)."""
    if splits.is_empty():
        return {}
    df = (splits.filter(pl.col("execution_date").is_not_null() & (pl.col("split_to") > 0))
          .group_by("ticker", "execution_date")
          .agg((pl.col("split_from") / pl.col("split_to")).product().alias("r")))
    return {(t, to_day(d)): float(r) for t, d, r in df.iter_rows()}


# ---------------------------------------------------------------------------- anchors
def anchors(snapshots: pl.DataFrame, lookups: pl.DataFrame, eps: pl.DataFrame) -> pl.DataFrame:
    """Every reference record inside an episode: ticker, ep, as_of, src ('s'|'l'), attributes."""
    cols = ["ticker", "as_of", *ATTR_FIELDS]
    parts = []
    if snapshots.height:
        parts.append(snapshots.select(cols).with_columns(pl.lit("s").alias("src")))
    if lookups.height:
        parts.append(lookups.filter(pl.col("found")).select(cols).with_columns(pl.lit("l").alias("src")))
    if not parts:
        return pl.DataFrame(schema={"ticker": pl.String, "ep": pl.Int32, "as_of": pl.Date, "src": pl.String,
                                    **{c: pl.String for c in ATTR_FIELDS}})
    a = pl.concat(parts, how="vertical_relaxed")
    return (a.join(eps.select("ticker", "ep", "first", "last"), on="ticker")
            .filter(pl.col("as_of").is_between(pl.col("first"), pl.col("last")))
            .select("ticker", "ep", "as_of", "src", *ATTR_FIELDS)
            .sort("ticker", "ep", "as_of", "src"))


def _compress(anc: pl.DataFrame) -> pl.DataFrame:
    """Keep only the first and last anchor of each run with identical ids (enough for segmentation)."""
    key = pl.concat_str([pl.col(c).fill_null("~") for c in ID_FIELDS], separator="|")
    g = ("ticker", "ep")
    run = (key != key.shift(1)).over(g).fill_null(True).cast(pl.Int32).cum_sum().over(g)
    a = anc.with_columns(run.alias("run"))
    first = pl.col("run") != pl.col("run").shift(1).over(g)
    last = pl.col("run") != pl.col("run").shift(-1).over(g)
    return a.filter(first.fill_null(True) | last.fill_null(True)).select("ticker", "ep", "as_of", *ID_FIELDS)


def compare(cur: dict[str, Any], rec: dict[str, Any]) -> tuple[str, str | None]:
    """('same', None) or (kind, deciding field) with kind 'figi' or 'cik'."""
    for f in ("composite_figi", "share_class_figi"):
        if cur.get(f) and rec.get(f):
            return ("same", None) if cur[f] == rec[f] else ("figi", f)
    if cur.get("cik") and rec.get("cik"):
        return ("same", None) if cur["cik"] == rec["cik"] else ("cik", "cik")
    return "same", None


# ---------------------------------------------------------------------------- segmentation
@dataclass
class Segmentation:
    segments: pl.DataFrame                      # ticker, ep, i0, i1, first, last, start_how
    needed: set[tuple[str, date]] = field(default_factory=set)
    unresolved_boundaries: int = 0


def segment_all(bars_index: BarIndex, eps: pl.DataFrame, anc: pl.DataFrame,
                lookups: pl.DataFrame, splits: dict[tuple[str, int], float],
                forced_breaks: dict[str, list[date]] | None = None, final: bool = False) -> Segmentation:
    forced_breaks = forced_breaks or {}
    looked: dict[str, set[int]] = defaultdict(set)      # days already asked (found or not)
    not_found: set[tuple[str, int]] = set()
    if lookups.height:
        for t, d, found in lookups.select("ticker", "as_of", "found").iter_rows():
            looked[t].add(to_day(d))
            if not found:
                not_found.add((t, to_day(d)))
    ep_count = eps.group_by("ticker").len()
    multi = set(ep_count.filter(pl.col("len") > 1)["ticker"].to_list())

    by_episode: dict[tuple[str, int], list[tuple]] = defaultdict(list)
    for t, ep, as_of, figi, scfigi, cik in _compress(anc).iter_rows():
        by_episode[(t, ep)].append((to_day(as_of), {"composite_figi": figi, "share_class_figi": scfigi, "cik": cik}))

    days_all = bars_index.days
    rows: list[tuple] = []
    needed: set[tuple[str, date]] = set()
    unresolved = 0

    for t, ep, _first, _last, i0, i1 in eps.iter_rows():
        days = days_all[i0:i1 + 1]
        recs = by_episode.get((t, ep), [])

        # Edge checks: reused symbols, and episodes no snapshot covers.
        if t in multi:
            for d in (int(days[0]), int(days[-1])):
                if d not in looked[t]:
                    needed.add((t, from_day(d)))
        elif not recs:
            d0, d1 = int(days[0]), int(days[-1])
            if d0 not in looked[t]:
                needed.add((t, from_day(d0)))
            elif (t, d0) in not_found and d1 not in looked[t]:
                needed.add((t, from_day(d1)))

        def locate(lo_day: int, hi_day: int, t=t, days=days, i0=i0, i1=i1) -> tuple[str, int]:
            lo = i0 + max(bisect_right(days, lo_day) - 1, 0)
            hi = i0 + min(bisect_left(days, hi_day), len(days) - 1)
            if hi <= lo:
                hi = min(lo + 1, i1)
            if hi - lo <= 1:
                return "at", hi
            cands = [i for i in range(lo + 1, hi) if int(days_all[i]) not in looked[t]]
            if cands and not final:
                return "need", int(days_all[cands[len(cands) // 2]])
            # No lookup can narrow it further: take the largest price jump.
            best, best_i = -1.0, hi
            for i in range(lo + 1, hi + 1):
                j = bars_index.jump(i - 1, i, splits.get((t, int(days_all[i])), 1.0))
                if j is not None and j > best:
                    best, best_i = j, i
            return "jump", best_i

        bounds: dict[int, str] = {i0: "episode"}
        cur: dict[str, Any] | None = None
        evidence: dict[str, int] = {}
        seg_start = i0
        for day, rec in recs:
            if cur is None:
                cur = dict(rec)
                evidence = {f: day for f in ID_FIELDS if rec[f]}
                continue
            kind, fld = compare(cur, rec)
            if kind == "same":
                for f in ID_FIELDS:
                    if rec[f]:
                        cur[f] = rec[f]
                        evidence[f] = day
                continue
            how, at = locate(evidence.get(fld, int(days_all[seg_start])), day)
            if how == "need":
                needed.add((t, from_day(at)))
                at = i0 + min(bisect_left(days, day), len(days) - 1)  # provisional
                how = "pending"
                unresolved += 1
            elif how == "jump":
                unresolved += 1
            if kind == "cik" and how in ("at", "jump") and at > i0:
                j = bars_index.jump(at - 1, at, splits.get((t, int(days_all[at])), 1.0))
                if j is not None and j <= MAX_JUMP:  # same security, new CIK (e.g. a reorganization)
                    cur.update({f: v for f, v in rec.items() if v})
                    evidence.update({f: day for f in ID_FIELDS if rec[f]})
                    continue
            if at > seg_start:
                bounds[at] = "bisect" if how in ("at", "pending") else "jump"
                seg_start = at
            cur = dict(rec)
            evidence = {f: day for f in ID_FIELDS if rec[f]}

        for d in forced_breaks.get(t, []):
            k = to_day(d)
            if int(days[0]) < k <= int(days[-1]):
                bounds[i0 + bisect_left(days, k)] = "override"

        starts = sorted(bounds)
        for n, s in enumerate(starts):
            e = (starts[n + 1] - 1) if n + 1 < len(starts) else i1
            rows.append((t, ep, s, e, from_day(int(days_all[s])), from_day(int(days_all[e])), bounds[s]))

    segs = pl.DataFrame(rows, schema={"ticker": pl.String, "ep": pl.Int32, "i0": pl.Int64, "i1": pl.Int64,
                                      "first": pl.Date, "last": pl.Date, "start_how": pl.String}, orient="row")
    return Segmentation(segs, needed, unresolved)


def segment_attributes(segs: pl.DataFrame, anc: pl.DataFrame) -> pl.DataFrame:
    """Add each segment's latest non-null attributes and its anchor counts."""
    s = segs.with_row_index("seg")
    j = (anc.join(s.select("seg", "ticker", "ep", "first", "last"), on=["ticker", "ep"])
         .filter(pl.col("as_of").is_between(pl.col("first"), pl.col("last")))
         .sort("seg", "as_of"))
    agg = j.group_by("seg").agg(
        *[pl.col(c).drop_nulls().last().alias(c) for c in ATTR_FIELDS],
        pl.col("cik").drop_nulls().first().alias("first_cik"),  # ids use the earliest CIK (stable)
        (pl.col("src") == "s").sum().alias("n_snapshots"),
        (pl.col("src") == "l").sum().alias("n_lookups"),
    )
    out = s.join(agg, on="seg", how="left").with_columns(
        pl.col("n_snapshots").fill_null(0), pl.col("n_lookups").fill_null(0))
    method = (pl.when(pl.col("start_how") == "override").then(pl.lit("override"))
              .when(pl.col("start_how").is_in(["bisect", "jump"])).then(pl.lit("bisect"))
              .when(pl.col("n_snapshots") > 0).then(pl.lit("snapshot"))
              .when(pl.col("n_lookups") > 0).then(pl.lit("lookup"))
              .otherwise(pl.lit("unresolved")))
    return out.with_columns(method.alias("method"),
                            pl.col("ticker").map_elements(lambda t: bool(TEST_SYMBOL.match(t)),
                                                          return_dtype=pl.Boolean).alias("is_test"))


# ---------------------------------------------------------------------------- linking
class _UnionFind:
    def __init__(self, n: int, blocked: list[tuple[int, int]]):
        self.parent = list(range(n))
        self.blocked = blocked

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        for x, y in self.blocked:
            fx, fy = self.find(x), self.find(y)
            if {fx, fy} == {ra, rb}:
                return False
        self.parent[rb] = ra
        return True


def _seg_at(segs: pl.DataFrame, ticker: str, d: date) -> int | None:
    hit = segs.filter((pl.col("ticker") == ticker) & pl.col("first").le(d) & pl.col("last").ge(d))
    return int(hit["seg"][0]) if hit.height else None


def link_and_assign(segs: pl.DataFrame, bars_index: BarIndex, splits: dict[tuple[str, int], float],
                    overrides: pl.DataFrame | None = None) -> tuple[pl.DataFrame, dict[str, int]]:
    """Link segments into securities and assign `security_id`. Returns (segments, link counts)."""
    segs = segs.sort("seg")
    n = segs.height
    blocked: list[tuple[int, int]] = []
    links: list[tuple[int, int, str]] = []
    stats: dict[str, int] = defaultdict(int)
    ov = overrides if overrides is not None else pl.DataFrame()

    if ov.height:
        for row in ov.iter_rows(named=True):
            if row["action"] == "break":
                after = _seg_at(segs, row["ticker"], row["date"])
                if after is not None and after > 0:
                    prev = segs.row(after - 1, named=True)
                    if prev["ticker"] == row["ticker"]:
                        blocked.append((int(prev["seg"]), after))
            elif row["action"] == "link":
                a = _seg_at(segs, row["ticker"], row["date"])
                b = _seg_at(segs, row["other_ticker"], row["other_date"])
                if a is not None and b is not None:
                    links.append((a, b, "override"))
                else:
                    stats["override_links_not_found"] += 1

    rows = segs.select("seg", "ticker", "ep", "i0", "i1", "first", "last", "type", *ID_FIELDS).to_dicts()

    # Equal FIGI: link non-overlapping segments in time order.
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = r["composite_figi"] or (r["share_class_figi"] and "sc:" + r["share_class_figi"])
        if key:
            by_key[key].append(r)
    for group in by_key.values():
        group.sort(key=lambda r: r["first"])
        for a, b in zip(group, group[1:]):
            if b["first"] > a["last"]:
                links.append((a["seg"], b["seg"], "figi"))
            else:
                stats["figi_overlaps"] += 1

    # Same ticker across a gap, when the ids agree.
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)
    for group in by_ticker.values():
        for a, b in zip(group, group[1:]):
            if a["ep"] != b["ep"] and any(a[f] for f in ID_FIELDS) and any(b[f] for f in ID_FIELDS):
                kind, _ = compare(a, b)
                common_figi = any(a[f] and a[f] == b[f] for f in ("composite_figi", "share_class_figi"))
                fund = (a["type"] or "") in FUND_TYPES or (b["type"] or "") in FUND_TYPES
                common_cik = bool(a["cik"]) and a["cik"] == b["cik"] and not fund  # trusts share CIKs
                if kind == "same" and (common_figi or common_cik):
                    links.append((a["seg"], b["seg"], "ticker_gap"))

    # CIK hand-off: one ticker stops, another with the same CIK starts within a week.
    # Every segment ends at an episode end or an identity break, so any of them can hand off;
    # the uniqueness and price checks below keep this safe. Funds are left out: a fund trust
    # shares one CIK across hundreds of ETFs that list and close near the same price, and real
    # fund renames keep their FIGI (linked above).
    eligible = [r for r in rows if r["cik"] and (r["type"] or "") not in FUND_TYPES]
    ends = eligible
    starts_by_cik: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        starts_by_cik[r["cik"]].append(r)
    cand_a: dict[int, list[int]] = defaultdict(list)
    cand_b: dict[int, list[int]] = defaultdict(list)
    for a in ends:
        if bars_index.days[a["i1"]] >= bars_index.end_day:
            continue
        for b in starts_by_cik.get(a["cik"], []):
            gap = (b["first"] - a["last"]).days
            figi_conflict = a["composite_figi"] and b["composite_figi"] and a["composite_figi"] != b["composite_figi"]
            if b["ticker"] != a["ticker"] and 0 < gap <= HANDOFF_DAYS and not figi_conflict:
                cand_a[a["seg"]].append(b["seg"])
                cand_b[b["seg"]].append(a["seg"])
    for a_seg, bs in cand_a.items():
        if len(bs) != 1 or len(cand_b[bs[0]]) != 1:
            stats["handoff_ambiguous"] += 1
            continue
        a, b = rows[a_seg], rows[bs[0]]
        ratio = splits.get((b["ticker"], to_day(b["first"])), 1.0) * splits.get((a["ticker"], to_day(b["first"])), 1.0)
        j = bars_index.jump(a["i1"], b["i0"], ratio)
        if j is not None and j <= MAX_JUMP:
            links.append((a_seg, bs[0], "cik_handoff"))
        else:
            stats["handoff_price_jump"] += 1

    uf = _UnionFind(n, blocked)
    link_from: dict[int, str] = {}
    for a, b, how in sorted(links, key=lambda x: x[2] != "override"):  # overrides first
        if uf.union(a, b):
            later = max(a, b, key=lambda s: rows[s]["first"])
            link_from.setdefault(later, how)
            stats[f"links_{how}"] += 1
        else:
            stats["links_blocked"] += 1

    comp = [uf.find(i) for i in range(n)]
    # A security cannot trade under two tickers at once. Temporary tickers (when-issued "w"/"V"
    # lines around spin-offs, reverse-split "D" tickers) share the parent's FIGI; split the
    # shorter-lived one off into its own security.
    detached: set[int] = set()
    members: dict[int, list[int]] = defaultdict(list)
    for i, c in enumerate(comp):
        members[c].append(i)
    for segs_in in members.values():
        if len(segs_in) < 2:
            continue
        kept: list[int] = []
        for i in sorted(segs_in, key=lambda s: (rows[s]["first"], -(rows[s]["i1"] - rows[s]["i0"]))):
            clash = [k for k in kept if rows[k]["ticker"] != rows[i]["ticker"]
                     and rows[i]["first"] <= rows[k]["last"] and rows[k]["first"] <= rows[i]["last"]]
            if not clash:
                kept.append(i)
                continue
            other = clash[0]
            loser = i if (rows[i]["i1"] - rows[i]["i0"]) <= (rows[other]["i1"] - rows[other]["i0"]) else other
            if loser == other:
                kept.remove(other)
                kept.append(i)
            detached.add(loser)
    for i in detached:
        comp[i] = n + i  # its own component
    stats["detached_concurrent"] = len(detached)
    linked = [None if i in detached else link_from.get(i) for i in range(n)]
    segs = segs.with_columns(pl.Series("component", comp), pl.Series("linked_by", linked),
                             pl.Series("detached", [i in detached for i in range(n)]))
    return _assign_ids(segs), dict(stats)


def _assign_ids(segs: pl.DataFrame) -> pl.DataFrame:
    order = segs.sort("component", "first")
    ids: dict[int, tuple[str, str]] = {}
    for comp, g in order.group_by("component", maintain_order=True):
        g = g.sort("first")
        sid = None
        for col, src in (("composite_figi", "composite_figi"), ("share_class_figi", "share_class_figi")):
            vals = g[col].drop_nulls()
            if len(vals):
                sid = (vals[0], src)
                break
        if sid is None:
            with_cik = g.filter(pl.col("first_cik").is_not_null())
            if with_cik.height:
                sid = (f"CIK:{with_cik['first_cik'][0]}:{with_cik['ticker'][0]}", "cik")
            else:
                sid = (f"SYM:{g['ticker'][0]}:{g['first'][0].isoformat()}", "symbol")
        if g["detached"][0] and sid[1] != "symbol":
            sid = (f"{sid[0]}:{g['ticker'][0]}", sid[1])  # temporary line of a parent security
        ids[comp[0]] = sid
    # Guard against two securities ending up with the same id (e.g. a when-issued line that
    # carries its parent's FIGI): the later one gets its ticker, then its first date, appended.
    seen: dict[str, int] = {}
    starts = order.group_by("component").agg(pl.col("first").min(), pl.col("ticker").first())
    first_map = {c: (f, t) for c, f, t in starts.iter_rows()}
    for comp in sorted(ids, key=lambda c: first_map[c][0]):
        sid, src = ids[comp]
        if sid in seen:
            first, ticker = first_map[comp]
            sid = f"{sid}:{ticker}" if f"{sid}:{ticker}" not in seen else f"{sid}:{ticker}:{first.isoformat()}"
            ids[comp] = (sid, src)
        seen[ids[comp][0]] = comp
    return segs.with_columns(
        pl.col("component").replace_strict({c: v[0] for c, v in ids.items()}, return_dtype=pl.String).alias("security_id"),
        pl.col("component").replace_strict({c: v[1] for c, v in ids.items()}, return_dtype=pl.String).alias("id_source"),
    )


# ---------------------------------------------------------------------------- outputs
def master_table(segs: pl.DataFrame, end_day: int) -> pl.DataFrame:
    end = from_day(end_day)
    return (segs.with_columns(pl.when(pl.col("last") >= end).then(None).otherwise(pl.col("last")).alias("valid_to"))
            .select("ticker", pl.col("first").alias("valid_from"), "valid_to", "security_id", "name", "type",
                    "primary_exchange", "cik", "composite_figi", "share_class_figi", "id_source", "method",
                    "linked_by", "is_test", pl.col("first").alias("first_trade"), pl.col("last").alias("last_trade"))
            .sort("ticker", "valid_from"))


def securities_table(master: pl.DataFrame) -> pl.DataFrame:
    m = master.sort("security_id", "valid_from")
    return m.group_by("security_id", maintain_order=True).agg(
        *[pl.col(c).drop_nulls().last().alias(c) for c in ATTR_FIELDS],
        pl.col("ticker").alias("tickers"),
        pl.col("ticker").last().alias("last_ticker"),
        pl.col("first_trade").min(),
        pl.col("last_trade").max(),
        pl.col("valid_to").is_null().any().alias("active"),
        pl.col("id_source").first(),
        pl.len().alias("segments"),
        pl.col("is_test").any(),
    ).sort("security_id")


def attributes_table(snapshots: pl.DataFrame, master: pl.DataFrame) -> pl.DataFrame:
    """Each month-end snapshot record mapped to its security (point-in-time attributes)."""
    mapped = map_to_security(snapshots.rename({"as_of": "date"}), master)
    return (mapped.filter(pl.col("security_id").is_not_null())
            .select(pl.col("date").alias("as_of"), "security_id", "ticker", *ATTR_FIELDS)
            .sort("security_id", "as_of"))


def map_to_security(df: pl.DataFrame | pl.LazyFrame, master: pl.DataFrame, date_col: str = "date",
                    grace_days: int = 0) -> Any:
    """Attach `security_id` to (ticker, date) rows; rows outside every window get null."""
    lazy = isinstance(df, pl.LazyFrame)
    lf = df.lazy() if not lazy else df
    windows = master.lazy().select("ticker", pl.col("valid_from").alias("__from"), pl.col("valid_to").alias("__to"),
                                   pl.col("security_id")).sort("__from")
    out = (lf.sort(date_col)
           .join_asof(windows, left_on=date_col, right_on="__from", by="ticker", strategy="backward",
                      check_sortedness=False)
           .with_columns(pl.when(pl.col("__to").is_null()
                                 | (pl.col(date_col) <= pl.col("__to") + pl.duration(days=grace_days)))
                         .then(pl.col("security_id")).otherwise(None).alias("security_id"))
           .drop("__from", "__to"))
    return out if lazy else out.collect()
