"""Flat-file datasets: parse Massive's daily CSVs into canonical Parquet tables.

Canonical schemas (prices are unadjusted, timestamps are UTC nanoseconds):

bars (day and minute):
    ticker, date (US/Eastern trading date), timestamp (bar start),
    open, high, low, close, volume, transactions
trades:
    ticker, sip_timestamp, participant_timestamp, trf_timestamp, price, size,
    exchange, conditions (list[int16]), correction, id, sequence_number, tape, trf_id
quotes:
    ticker, sip_timestamp, participant_timestamp, trf_timestamp, bid_price, bid_size,
    bid_exchange, ask_price, ask_size, ask_exchange, conditions, indicators,
    sequence_number, tape
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from .._storage import COMPRESSION, COMPRESSION_LEVEL, ROW_GROUP_SIZE, tmp_path_for

ET_ZONE = "America/New_York"


@dataclass(frozen=True)
class FlatSpec:
    name: str
    s3_dir: str
    kind: str  # "bars" or "ticks"
    description: str


FLAT_DATASETS: dict[str, FlatSpec] = {
    "bars_day": FlatSpec("bars_day", "day_aggs_v1", "bars", "Daily OHLCV bars, all US-listed tickers"),
    "bars_minute": FlatSpec("bars_minute", "minute_aggs_v1", "bars", "1-minute OHLCV bars incl. extended hours"),
    "trades": FlatSpec("trades", "trades_v1", "ticks", "Every SIP trade (about 3 GB/day compressed)"),
    "quotes": FlatSpec("quotes", "quotes_v1", "ticks", "Every SIP NBBO quote (about 9 GB/day compressed)"),
}

BAR_CSV_SCHEMA = {
    "ticker": pl.String,
    "volume": pl.Float64,
    "open": pl.Float64,
    "close": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "window_start": pl.Int64,
    "transactions": pl.Int64,
}

TICK_CSV_TYPES: dict[str, dict[str, pa.DataType]] = {
    "trades": {
        "ticker": pa.string(),
        "conditions": pa.string(),
        "correction": pa.int8(),
        "exchange": pa.int16(),
        "id": pa.string(),
        "participant_timestamp": pa.int64(),
        "price": pa.float64(),
        "sequence_number": pa.int64(),
        "sip_timestamp": pa.int64(),
        "size": pa.float64(),
        "tape": pa.int8(),
        "trf_id": pa.int32(),
        "trf_timestamp": pa.int64(),
    },
    "quotes": {
        "ticker": pa.string(),
        "ask_exchange": pa.int16(),
        "ask_price": pa.float64(),
        "ask_size": pa.float64(),
        "bid_exchange": pa.int16(),
        "bid_price": pa.float64(),
        "bid_size": pa.float64(),
        "conditions": pa.string(),
        "indicators": pa.string(),
        "participant_timestamp": pa.int64(),
        "sequence_number": pa.int64(),
        "sip_timestamp": pa.int64(),
        "tape": pa.int8(),
        "trf_timestamp": pa.int64(),
    },
}


def _ts(col: str) -> pl.Expr:
    return pl.from_epoch(pl.col(col), time_unit="ns").dt.replace_time_zone("UTC").alias(col)


def _ts_or_null(col: str) -> pl.Expr:
    """Massive writes 0 when a timestamp is absent."""
    return pl.when(pl.col(col) > 0).then(_ts(col)).otherwise(None).alias(col)


def _int_list(col: str) -> pl.Expr:
    return pl.when(pl.col(col) != "").then(pl.col(col)).str.split(",").cast(pl.List(pl.Int16)).alias(col)


def read_bars_csv(path: Path) -> pl.DataFrame:
    """Parse a day/minute aggregates CSV (.csv or .csv.gz)."""
    df = pl.read_csv(path, schema_overrides=BAR_CSV_SCHEMA)
    ts = pl.from_epoch(pl.col("window_start"), time_unit="ns").dt.replace_time_zone("UTC")
    return df.select(
        pl.col("ticker"),
        ts.dt.convert_time_zone(ET_ZONE).dt.date().alias("date"),
        ts.alias("timestamp"),
        "open",
        "high",
        "low",
        "close",
        "volume",
        "transactions",
    ).sort(["ticker", "timestamp"])


def _transform_ticks(kind: str, df: pl.DataFrame) -> pl.DataFrame:
    if kind == "trades":
        return df.select(
            "ticker",
            _ts("sip_timestamp"),
            _ts_or_null("participant_timestamp"),
            _ts_or_null("trf_timestamp"),
            "price",
            "size",
            "exchange",
            _int_list("conditions"),
            "correction",
            "id",
            "sequence_number",
            "tape",
            "trf_id",
        )
    return df.select(
        "ticker",
        _ts("sip_timestamp"),
        _ts_or_null("participant_timestamp"),
        _ts_or_null("trf_timestamp"),
        "bid_price",
        "bid_size",
        "bid_exchange",
        "ask_price",
        "ask_size",
        "ask_exchange",
        _int_list("conditions"),
        _int_list("indicators"),
        "sequence_number",
        "tape",
    )


def convert_ticks(kind: str, src: Path, dest: Path, tickers: Iterable[str] | None = None,
                  block_size: int = 64 << 20) -> int:
    """Stream a trades/quotes CSV.gz into Parquet with bounded memory.

    Optionally keeps only `tickers`. Returns the number of rows written.
    """
    types = TICK_CSV_TYPES[kind]
    keep = sorted(set(tickers)) if tickers else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(dest)
    rows = 0
    writer: pq.ParquetWriter | None = None
    empty: pl.DataFrame | None = None
    compression = "gzip" if src.name.endswith(".gz") else None
    try:
        with pa.input_stream(str(src), compression=compression) as stream:
            reader = pacsv.open_csv(
                stream,
                read_options=pacsv.ReadOptions(block_size=block_size),
                convert_options=pacsv.ConvertOptions(column_types=types, include_columns=list(types)),
            )
            for batch in reader:
                df = pl.from_arrow(pa.Table.from_batches([batch]))
                if keep is not None:
                    df = df.filter(pl.col("ticker").is_in(keep))
                df = _transform_ticks(kind, df)
                if df.height == 0:
                    empty = df
                    continue
                table = df.to_arrow()
                if writer is None:
                    writer = pq.ParquetWriter(tmp, table.schema, compression=COMPRESSION,
                                              compression_level=COMPRESSION_LEVEL)
                writer.write_table(table, row_group_size=ROW_GROUP_SIZE)
                rows += df.height
        if writer is None:
            schema_df = empty if empty is not None else _transform_ticks(
                kind, pl.from_arrow(pa.schema(types).empty_table())
            )
            schema_df.head(0).write_parquet(tmp, compression=COMPRESSION)
        else:
            writer.close()
            writer = None
        os.replace(tmp, dest)
    finally:
        if writer is not None:
            writer.close()
        tmp.unlink(missing_ok=True)
    return rows
