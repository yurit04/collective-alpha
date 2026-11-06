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
from fractions import Fraction

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
ADR_KEEP = -5.5         # an ADR level plausible as filed is not aligned below this turnover (log10)
SNAP = 0.15             # snap a basis ratio to a round number within 15%
LOOSE = 2.0             # ordinary stock within 2x of Massive's count keeps ratio 1
_MANTISSAS = (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8)
NICE = sorted({m * 10 ** e for m in _MANTISSAS for e in range(-3, 5)}
              | {1 / (m * 10 ** e) for m in _MANTISSAS for e in range(0, 5)})
ADR_TYPES = ("ADRC", "GDR", "NYRS")
PRICE_ONLY_MOVE = 1.1   # a non-round "split" moving the price more than this ...
PRICE_ONLY_TOL = 1.15   # ... while the SEC count across it moves less than this: price-only (a spin-off)


def split_events(splits: pl.DataFrame) -> pl.DataFrame:
    """security_id, execution_date, k (shares multiply by k)."""
    return (splits.filter(pl.col("execution_date").is_not_null() & (pl.col("split_from") > 0)
                          & (pl.col("split_to") > 0))
            .group_by("security_id", "execution_date")
            .agg((pl.col("split_to") / pl.col("split_from")).product().alias("k"))
            .filter((pl.col("k") - 1).abs() > 1e-9)
            .sort("security_id", "execution_date"))


def is_round(k: float) -> bool:
    """A split ratio that is a simple fraction (k or 1/k within 0.2% of n/m, m <= 10)."""
    for x in (k, 1 / k):
        f = Fraction(x).limit_denominator(10)
        if abs(float(f) / x - 1) < 2e-3:
            return True
    return False


def price_only_events(obs: pl.DataFrame, events: pl.DataFrame) -> pl.DataFrame:
    """Spin-offs (and other distributions) that Massive records as splits: a non-round ratio
    moving the price by more than PRICE_ONLY_MOVE while the SEC's count across it (last filed
    before, first dated after, at most 400 days apart) moves by less than PRICE_ONLY_TOL and a
    third of the ratio. They change the price, not the shares (Dell's VMware spin, 1.973; DuPont's
    Qnity spin, 2.39). Without counts on both sides an event stays a split.
    Returns security_id, cik, execution_date, k."""
    cols = {"security_id": pl.String, "cik": pl.String, "execution_date": pl.Date, "k": pl.Float64}
    cand = events.filter((pl.col("k").log().abs() > math.log(PRICE_ONLY_MOVE))
                         & ~pl.col("k").map_elements(is_round, return_dtype=pl.Boolean))
    if cand.is_empty() or obs.is_empty():
        return pl.DataFrame(schema=cols)
    ev = (obs.select("security_id", "cik").unique().join(cand, on="security_id")
          .with_columns((pl.col("execution_date") - pl.duration(days=1)).alias("__before")).sort("__before"))
    o = obs.select("security_id", "cik", "end", "filed", "shares")
    pre = o.sort("filed").rename({"filed": "__pf", "shares": "__pre", "end": "__pend"})
    post = o.sort("end").rename({"end": "__qend", "shares": "__post", "filed": "__qf"})
    ev = (ev.join_asof(pre, left_on="__before", right_on="__pf", by=["security_id", "cik"], strategy="backward",
                       check_sortedness=False)
          .sort("execution_date")
          .join_asof(post, left_on="execution_date", right_on="__qend", by=["security_id", "cik"],
                     strategy="forward", check_sortedness=False))
    seen = (pl.col("__post") / pl.col("__pre")).log().abs()
    usable = (pl.col("__pre").is_not_null() & pl.col("__post").is_not_null()
              & ((pl.col("__qend") - pl.col("__pend")).dt.total_days() <= 400))
    flat = (seen < math.log(PRICE_ONLY_TOL)) & (seen < pl.col("k").log().abs() / 3)
    return (ev.filter(usable & flat).select(*cols).unique(["security_id", "cik", "execution_date"])
            .sort("security_id", "execution_date"))


def price_only_factor(df: pl.DataFrame, spins: pl.DataFrame, col: str = "date", name: str = "price_only") -> pl.DataFrame:
    """Add `name` = product of k over the security's price-only events executed after df[col]:
    the part of Massive's split adjustment they account for (adjusted close x it = the close
    adjusted for real splits only)."""
    sp = spins.select("security_id", "execution_date", "k").unique(["security_id", "execution_date"])
    if sp.is_empty() or df.is_empty():
        return df.with_columns(pl.lit(1.0).alias(name))
    cum = (sp.sort("security_id", "execution_date", descending=[False, True])
           .with_columns(pl.col("k").cum_prod().over("security_id").alias(name))
           .select("security_id", (pl.col("execution_date") - pl.duration(days=1)).alias("__eff"), name)
           .sort("__eff"))
    out = df.sort(col).join_asof(cum, left_on=col, right_on="__eff", by="security_id", strategy="forward",
                                 check_sortedness=False)
    return out.with_columns(pl.col(name).fill_null(1.0)).drop("__eff")


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
    # Only years the security itself traded: a security that replaced another on the same CIK
    # (Biolase from its 2022 reverse split) must not be judged on its predecessor's prices,
    # which are in other units.
    own = liquidity.select("security_id", "year").unique()
    t = (o.select("security_id", "cik", "end", "shares_now").with_columns(pl.col("end").dt.year().alias("year"))
         .join(own, on=["security_id", "year"])
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
    Massive), except across a step of about 1000^n when the level is plausible as filed and
    alignment would put its turnover under ADR_KEEP: a real share issuance, not a unit change
    (LATAM issued 1000x its shares in its 2022 reorganization; aligned, its old ADS read $2.4T
    on $7M a day). Smaller ADR steps are always aligned: turnover before the basis is off by the
    ADS ratio, so it cannot judge them. For stocks the implied daily turnover (dollar volume / market cap) decides:
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
    o = o.join(latest, on=g).join(levels.select(*g, "__lvl", "__tshift", "__mlt"), on=[*g, "__lvl"])
    to_latest = pl.col("__latest") - pl.col("__lvl")
    k = (to_latest / 3).round()
    thousand = ((to_latest - 3 * k).abs() < SLIP_TOL) & (k != 0)
    # Turnover before the basis is off by the ADS ratio, so only a 1000^n step is judged by it.
    issuance = thousand & inside(pl.col("__mlt")) & (pl.col("__mlt") - to_latest < ADR_KEEP)
    o = (o.with_columns(pl.when(is_adr & issuance.fill_null(False)).then(0.0)
                       .when(is_adr).then(to_latest)
                       .otherwise(pl.col("__tshift")).alias("__shift")))
    o = o.with_columns((pl.col("shares_now") * 10 ** pl.col("__shift")).alias("shares_now"),
                       (pl.col("__shift").abs() > 1e-9).alias("unit_fixed"),
                       (pl.col("__lt") - pl.col("__shift")).alias("__lt"))
    if use_t:
        far = ~is_adr & ((pl.col("__lt") < lo - FAR[0]) | (pl.col("__lt") > hi + FAR[1])).fill_null(False)
        o = o.filter(~far)
    return o.drop("__s", "__lvl", "__latest", "__tshift", "__shift", "__lt", "__mlt")


def drop_inherited(o: pl.DataFrame, first_trade: pl.DataFrame, g: list[str]) -> pl.DataFrame:
    """Drop counts that belong to another security on the same CIK: counts dated before a
    security's first trade more than JUMP off its first count dated after it, and counts dated
    after its last trade more than JUMP off its last count dated before it.
    - Before: a security that replaced another on the same CIK at a reverse split (Aradigm's
      NASDAQ listing in June 2014 after a 1-for-40) has no split of its own, so the
      predecessor's pre-split count would be applied to post-split prices.
    - After: LATAM's old ADS stopped trading in June 2020; the CIK's counts from its 2022
      reorganization are 1000x the old ones, and ADR level alignment would scale the old counts
      up to them. Counts after the last trade are never used for that security's market cap.
    first_trade: security_id, first_trade and optionally last_trade."""
    if first_trade is None or first_trade.is_empty():
        return o
    cols = [pl.col("first_trade").alias("__ft")]
    has_last = "last_trade" in first_trade.columns
    cols.append((pl.col("last_trade") if has_last else pl.lit(None, pl.Date)).alias("__lt"))
    o = o.join(first_trade.select("security_id", *cols), on="security_id", how="left")
    first = (o.filter(pl.col("end") >= pl.col("__ft")).sort(*g, "end").group_by(g)
             .agg(pl.col("shares_now").first().alias("__ref")))
    last = (o.filter(pl.col("end") <= pl.col("__lt")).sort(*g, "end").group_by(g)
            .agg(pl.col("shares_now").last().alias("__ref_last")))
    o = o.join(first, on=g, how="left").join(last, on=g, how="left")
    jump = math.log(JUMP)
    pre = ((pl.col("end") < pl.col("__ft")) & pl.col("__ref").is_not_null()
           & ((pl.col("shares_now") / pl.col("__ref")).log().abs() > jump))
    post = ((pl.col("end") > pl.col("__lt")) & pl.col("__ref_last").is_not_null()
            & ((pl.col("shares_now") / pl.col("__ref_last")).log().abs() > jump))
    return o.filter(~(pre.fill_null(False) | post.fill_null(False))).drop("__ft", "__lt", "__ref", "__ref_last")


def adjust(obs: pl.DataFrame, events: pl.DataFrame, adrs: list[str] | None = None,
           liquidity: pl.DataFrame | None = None, multi: list[str] | None = None,
           first_trade: pl.DataFrame | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """obs: security_id, cik, end, filed, available_from, shares, tag (SEC counts per security);
    adrs: security ids whose splits may be ADS ratio changes; liquidity: security_id, year,
    dv_med, close_med (median daily dollar volume and split-adjusted close) for level checks;
    multi: classes sharing their CIK with another listed class (splits tested like ADRs);
    first_trade: security_id, first_trade[, last_trade] (see drop_inherited).
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
    o = drop_inherited(o, first_trade, g).sort(*g, "end")
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
    may be null), classes (equity securities on the same CIK); optional adr (ever a depositary
    receipt, by type or name), adr_now (one today) and name_ratio (ordinary shares per ADS in
    its latest name).
    Returns security_id, basis_ratio, basis (how it was set).

    Without Massive's current count (delisted): ordinary stock and former ADRs that converted
    to ordinary shares assume 1; an ADR takes the ratio its name states ("name"), otherwise it
    has none ("unknown"): a CS-typed ADS valued at one ordinary share per ADS was inflated by
    its ratio (Youku 18x, iSoftStone 10x)."""
    rows = []
    for r in latest.iter_rows(named=True):
        adr = r.get("adr", r["type"] in ADR_TYPES) or r["type"] in ADR_TYPES
        adr_now = r.get("adr_now", r["type"] in ADR_TYPES) or r["type"] in ADR_TYPES
        m, s = r["massive_shares"], r["shares_now"]
        if not m or not s or m <= 0 or s <= 0:
            if adr and adr_now:
                ratio = r.get("name_ratio")
                rows.append((r["security_id"], 1 / ratio, "name") if ratio else (r["security_id"], None, "unknown"))
            else:
                rows.append((r["security_id"], 1.0, "assumed"))
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
