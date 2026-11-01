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

    biggest = lf.filter(pl.col("is_primary")).select(pl.col("issuer_market_cap").max()).collect().item()
    out.append(("no company above $8T", biggest is not None and biggest < 8e12,
                f"max {biggest / 1e12:.2f}T" if biggest is not None else "no market caps"))
    return out
