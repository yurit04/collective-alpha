"""`vinculum check`: known market caps, multi-class handling, top companies, agreement with
Massive's current market caps, coverage, and sanity limits."""

from __future__ import annotations

from datetime import date

import polars as pl

D = date
# (ticker, day, approximate market cap): splits, renames, classes, ADRs and delistings.
KNOWN_CAPS = [
    ("AAPL", D(2012, 9, 19), 656e9, "before the 2014 and 2020 splits"),
    ("AAPL", D(2020, 8, 31), 2.21e12, "day of the 4:1 split"),
    ("NVDA", D(2021, 7, 20), 470e9, "day of the 4:1 split"),
    ("NVDA", D(2024, 6, 10), 2.98e12, "day of the 10:1 split"),
    ("AMZN", D(2022, 6, 6), 1.27e12, "day of the 20:1 split"),
    ("CMG", D(2024, 6, 26), 88e9, "day of the 50:1 split"),
    ("FB", D(2015, 6, 30), 240e9, "Meta under its old ticker"),
    ("GOOGL", D(2020, 12, 31), 1.18e12, "Alphabet, all classes"),
    ("BRK.B", D(2014, 12, 31), 370e9, "Class B, count in Class A equivalents"),
    ("BRK.A", D(2014, 12, 31), 370e9, "Class A"),
    ("TSM", D(2026, 10, 30), 2.1e12, "ADR, 5 ordinary shares per ADS"),
    ("TWTR", D(2021, 12, 31), 34.5e9, "delisted 2022"),
    ("XOM", D(2012, 12, 31), 390e9, "largest company of 2011"),
]
# (ticker, day, public float range, why): from the 10-K cover's market value held by non-affiliates.
FLOAT_KNOWN = [
    ("AAPL", D(2025, 6, 30), 0.95, 1.0, "widely held"),
    ("WMT", D(2025, 6, 30), 0.45, 0.60, "Walton family"),
    ("META", D(2025, 6, 30), 0.80, 0.92, "founder's Class B"),
    ("DELL", D(2025, 6, 30), 0.30, 0.55, "Michael Dell and Silver Lake"),
]
# Penny ADRs whose ADS ratio was wrong, shown as $6-50B companies (now withheld by ADR_PENNY).
JUNK_ADRS = {"BBG00HMJNMV8": "NCNA", "BBG015VQFP30": "SVRE", "BBG00BLVW2R4": "BDRX"}
# Large ADRs that trade mostly abroad (low US dollar volume): must stay ranked.
REAL_ADRS = [("MFG", D(2023, 5, 31)), ("JHX", D(2023, 5, 31))]
TOP_N = 1000
TOP = {
    D(2012, 12, 31): {"AAPL", "XOM", "GOOG", "WMT", "MSFT", "BRK.B", "BRK.A", "GE", "IBM", "CVX", "JNJ"},
    D(2020, 12, 31): {"AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "FB", "TSLA", "BABA", "TSM", "JNJ", "WMT"},
}


def run_checks(v) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    lf = v.scan()
    for t, d, cap, why in KNOWN_CAPS:
        r = lf.filter((pl.col("ticker") == t) & (pl.col("date") == d)).select("market_cap").collect()
        got = r["market_cap"][0] if r.height else None
        ok = got is not None and abs(got / cap - 1) < 0.1
        out.append((f"{t} {d} market cap ~{cap / 1e9:,.0f}B ({why})", ok,
                    f"{got / 1e9:,.1f}B" if got else "missing"))

    g = lf.filter(pl.col("ticker").is_in(["GOOG", "GOOGL"]) & (pl.col("date") == D(2020, 12, 31))).collect()
    ok = g.height == 2 and g["is_primary"].sum() == 1 and g["issuer_market_cap"].n_unique() == 1
    out.append(("GOOG/GOOGL 2020-12-31: one company, one primary class", ok,
                f"primary={g.filter(pl.col('is_primary'))['ticker'].to_list()}"))

    for d, expected in TOP.items():
        top = v.ranks(d).head(10)["ticker"].to_list()
        hits = len(set(top) & expected)
        out.append((f"top 10 on {d} are the known largest companies", hits >= 8, ", ".join(top)))

    b = v.basis()
    cmp = b.filter(pl.col("massive_market_cap").is_not_null())
    if cmp.height:
        last = lf.select(pl.col("date").max()).collect().item()
        mc = lf.filter(pl.col("date") == last).select("security_id", "market_cap").collect()
        j = (cmp.join(mc, on="security_id").filter(pl.col("market_cap").is_not_null())
             .with_columns(((pl.col("market_cap") / pl.col("massive_market_cap") - 1).abs() < 0.1).alias("ok")))
        cal = j.filter(pl.col("basis").is_in(["calibrated", "calibrated_raw", "rescaled", "override"]))
        share = lambda df: df["ok"].mean() or 0.0  # noqa: E731  (None on an empty frame)
        out.append((f"calibrated: market cap within 10% of Massive's ({last})", share(cal) >= 0.95,
                    f"{share(cal):.1%} of {cal.height:,}"))
        out.append(("all: market cap within 10% of Massive's (SEC and Massive counts differ for some)",
                    share(j) >= 0.85, f"{share(j):.1%} of {j.height:,}"))

    m = v.cik_map()
    tasr = m.filter(pl.col("security_id").str.ends_with(":TASR"))
    out.append(("Taser's bogus Massive CIK repaired by name", tasr.height > 0 and tasr["cik"][0] == "0001069183",
                str(tasr["cik"].to_list())))

    yr = pl.col("date").dt.year().alias("year")
    cov = (lf.filter(pl.col("type") == "CS").group_by(yr)
           .agg((pl.col("cik").is_not_null().cast(pl.Float64) * pl.col("dollar_volume")).sum() / pl.col("dollar_volume").sum(),
                (pl.col("market_cap").is_not_null().cast(pl.Float64) * pl.col("dollar_volume")).sum()
                / pl.col("dollar_volume").sum())
           .collect().sort("year"))
    cov.columns = ["year", "cik", "mcap"]
    c10 = cov.filter(pl.col("year") >= 2010)
    out.append(("CIK on >= 97% of common-stock dollar volume (2010+)", c10["cik"].min() >= 0.97,
                f"min {c10['cik'].min():.1%}"))
    c12 = cov.filter(pl.col("year") >= 2012)
    out.append(("market cap on >= 85% of common-stock dollar volume (2012+)", c12["mcap"].min() >= 0.85,
                f"min {c12['mcap'].min():.1%}"))

    # Public float: controlled companies are known; most large companies are nearly all public.
    for t, d, lo, hi, why in FLOAT_KNOWN:
        r = lf.filter((pl.col("ticker") == t) & (pl.col("date") == d)).select("float_fraction").collect()
        got = r["float_fraction"][0] if r.height else None
        out.append((f"{t} {d} public float {lo:.0%}-{hi:.0%} ({why})", got is not None and lo <= got <= hi,
                    f"{got:.1%}" if got is not None else "missing"))
    # Foreign filers (20-F ADRs, 40-F Canadians) report no public float: common stock only.
    fl = (lf.filter(pl.col("is_primary") & (pl.col("type") == "CS") & (pl.col("date").dt.year() >= 2013)
                    & pl.col("issuer_market_cap").is_not_null())
          .group_by(yr).agg((pl.col("float_fraction").is_not_null().cast(pl.Float64) * pl.col("issuer_market_cap")).sum()
                            / pl.col("issuer_market_cap").sum())
          .collect())
    low = fl[fl.columns[1]].min() if fl.height else None
    out.append(("public float on >= 85% of common-stock market cap (2013+)", low is not None and low >= 0.85,
                f"min {low:.1%}" if low is not None else "no market caps"))
    try:
        massive = v.store.dataset("float")
    except (FileNotFoundError, ValueError):
        massive = None
    if massive is not None and massive.height:
        last = lf.select(pl.col("date").max()).collect().item()
        ours = (lf.filter((pl.col("date") == last) & pl.col("float_fraction").is_not_null())
                .select("ticker", "float_fraction").collect())
        m = (massive.sort("effective_date").unique("ticker", keep="last")
             .select("ticker", (pl.col("free_float_percent") / 100).alias("mf")))
        j = ours.join(m, on="ticker")
        med = (j["float_fraction"] - j["mf"]).abs().median() if j.height else None
        out.append(("public float near Massive's free float (median gap < 10 points; Massive counts per class)",
                    med is not None and med < 0.10, f"median gap {med:.1%} over {j.height:,}" if med is not None
                    else "no overlap"))

    out += _adr_checks(v, lf)
    out += _short_checks(v, lf)

    biggest = lf.filter(pl.col("is_primary")).select(pl.col("issuer_market_cap").max()).collect().item()
    out.append(("no company above $8T", biggest is not None and biggest < 8e12,
                f"max {biggest / 1e12:.2f}T" if biggest is not None else "no market caps"))
    return out


def _adr_checks(v, lf) -> list[tuple[str, bool, str]]:
    """Penny ADRs with a wrong ratio stay out of the ranks; real large ADRs stay in."""
    out = []
    ranked = lf.filter(pl.col("is_primary") & pl.col("issuer_market_cap").is_not_null()
                       & (pl.col("type").is_in(["CS", "ADRC"]) | pl.col("type").is_null()))
    # A real company above $5B with an ADR under $2 has billions of ADSs and trades heavily (LYG,
    # ABEV, SAN: tens of millions of dollars a day); one trading under $1M a day is a wrong ratio.
    penny = (ranked.filter((pl.col("type") == "ADRC") & (pl.col("close") < 2) & (pl.col("issuer_market_cap") > 5e9))
             .group_by("security_id").agg(pl.col("ticker").last(), pl.len().alias("days"),
                                         pl.col("dollar_volume").median().alias("dv"),
                                         pl.col("issuer_market_cap").max().alias("cap"))
             .collect())
    bad = penny.filter(pl.col("dv") < 1e6).sort("cap", descending=True)
    b = v.basis()
    if "penny_days" in b.columns:   # built before the penny rule: no stats
        cols = [c for c in ("basis", "penny_days", "penny_turnover", "thin_days") if c in b.columns]
        bad = bad.join(b.select("security_id", *cols), on="security_id", how="left")
    why = lambda r: (f" ({r.get('basis')}, {r.get('penny_days') or 0} penny days at "  # noqa: E731
                     f"{r.get('penny_turnover') or 0:.0e}, {r.get('thin_days') or 0} thin)" if "penny_days" in r else "")
    out.append(("no ranked ADR under $2 above $5B trading under $1M a day", bad.height == 0,
                f"{penny.height} ADRs, {penny['days'].sum()} days under $2 above $5B; thin: "
                + (", ".join(f"{r['ticker']} {r['cap'] / 1e9:,.1f}B{why(r)}" for r in bad.head(5).iter_rows(named=True))
                   or "none")))

    junk = (ranked.filter(pl.col("security_id").is_in(list(JUNK_ADRS)))
            .select("date", "security_id", "issuer_market_cap").collect())
    hits = junk.clear()
    if junk.height:
        days = junk.select("date").unique()
        thr = (ranked.join(days.lazy(), on="date").group_by("date")
               .agg(pl.col("issuer_market_cap").top_k(TOP_N).min().alias("__thr")).collect())
        hits = junk.join(thr, on="date").filter(pl.col("issuer_market_cap") >= pl.col("__thr"))
    out.append((f"penny ADRs with a wrong ratio never in the top {TOP_N} ({', '.join(JUNK_ADRS.values())})",
                hits.height == 0,
                ", ".join(f"{JUNK_ADRS[s]} {n} days" for s, n in hits.group_by("security_id").len().iter_rows())
                or "none"))
    for t, d in REAL_ADRS:
        r = v.ranks(d).filter(pl.col("ticker") == t)
        rank = r["rank"][0] if r.height else None
        out.append((f"{t} {d} in the top {TOP_N} (large ADR, mostly traded abroad)",
                    rank is not None and rank <= TOP_N, f"rank {rank}" if rank is not None else "not ranked"))
    return out


def _short_checks(v, lf) -> list[tuple[str, bool, str]]:
    """FINRA short data mapped to securities (skipped when it is not downloaded)."""
    try:
        gme = v.short_interest("GME", start=D(2021, 1, 15), end=D(2021, 1, 15), date_by="settlement")
        brk = v.short_interest("BRK.B", start=D(2025, 10, 15), end=D(2025, 10, 15), date_by="settlement")
        day = D(2025, 10, 15)
        si = v.short_interest(start=day, end=day, date_by="settlement")
        aapl = v.short_volume("AAPL", start=D(2024, 1, 1), end=D(2024, 12, 31))
    except FileNotFoundError as exc:
        return [("FINRA short data (skipped: not downloaded)", True, str(exc)[:80])]
    out = []
    got = gme["si_pct_float"][0] if gme.height else None
    out.append(("GME 2021-01-15 short interest above its float (the squeeze)", got is not None and got > 1,
                f"{got:.0%} of float" if got is not None else "missing"))
    got = brk["short_interest"][0] if brk.height else None
    out.append(("BRK.B 2025-10-15 short interest found through FINRA's BRKB", got == 11_093_599, str(got)))
    big = (lf.filter((pl.col("date") == day) & pl.col("is_primary") & (pl.col("type") == "CS")
                     & (pl.col("issuer_market_cap") >= 1e9)).select("security_id").collect())
    share = big["security_id"].is_in(si["security_id"].implode()).mean() if big.height else None
    out.append((f"short interest for >= 97% of $1B+ common stocks ({day})", share is not None and share >= 0.97,
                f"{share:.1%} of {big.height:,}" if share is not None else "no stocks"))
    med = aapl["short_ratio"].median() if aapl.height else None
    out.append(("AAPL 2024 off-exchange short ratio 0.3-0.6", med is not None and 0.3 <= med <= 0.6,
                f"median {med:.2f} over {aapl.height} days" if med is not None else "missing"))
    return out
