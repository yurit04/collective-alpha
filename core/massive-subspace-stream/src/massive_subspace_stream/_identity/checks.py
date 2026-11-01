"""Checks on a built security master: coverage, consistency, and known regression cases."""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl

from .build import map_to_security

# (description, ticker, date, other ticker, other date, expect same security?)
REGRESSION_CASES: list[tuple[str, str, str, str, str, bool]] = [
    ("FB before the rename is Meta", "FB", "2015-06-30", "META", "2024-06-28", True),
    ("FB since 2025-06 is not Meta", "FB", "2026-01-30", "META", "2024-06-28", False),
    ("META in 2021 was a different ETF", "META", "2021-09-30", "META", "2024-06-28", False),
    ("TWTR: Tweeter is not Twitter", "TWTR", "2005-06-30", "TWTR", "2015-06-30", False),
    ("SIVB stays one security across its 2005 gap", "SIVB", "2005-06-30", "SIVB", "2010-06-30", True),
    ("GOOG before 2014-04-03 continues as GOOGL", "GOOG", "2010-06-30", "GOOGL", "2020-06-30", True),
    ("GOOG after 2014-04-03 is class C, not GOOGL", "GOOG", "2020-06-30", "GOOGL", "2020-06-30", False),
    ("UTX continues as RTX", "UTX", "2010-06-30", "RTX", "2022-06-30", True),
]
# (description, ticker, expected last trade date)
LAST_TRADE_CASES = [("ATVI delisted after 2023-10-12", "ATVI", "2023-10-12"),
                    ("SIVB stopped trading after 2023-03-09", "SIVB", "2023-03-09"),
                    ("TWTR (Twitter) taken private after 2022-10-27", "TWTR", "2022-10-27"),
                    ("LEH (Lehman) present", "LEH", None)]


def resolve(master: pl.DataFrame, ticker: str, on: date) -> str | None:
    df = map_to_security(pl.DataFrame({"ticker": [ticker], "date": [on]}), master)
    return df["security_id"][0]


def run_checks(master: pl.DataFrame, securities: pl.DataFrame, bars: pl.DataFrame | None = None,
               splits: pl.DataFrame | None = None, dividends: pl.DataFrame | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = master.sort("ticker", "valid_from")
    overlap = m.filter(pl.col("valid_from") <= pl.col("last_trade").shift(1).over("ticker"))
    out["overlapping_windows"] = overlap.height
    if bars is not None:
        mapped = map_to_security(bars.select("ticker", "date"), master)
        out["bar_days"] = mapped.height
        out["bar_days_mapped_pct"] = round(100 * (1 - mapped["security_id"].null_count() / max(mapped.height, 1)), 4)
    out["segments"] = master.height
    out["securities"] = securities.height
    out["id_source"] = dict(securities.group_by("id_source").len().iter_rows())
    out["method"] = dict(master.group_by("method").len().iter_rows())
    out["linked_by"] = {k or "none": v for k, v in master.group_by("linked_by").len().iter_rows()}
    out["unresolved_segments"] = master.filter(pl.col("method") == "unresolved").height
    out["test_symbol_segments"] = master.filter(pl.col("is_test")).height
    multi_sec = master.group_by("ticker").agg(pl.col("security_id").n_unique().alias("n")).filter(pl.col("n") > 1)
    out["tickers_with_several_securities"] = multi_sec.height
    out["securities_with_several_tickers"] = securities.filter(pl.col("tickers").list.unique().list.len() > 1).height
    # Same security trading under two tickers at once is suspicious.
    s = master.sort("security_id", "valid_from")
    out["security_self_overlaps"] = s.filter(
        (pl.col("valid_from") <= pl.col("last_trade").shift(1).over("security_id"))
        & (pl.col("ticker") != pl.col("ticker").shift(1).over("security_id"))).height
    if splits is not None and splits.height:
        out["splits_mapped_by"] = dict(splits.group_by("mapped_by").len().iter_rows())
    if dividends is not None and dividends.height:
        out["dividends"] = dividends.height
        out["dividend_mapped_by"] = dict(dividends.group_by("mapped_by").len().iter_rows())
        out["dividend_factor_source"] = dict(dividends.group_by("factor_source").len().iter_rows())
        both = dividends.filter((pl.col("factor_source") == "price") & pl.col("massive_factor").is_not_null())
        if both.height:
            close = ((pl.col("factor") - pl.col("massive_factor")).abs() < 0.002).mean()
            out["dividend_factor_agrees_with_massive_pct"] = round(100 * both.select(close).item(), 2)

    cases = []
    tickers = set(master["ticker"].unique().to_list())
    for desc, t1, d1, t2, d2, same in REGRESSION_CASES:
        if t1 not in tickers or t2 not in tickers:
            cases.append({"case": desc, "result": "skipped"})
            continue
        a, b = resolve(master, t1, date.fromisoformat(d1)), resolve(master, t2, date.fromisoformat(d2))
        ok = a is not None and b is not None and ((a == b) == same)
        cases.append({"case": desc, "result": "pass" if ok else "FAIL", "detail": f"{a} vs {b}"})
    for desc, t, last in LAST_TRADE_CASES:
        rows = master.filter(pl.col("ticker") == t)
        if not rows.height:
            cases.append({"case": desc, "result": "FAIL", "detail": "ticker missing"})
            continue
        if last is None:
            cases.append({"case": desc, "result": "pass", "detail": rows["security_id"][0]})
            continue
        lasts = [d.isoformat() for d in rows["last_trade"].to_list()]
        cases.append({"case": desc, "result": "pass" if last in lasts else "FAIL", "detail": ", ".join(lasts)})
    out["regression"] = cases
    return out
