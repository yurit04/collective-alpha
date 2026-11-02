"""FINRA short interest and short sale volume per security.

FINRA keys its data by its own symbols (BRKB in short interest, BRK/B in daily volume). Each
symbol is mapped to the security that held the matching Massive ticker on that date (the
security master's validity windows), never by ticker alone. A symbol that matches two
securities on one date is dropped (none did in the 2017-2026 history). Only equity securities
of the market-cap panel are returned, with their point-in-time size on the same day.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

MATCH_TOLERANCE = "7d"   # a settlement date may fall on a day the stock did not trade


def symbol_windows(master: pl.DataFrame, dataset: str) -> pl.DataFrame:
    """FINRA symbol per Massive ticker validity window: symbol, valid_from, valid_to,
    security_id, ticker."""
    from finra_sensor_array import finra_symbols

    m = master.select("ticker", "valid_from", "valid_to", "security_id")
    return m.with_columns(pl.Series("symbol", finra_symbols(m["ticker"].to_list(), dataset), dtype=pl.String))


def map_symbols(rows: pl.DataFrame, windows: pl.DataFrame, date_col: str) -> pl.DataFrame:
    """Attach security_id and the Massive ticker to FINRA rows (inner: unmapped rows are left out)."""
    j = rows.join(windows, on="symbol").filter(
        (pl.col(date_col) >= pl.col("valid_from"))
        & (pl.col("valid_to").is_null() | (pl.col(date_col) <= pl.col("valid_to"))))
    once = pl.col("security_id").n_unique().over(date_col, "symbol") == 1
    return j.filter(once).drop("valid_from", "valid_to")


def short_interest(finra, store, panel: pl.LazyFrame, start: date | None, end: date | None,
                   date_by: str, ids: list[str] | None) -> pl.DataFrame:
    rows = finra.short_interest(start=start, end=end, date_by=date_by)
    windows = symbol_windows(store.security_master(), "short_interest")
    if ids is not None:
        windows = windows.filter(pl.col("security_id").is_in(ids))
    df = map_symbols(rows, windows, "settlement_date")
    if df.is_empty():
        return _empty_si()
    lo, hi = df["settlement_date"].min(), df["settlement_date"].max()
    p = (panel.filter(pl.col("date").is_between(lo - timedelta(days=10), hi)
                      & pl.col("security_id").is_in(df["security_id"].unique().implode()))
         .select("date", "security_id", "type", "cik", "close", "issuer_market_cap", "issuer_float_market_cap",
                 "is_primary")
         .collect().sort("date"))
    out = (df.sort("settlement_date")
           .join_asof(p, left_on="settlement_date", right_on="date", by="security_id", strategy="backward",
                      tolerance=MATCH_TOLERANCE, check_sortedness=False)
           .filter(pl.col("date").is_not_null()).drop("date"))
    value = pl.col("short_interest") * pl.col("close")
    out = out.with_columns(value.alias("short_value"),
                           (value / pl.col("issuer_market_cap")).alias("si_pct_market_cap"),
                           (value / pl.col("issuer_float_market_cap")).alias("si_pct_float"))
    return out.select(SI_COLUMNS).sort(f"{date_by}_date", "security_id")


SI_COLUMNS = ["settlement_date", "published_date", "security_id", "ticker", "symbol", "market", "type", "cik",
              "is_primary", "short_interest", "change_pct", "avg_daily_volume", "days_to_cover", "close",
              "short_value", "si_pct_market_cap", "si_pct_float"]


def _empty_si() -> pl.DataFrame:
    return pl.DataFrame(schema={c: pl.Float64 for c in SI_COLUMNS}).cast(
        {"settlement_date": pl.Date, "published_date": pl.Date, "security_id": pl.String, "ticker": pl.String,
         "symbol": pl.String, "market": pl.String, "type": pl.String, "cik": pl.String, "is_primary": pl.Boolean,
         "short_interest": pl.Int64, "avg_daily_volume": pl.Int64})


SV_COLUMNS = ["date", "security_id", "ticker", "symbol", "type", "cik", "short_volume", "short_exempt_volume",
              "off_exchange_volume", "volume", "short_ratio", "off_exchange_share", "source"]


def short_volume(finra, store, panel: pl.LazyFrame, start: date | None, end: date | None,
                 ids: list[str] | None) -> pl.DataFrame:
    windows = symbol_windows(store.security_master(), "short_volume")
    if ids is not None:
        windows = windows.filter(pl.col("security_id").is_in(ids))
    lf = finra.short_volume(start=start, end=end, lazy=True)
    if ids is not None:
        lf = lf.filter(pl.col("symbol").is_in(windows["symbol"].unique().implode()))
    df = map_symbols(lf.collect(), windows, "date")
    if df.is_empty():
        return pl.DataFrame(schema={c: pl.String for c in SV_COLUMNS})
    lo, hi = df["date"].min(), df["date"].max()
    p = (panel.filter(pl.col("date").is_between(lo, hi) & pl.col("security_id").is_in(df["security_id"].unique().implode()))
         .select("date", "security_id", "type", "cik", "volume").collect())
    out = df.rename({"total_volume": "off_exchange_volume"}).join(p, on=["date", "security_id"])
    out = out.with_columns(
        (pl.col("short_volume") / pl.col("off_exchange_volume")).alias("short_ratio"),
        pl.when(pl.col("volume") > 0).then(pl.col("off_exchange_volume") / pl.col("volume"))
        .otherwise(None).alias("off_exchange_share"))
    return out.select(SV_COLUMNS).sort("date", "security_id")
