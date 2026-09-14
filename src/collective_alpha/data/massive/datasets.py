"""Dataset specifications: what each flat-file / REST table looks like on disk."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

# ---------------------------------------------------------------- flat files
# Vendor CSV columns are cast explicitly; anything unexpected is kept as-is.

AGG_SCHEMA: dict[str, pl.DataType] = {
    "ticker": pl.Utf8,
    "volume": pl.Float64,  # vendor emits floats for some rows (fractional/odd lots aggregated)
    "open": pl.Float64,
    "close": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "window_start": pl.Int64,  # ns since epoch, UTC
    "transactions": pl.Int64,
}

TRADES_SCHEMA: dict[str, pl.DataType] = {
    "ticker": pl.Utf8,
    "conditions": pl.Utf8,
    "correction": pl.Int32,
    "exchange": pl.Int16,
    "id": pl.Utf8,
    "participant_timestamp": pl.Int64,
    "price": pl.Float64,
    "sequence_number": pl.Int64,
    "sip_timestamp": pl.Int64,
    "size": pl.Int64,
    "tape": pl.Int8,
    "trf_id": pl.Int16,
    "trf_timestamp": pl.Int64,
}

QUOTES_SCHEMA: dict[str, pl.DataType] = {
    "ticker": pl.Utf8,
    "ask_exchange": pl.Int16,
    "ask_price": pl.Float64,
    "ask_size": pl.Int64,
    "bid_exchange": pl.Int16,
    "bid_price": pl.Float64,
    "bid_size": pl.Int64,
    "conditions": pl.Utf8,
    "indicators": pl.Utf8,
    "participant_timestamp": pl.Int64,
    "sequence_number": pl.Int64,
    "sip_timestamp": pl.Int64,
    "tape": pl.Int8,
    "trf_timestamp": pl.Int64,
}


@dataclass(frozen=True)
class FlatFileDataset:
    name: str  # s3 prefix segment, e.g. day_aggs_v1
    table: str  # curated table name
    schema: dict[str, pl.DataType]
    ts_column: str  # epoch-ns column to convert
    sort_by: tuple[str, ...]
    convert_by_default: bool = True


FLATFILE_DATASETS: dict[str, FlatFileDataset] = {
    "day_aggs_v1": FlatFileDataset("day_aggs_v1", "day_aggs", AGG_SCHEMA, "window_start", ("ticker",)),
    "minute_aggs_v1": FlatFileDataset(
        "minute_aggs_v1", "minute_aggs", AGG_SCHEMA, "window_start", ("ticker", "window_start")
    ),
    "trades_v1": FlatFileDataset(
        "trades_v1", "trades", TRADES_SCHEMA, "sip_timestamp", ("ticker", "sip_timestamp"), False
    ),
    "quotes_v1": FlatFileDataset(
        "quotes_v1", "quotes", QUOTES_SCHEMA, "sip_timestamp", ("ticker", "sip_timestamp"), False
    ),
}

# ---------------------------------------------------------------- REST tables


@dataclass(frozen=True)
class RestTable:
    table: str
    path: str
    params: dict = field(default_factory=dict)
    kind: str = "snapshot"  # snapshot | monthly | per_ticker
    date_field: str | None = None  # for monthly tables: the filter field, e.g. published_utc


REST_TABLES: dict[str, RestTable] = {
    # reference (snapshot as-of run date)
    "tickers_active": RestTable(
        "tickers", "/v3/reference/tickers", {"market": "stocks", "active": "true", "limit": 1000}
    ),
    "tickers_inactive": RestTable(
        "tickers", "/v3/reference/tickers", {"market": "stocks", "active": "false", "limit": 1000}
    ),
    "ticker_types": RestTable("ticker_types", "/v3/reference/tickers/types", {"asset_class": "stocks"}),
    "exchanges": RestTable("exchanges", "/v3/reference/exchanges", {"asset_class": "stocks"}),
    "conditions": RestTable("conditions", "/v3/reference/conditions", {"asset_class": "stocks", "limit": 1000}),
    "market_holidays": RestTable("market_holidays", "/v1/marketstatus/upcoming"),
    # corporate actions (snapshot of full history)
    "splits": RestTable("splits", "/v3/reference/splits", {"limit": 1000}),
    "dividends": RestTable("dividends", "/v3/reference/dividends", {"limit": 1000}),
    "ipos": RestTable("ipos", "/vX/reference/ipos", {"limit": 1000}),
    # per-ticker (snapshot, fan-out over the active universe)
    "ticker_details": RestTable("ticker_details", "/v3/reference/tickers/{ticker}", kind="per_ticker"),
    "ticker_events": RestTable("ticker_events", "/vX/reference/tickers/{ticker}/events", kind="per_ticker"),
    # time-partitioned
    "news": RestTable(
        "news",
        "/v2/reference/news",
        {"limit": 1000, "order": "asc", "sort": "published_utc"},
        "monthly",
        "published_utc",
    ),
    "short_interest": RestTable(
        "short_interest", "/stocks/v1/short-interest", {"limit": 50000}, "monthly", "settlement_date"
    ),
    "short_volume": RestTable("short_volume", "/stocks/v1/short-volume", {"limit": 50000}, "monthly", "date"),
    # fundamentals (Advanced tier) – snapshots of full history
    "financials_income": RestTable("financials_income", "/stocks/financials/v1/income-statements", {"limit": 50000}),
    "financials_balance": RestTable("financials_balance", "/stocks/financials/v1/balance-sheets", {"limit": 50000}),
    "financials_cashflow": RestTable(
        "financials_cashflow", "/stocks/financials/v1/cash-flow-statements", {"limit": 50000}
    ),
    "ratios": RestTable("ratios", "/stocks/financials/v1/ratios", {"limit": 50000}),
    "float": RestTable("float", "/stocks/vX/float", {"limit": 50000}),
}

REFERENCE_TABLES = ["tickers_active", "tickers_inactive", "ticker_types", "exchanges", "conditions", "market_holidays"]
CORPORATE_ACTION_TABLES = ["splits", "dividends", "ipos"]
FUNDAMENTAL_TABLES = ["financials_income", "financials_balance", "financials_cashflow", "ratios", "float"]
