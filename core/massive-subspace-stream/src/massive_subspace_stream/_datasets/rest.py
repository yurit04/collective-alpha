"""REST datasets: what to fetch, how to store it, and how to normalize it.

Modes
-----
snapshot    Full pull every refresh; the file is replaced (e.g. tickers, splits), with a guard
            against shorter answers (a smaller plan's history limit): stored rows dated before
            the answer's first `span_field` date are kept, and if the answer has more than 2%
            fewer rows than stored for the same span, stored rows missing from it (by `keys`)
            are kept too. Without `keys`, such an answer is refused.
accumulate  Full pull every refresh; rows are merged into one file by `keys`, so
            history builds up over time (e.g. float, upcoming holidays).
timeseries  Incremental pull from the last seen date (minus `overlap_days`);
            rows are merged into per-year files by `keys`.
details     One request per active ticker (of DETAIL_TYPES), once per day; rows dated
            `as_of` are merged into per-year files, so a daily history builds up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import orjson
import polars as pl


@dataclass(frozen=True)
class RestSpec:
    name: str
    endpoint: str
    path: str
    mode: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)
    param_sets: tuple[dict[str, Any], ...] = ({},)
    limit: int | None = 1000
    date_field: str | None = None
    filter_param: str | None = None
    keys: tuple[str, ...] = ()
    sort: tuple[str, ...] = ()
    overlap_days: int = 7
    span_field: str | None = None  # snapshot: the date the answer's history is measured by


def _fin(name: str, endpoint: str, description: str) -> RestSpec:
    return RestSpec(
        name=name,
        endpoint=endpoint,
        path=f"fundamentals/{name}",
        mode="timeseries",
        description=description,
        limit=50000,
        date_field="filing_date",
        filter_param="filing_date.gte",
        keys=("cik", "period_end", "timeframe", "fiscal_year", "fiscal_quarter", "filing_date"),
        sort=("cik", "period_end", "filing_date"),
        overlap_days=30,
    )


# Ticker types whose details (share counts, market cap) are snapshotted daily.
DETAIL_TYPES = ("CS", "ADRC", "OS")
DETAIL_FIELDS = ("ticker", "name", "type", "cik", "composite_figi", "share_class_figi", "market_cap",
                 "share_class_shares_outstanding", "weighted_shares_outstanding", "sic_code",
                 "sic_description", "total_employees", "currency_name", "list_date")

REST_DATASETS: dict[str, RestSpec] = {
    s.name: s
    for s in [
        RestSpec(
            "tickers", "/v3/reference/tickers", "reference/tickers", "snapshot",
            "All stock tickers, active and delisted, with FIGI/CIK identifiers",
            params={"market": "stocks"}, param_sets=({"active": "true"}, {"active": "false"}),
            keys=("ticker",), sort=("ticker",),
        ),
        RestSpec("ticker_details", "/v3/reference/tickers/{ticker}", "reference/ticker_details", "details",
                 "Daily snapshot of share counts, market cap and SIC per active stock/ADR "
                 "(history builds up from the first run)",
                 date_field="as_of", keys=("ticker", "as_of"), sort=("as_of", "ticker")),
        RestSpec("ticker_types", "/v3/reference/tickers/types", "reference/ticker_types", "snapshot",
                 "Ticker type codes (CS, ETF, ADRC, ...)", params={"asset_class": "stocks", "locale": "us"},
                 limit=None),
        RestSpec("exchanges", "/v3/reference/exchanges", "reference/exchanges", "snapshot",
                 "Exchange and TRF ids used in trades/quotes", params={"asset_class": "stocks"}, limit=None),
        RestSpec("conditions", "/v3/reference/conditions", "reference/conditions", "snapshot",
                 "Trade/quote condition codes with names and how each affects bars (update_rules)",
                 params={"asset_class": "stocks"}, limit=1000, sort=("data_types", "id")),
        RestSpec("market_holidays", "/v1/marketstatus/upcoming", "reference/market_holidays", "accumulate",
                 "Upcoming market holidays and early closes (history accumulates)", limit=None,
                 keys=("date", "exchange"), sort=("date", "exchange")),
        RestSpec("splits", "/stocks/v1/splits", "corporate_actions/splits", "snapshot",
                 "Stock splits with cumulative adjustment factors", limit=5000,
                 keys=("id",), span_field="execution_date", sort=("ticker", "execution_date")),
        RestSpec("dividends", "/stocks/v1/dividends", "corporate_actions/dividends", "snapshot",
                 "Cash dividends with cumulative adjustment factors", limit=5000,
                 keys=("id",), span_field="ex_dividend_date", sort=("ticker", "ex_dividend_date")),
        RestSpec("ipos", "/vX/reference/ipos", "corporate_actions/ipos", "snapshot",
                 "IPO calendar and history", keys=("ticker", "announced_date", "listing_date"),
                 span_field="listing_date", sort=("listing_date", "ticker")),
        _fin("income_statements", "/stocks/financials/v1/income-statements", "Income statements (quarterly/annual/TTM)"),
        _fin("balance_sheets", "/stocks/financials/v1/balance-sheets", "Balance sheets"),
        _fin("cash_flow_statements", "/stocks/financials/v1/cash-flow-statements", "Cash flow statements"),
        RestSpec("ratios", "/stocks/financials/v1/ratios", "fundamentals/ratios", "timeseries",
                 "Daily valuation and quality ratios (a history builds up from each refresh)",
                 limit=50000, date_field="date", keys=("ticker", "date"), sort=("date", "ticker")),
        RestSpec("float", "/stocks/vX/float", "fundamentals/float", "accumulate",
                 "Free float (history builds up from each refresh)", limit=5000,
                 keys=("ticker", "effective_date"), sort=("ticker", "effective_date")),
        RestSpec("short_interest", "/stocks/v1/short-interest", "short_interest", "timeseries",
                 "Bi-monthly FINRA short interest (from 2017)", limit=50000, date_field="settlement_date",
                 filter_param="settlement_date.gte", keys=("ticker", "settlement_date"),
                 sort=("settlement_date", "ticker"), overlap_days=31),
        RestSpec("short_volume", "/stocks/v1/short-volume", "short_volume", "timeseries",
                 "Daily off-exchange short volume (from 2024)", limit=50000, date_field="date",
                 filter_param="date.gte", keys=("ticker", "date"), sort=("date", "ticker")),
        RestSpec("news", "/v2/reference/news", "news", "timeseries",
                 "News articles with tickers and sentiment insights", date_field="published_utc",
                 filter_param="published_utc.gte", keys=("id",), sort=("published_utc",), overlap_days=3),
    ]
}

_DATE_NAMES = {"date", "period_end"}
_DATETIME_NAMES = {"last_updated"}


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in row.items():
        if isinstance(value, dict):
            if key == "publisher":
                out["publisher_name"] = value.get("name")
            out[key] = orjson.dumps(value).decode()
        elif isinstance(value, list) and value and isinstance(value[0], (dict, list)):
            out[key] = orjson.dumps(value).decode()
        else:
            out[key] = value
    return out


def to_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    """Turn API results into a typed DataFrame (dates parsed, nested values as JSON text)."""
    if not rows:
        return pl.DataFrame()
    df = pl.from_dicts([_flatten_row(r) for r in rows], infer_schema_length=None)
    exprs = []
    for name, dtype in df.schema.items():
        if dtype != pl.String:
            continue
        if name in _DATE_NAMES or name.endswith("_date"):
            exprs.append(pl.col(name).str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False))
        elif name.endswith("_utc") or name in _DATETIME_NAMES:
            exprs.append(pl.col(name).str.to_datetime(time_zone="UTC", strict=False, time_unit="us"))
    return df.with_columns(exprs) if exprs else df


def year_expr(date_field: str) -> pl.Expr:
    return pl.col(date_field).dt.year().alias("__year")
