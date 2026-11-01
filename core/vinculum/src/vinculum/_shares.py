"""EDGAR share counts in the units of a traded security.

All counts are first put in *today's* units of the security (`shares_now`), so that

    market cap on day d = split-adjusted close on d x shares_now x basis_ratio

with Massive's split adjustment doing the per-day conversion.

1. Split attribution. For ordinary stock every split applies to the SEC's count. For ADRs a
   split recorded on the ADS may be a ratio change, which moves the ADS price but not the
   ordinary shares the SEC reports; for a class of a multi-class company the count may be in
   another class's units (BRK.B's 2010 split did not change Berkshire's Class A equivalents).
   For those, a split applies only when the count jumps by about the split ratio around it
   (or when there are no counts on both sides).
2. Basis of each count. A count dated before a split but filed after it may already be restated
   for the split (US GAAP restates share counts retroactively); the basis closer to the
   neighbouring counts wins.
3. Isolated values far off their neighbours after adjustment are dropped. Steps of exactly a
   power of 1000 are unit slips (a company reporting in thousands for a while): every count is
   brought to the level most of the company's counts are on. Other lasting jumps that no split
   explains are kept and flagged.
4. Basis ratio (traded units per SEC unit): from Massive's current share count for the
   security. About 1 for ordinary stock; ADRs (TSM: 1 ADS = 5 ordinary shares) and classes
   counted in another class's units (BRK.B: 1,500 per Class A equivalent) get a round ratio.
"""

from __future__ import annotations

import math

import polars as pl

SMALL_SPLIT = 1.25      # splits closer to 1 than this (stock dividends) always apply
SPIKE = 3.0             # isolated adjusted count this far from both neighbours: dropped
JUMP = 3.0              # lasting jump this large with no split: flagged
SLIP_TOL = 0.35         # a step within ~2x of 1000^n is a unit slip (with a real change at the same time)
GROSS = 100.0           # a value this far from both neighbours is dropped even if they disagree
TURNOVER = (10 ** -4.6, 1.0)  # plausible median daily turnover (dollar volume / market cap)
TARGET = -2.5           # typical daily turnover (log10): 0.3%
ABSURD = (4.0, 1.0)     # counts this far (log10) below/above the band are dropped before levelling
FAR = (2.0, 1.0)        # ... and after levelling (a median turnover above 10x a day is impossible)
SNAP = 0.15             # snap a basis ratio to a round number within 15%
LOOSE = 2.0             # ordinary stock within 2x of Massive's count keeps ratio 1
_MANTISSAS = (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8)
NICE = sorted({m * 10 ** e for m in _MANTISSAS for e in range(-3, 5)}
              | {1 / (m * 10 ** e) for m in _MANTISSAS for e in range(0, 5)})
ADR_TYPES = ("ADRC", "GDR", "NYRS")


def split_events(splits: pl.DataFrame) -> pl.DataFrame:
    """security_id, execution_date, k (shares multiply by k)."""
    return (splits.filter(pl.col("execution_date").is_not_null() & (pl.col("split_from") > 0)
                          & (pl.col("split_to") > 0))
            .group_by("security_id", "execution_date")
            .agg((pl.col("split_to") / pl.col("split_from")).product().alias("k"))
            .filter((pl.col("k") - 1).abs() > 1e-9)
            .sort("security_id", "execution_date"))


def _attribution(obs: pl.DataFrame, events: pl.DataFrame, tested: list[str]) -> pl.DataFrame:
    """Which splits apply to the SEC's counts, per (security, cik)."""
    ev = (obs.select("security_id", "cik").unique().join(events, on="security_id")
          .sort("execution_date"))
    if ev.is_empty():
        return ev.with_columns(pl.lit(True).alias("applies"))
    o = obs.select("security_id", "cik", "end", "filed", "shares")
    # Last count filed before the split (certainly on the old basis) ...
    pre = o.sort("filed").rename({"filed": "__pf", "shares": "__pre", "end": "__pend"})
    ev = ev.with_columns((pl.col("execution_date") - pl.duration(days=1)).alias("__before")).join_asof(
        pre, left_on="__before", right_on="__pf", by=["security_id", "cik"], strategy="backward",
        check_sortedness=False)
    # ... and the first count dated after it (certainly on the new basis).
    post = o.sort("end").rename({"end": "__qend", "shares": "__post", "filed": "__qf"})
    ev = ev.join_asof(post, left_on="execution_date", right_on="__qend", by=["security_id", "cik"],
                      strategy="forward", check_sortedness=False)
    seen = (pl.col("__post") / pl.col("__pre")).log()
    usable = (pl.col("__pre").is_not_null() & pl.col("__post").is_not_null()
              & ((pl.col("__qend") - pl.col("__pend")).dt.total_days() <= 400))
    small = pl.col("k").log().abs() < math.log(SMALL_SPLIT)
    applies = (~pl.col("security_id").is_in(tested) | small | ~usable
               | ((seen - pl.col("k").log()).abs() < seen.abs()))
    return ev.select("security_id", "cik", "execution_date", "k", applies.alias("applies"))


def _factor_after(obs: pl.DataFrame, ev: pl.DataFrame, col: str, name: str) -> pl.DataFrame:
    """Product of k over applying splits executed after obs[col]."""
    if ev.is_empty():
        return obs.with_columns(pl.lit(1.0).alias(name))
    cum = (ev.filter(pl.col("applies")).sort("security_id", "cik", "execution_date", descending=[False, False, True])
           .with_columns(pl.col("k").cum_prod().over("security_id", "cik").alias(name))
           .select("security_id", "cik", (pl.col("execution_date") - pl.duration(days=1)).alias("__eff"), name)
           .sort("__eff"))
    out = obs.sort(col).join_asof(cum, left_on=col, right_on="__eff", by=["security_id", "cik"],
                                  strategy="forward", check_sortedness=False)
    return out.with_columns(pl.col(name).fill_null(1.0)).drop("__eff")


def _turnover(o: pl.DataFrame, liquidity: pl.DataFrame) -> pl.DataFrame:
    """log10 implied daily turnover per count (security_id, cik, end).

    Measured against one listed class per company: the one under which most counts look
    plausible (a count in Class A equivalents fits BRK.A's price, not BRK.B's); usually the
    security itself."""
    sib = o.select("cik", pl.col("security_id").alias("__sib")).unique()
    t = (o.select("security_id", "cik", "end", "shares_now").with_columns(pl.col("end").dt.year().alias("year"))
         .join(sib, on="cik").join(liquidity.rename({"security_id": "__sib"}), on=["__sib", "year"])
         .with_columns((pl.col("dv_med") / (pl.col("close_med") * pl.col("shares_now"))).log10().alias("__lt")))
    lo, hi = math.log10(TURNOVER[0]) - 1, math.log10(TURNOVER[1])
    pick = (t.group_by("security_id", "cik", "__sib")
            .agg(pl.col("__lt").is_between(lo, hi).sum().alias("__ok"), pl.col("dv_med").median().alias("__dv"))
            .with_columns((pl.col("__sib") == pl.col("security_id")).alias("__self"))
            .sort("__ok", "__self", "__dv", descending=True).unique(["security_id", "cik"], keep="first")
            .select("security_id", "cik", "__sib"))
    return t.join(pick, on=["security_id", "cik", "__sib"]).select("security_id", "cik", "end", "__lt")


def _unit_slips(o: pl.DataFrame, g: list[str], adrs: list[str], liquidity: pl.DataFrame | None) -> pl.DataFrame:
    """Put every count on a plausible level.

    Counts are split into levels at steps of exactly 1000^n (unit slips) and, for ADRs, at
    unexplained round-ratio steps (the ordinary shares behind an ADS were consolidated or split
    without a split on the ADS). ADR levels are aligned to the latest one (calibrated against
    Massive). For stocks the implied daily turnover (dollar volume / market cap) decides:
    a level moves back to the company's usual level when
    that is plausible, stays when it is plausible itself, and is otherwise scaled by the power
    of 1000 closest to typical turnover; counts still far off are dropped. Without liquidity
    data, levels are aligned to the most common one.
    """
    is_adr = pl.col("security_id").is_in(adrs)
    lo, hi = math.log10(TURNOVER[0]), math.log10(TURNOVER[1])
    use_t = liquidity is not None and liquidity.height > 0
    if use_t:
        o = o.join(_turnover(o, liquidity), on=["security_id", "cik", "end"], how="left")
    else:
        o = o.with_columns(pl.lit(None, pl.Float64).alias("__lt"))
    ratio = pl.col("shares_now") / pl.col("shares_now").shift(1).over(g)
    step = ratio.log10()
    n = (step / 3).round()
    slip = ((step - 3 * n).abs() < SLIP_TOL) & (n != 0)
    adr_step = is_adr & (ratio.log().abs() > math.log(JUMP)) & ratio.map_elements(
        lambda r: snap(r) is not None, return_dtype=pl.Boolean)
    o = o.with_columns(pl.when(slip.fill_null(False) & ~is_adr).then(3 * n)
                       .when(adr_step.fill_null(False)).then(step).otherwise(0.0).alias("__s"))
    o = o.with_columns(pl.col("__s").cum_sum().over(g).alias("__lvl"))
    levels = o.group_by(*g, "__lvl").agg(pl.len().alias("__n"), pl.col("end").max().alias("__last"),
                                         pl.col("__lt").median().alias("__mlt"))
    latest = levels.sort("__last", descending=True).unique(g, keep="first").select(*g, pl.col("__lvl").alias("__latest"))
    common = (levels.sort("__n", "__last", descending=True).unique(g, keep="first")
              .select(*g, pl.col("__lvl").alias("__common")))
    levels = levels.join(common, on=g)
    inside = lambda e: e.is_between(lo, hi)  # noqa: E731
    at_common = pl.col("__mlt") - (pl.col("__common") - pl.col("__lvl"))
    levels = levels.with_columns(
        pl.when(pl.col("__mlt").is_null()).then(pl.col("__common") - pl.col("__lvl"))
        .when((pl.col("__lvl") != pl.col("__common")) & inside(at_common))
        .then(pl.col("__common") - pl.col("__lvl"))              # a unit slip back to the usual level
        .when(inside(pl.col("__mlt"))).then(0.0)                  # plausible as it is
        .otherwise(((pl.col("__mlt") - TARGET) / 3).round() * 3)  # the whole level is off by 1000^n
        .alias("__tshift"))
    o = (o.join(latest, on=g).join(levels.select(*g, "__lvl", "__tshift"), on=[*g, "__lvl"])
         .with_columns(pl.when(is_adr).then(pl.col("__latest") - pl.col("__lvl"))
                       .otherwise(pl.col("__tshift")).alias("__shift")))
    o = o.with_columns((pl.col("shares_now") * 10 ** pl.col("__shift")).alias("shares_now"),
                       (pl.col("__shift").abs() > 1e-9).alias("unit_fixed"),
                       (pl.col("__lt") - pl.col("__shift")).alias("__lt"))
    if use_t:
        far = ~is_adr & ((pl.col("__lt") < lo - FAR[0]) | (pl.col("__lt") > hi + FAR[1])).fill_null(False)
        o = o.filter(~far)
    return o.drop("__s", "__lvl", "__latest", "__tshift", "__shift", "__lt")


def adjust(obs: pl.DataFrame, events: pl.DataFrame, adrs: list[str] | None = None,
           liquidity: pl.DataFrame | None = None, multi: list[str] | None = None
           ) -> tuple[pl.DataFrame, pl.DataFrame]:
    """obs: security_id, cik, end, filed, available_from, shares, tag (SEC counts per security);
    adrs: security ids whose splits may be ADS ratio changes; liquidity: security_id, year,
    dv_med, close_med (median daily dollar volume and split-adjusted close) for level checks;
    multi: classes sharing their CIK with another listed class (splits tested like ADRs).
    Returns (counts with shares_now and flags, split attribution table)."""
    adrs = adrs or []
    ev = _attribution(obs, events, adrs + (multi or []))
    o = _factor_after(_factor_after(obs, ev, "end", "__f_end"), ev, "filed", "__f_filed")
    o = o.with_columns((pl.col("shares") * pl.col("__f_end")).alias("__s_end"),
                       (pl.col("shares") * pl.col("__f_filed")).alias("__s_filed"))
    g = ["security_id", "cik"]
    o = o.sort(*g, "end")
    nb = [pl.col("__s_end").shift(k).over(g) for k in (1, 2, -1, -2)]
    ref = pl.concat_list(nb).list.drop_nulls().list.median()
    ambiguous = (pl.col("__f_end") != pl.col("__f_filed")) & ref.is_not_null()
    pick_filed = ambiguous & ((pl.col("__s_filed") / ref).log().abs() < (pl.col("__s_end") / ref).log().abs())
    o = o.with_columns(pl.when(pick_filed).then(pl.col("__s_filed")).otherwise(pl.col("__s_end")).alias("shares_now"),
                       pl.when(pick_filed).then(pl.lit("filed")).otherwise(pl.lit("end")).alias("basis_date"))
    # Isolated values far off both neighbours (after adjustment): filing errors or unrecorded
    # splits that were reversed. Lasting jumps stay, flagged.
    if liquidity is not None and liquidity.height and adrs is not None:
        # Absurd counts first (12 shares for a listed company), so they cannot make their
        # neighbours look like spikes.
        lo, hi = math.log10(TURNOVER[0]), math.log10(TURNOVER[1])
        lt = o.join(_turnover(o, liquidity), on=["security_id", "cik", "end"], how="left")["__lt"]
        absurd = (~o["security_id"].is_in(adrs) & ((lt < lo - ABSURD[0]) | (lt > hi + ABSURD[1])).fill_null(False))
        o = o.filter(~absurd).sort(*g, "end")
    lv = pl.col("shares_now").log()
    prev, nxt = lv.shift(1).over(g), lv.shift(-1).over(g)
    spike = ((lv - prev).abs() > math.log(SPIKE)) & ((lv - nxt).abs() > math.log(SPIKE)) & ((prev - nxt).abs() < math.log(1.5))
    gross = ((lv - prev).abs() > math.log(GROSS)) & ((lv - nxt).abs() > math.log(GROSS))
    o = o.filter(~(spike.fill_null(False) | gross.fill_null(False))).sort(*g, "end")
    o = _unit_slips(o, g, adrs, liquidity).sort(*g, "end")
    lv = pl.col("shares_now").log()
    jump = (lv - lv.shift(1).over(g)).abs() > math.log(JUMP)
    o = o.with_columns(jump.fill_null(False).alias("jump"))
    return o.drop("__f_end", "__f_filed", "__s_end", "__s_filed"), ev


def snap(r: float) -> float | None:
    """The round number within SNAP of r, if any."""
    best = min(NICE, key=lambda n: abs(math.log(r / n)))
    return best if abs(math.log(r / best)) <= math.log(1 + SNAP) else None


def basis_ratios(latest: pl.DataFrame) -> pl.DataFrame:
    """latest: security_id, type, massive_shares (current, may be null), shares_now (SEC, latest,
    may be null), classes (equity securities on the same CIK).
    Returns security_id, basis_ratio, basis (how it was set)."""
    rows = []
    for r in latest.iter_rows(named=True):
        adr = r["type"] in ADR_TYPES
        m, s = r["massive_shares"], r["shares_now"]
        if not m or not s or m <= 0 or s <= 0:
            rows.append((r["security_id"], None if adr else 1.0, "unknown" if adr else "assumed"))
            continue
        raw = m / s
        nice = snap(raw)
        if nice == 1.0:
            rows.append((r["security_id"], 1.0, "calibrated"))
        elif adr:
            rows.append((r["security_id"], nice or raw, "calibrated" if nice else "calibrated_raw"))
        elif r["classes"] > 1:
            # a class counted in another class's units (BRK.B), or a tracking stock (no ratio)
            rows.append((r["security_id"], nice, "rescaled") if nice else (r["security_id"], None, "mismatch"))
        else:
            # one listed class: the SEC's count is the company's; keep it, note the disagreement
            rows.append((r["security_id"], 1.0, "loose" if 1 / LOOSE <= raw <= LOOSE else "disagree"))
    return pl.DataFrame(rows, schema={"security_id": pl.String, "basis_ratio": pl.Float64, "basis": pl.String},
                        orient="row")
