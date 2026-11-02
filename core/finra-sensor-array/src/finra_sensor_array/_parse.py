"""FINRA records -> typed tables.

Short interest (Query API, `consolidatedShortInterest`): one row per settlement date and
symbol, exchange-listed and OTC. Daily short sale volume (Reg SHO files): off-exchange
trades reported to the TRFs and the ADF, one file per facility and day. The consolidated
NMS file (CNMS, from 2018-08-01) is the sum of the exchange-listed facility files (checked:
CNMS = FNSQ + FNYX + FNRA + FNQC for every symbol); before it, the facility files are summed.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from ._calendar import si_published

SI_SCHEMA = {
    "settlement_date": pl.Date, "published_date": pl.Date, "symbol": pl.String, "issue_name": pl.String,
    "market": pl.String, "short_interest": pl.Int64, "previous_short_interest": pl.Int64, "change": pl.Int64,
    "change_pct": pl.Float64, "avg_daily_volume": pl.Int64, "days_to_cover": pl.Float64,
    "split_flag": pl.String, "revision_flag": pl.String, "fetched_at": pl.Datetime("us", "UTC"),
}
SI_KEYS = ("settlement_date", "symbol", "market")
_SI_FIELDS = {
    "symbol": "symbolCode", "issue_name": "issueName", "market": "marketClassCode",
    "short_interest": "currentShortPositionQuantity", "previous_short_interest": "previousShortPositionQuantity",
    "change": "changePreviousNumber", "change_pct": "changePercent", "avg_daily_volume": "averageDailyVolumeQuantity",
    "days_to_cover": "daysToCoverQuantity", "split_flag": "stockSplitFlag", "revision_flag": "revisionFlag",
}

SV_SCHEMA = {
    "date": pl.Date, "symbol": pl.String, "short_volume": pl.Int64, "short_exempt_volume": pl.Int64,
    "total_volume": pl.Int64, "markets": pl.String, "source": pl.String,
}
CNMS_START = date(2018, 8, 1)                  # first consolidated NMS file
FACILITIES_BEFORE_CNMS = ("FNSQ", "FNYX", "FNRA")  # Nasdaq TRF, NYSE TRF, ADF (FNQC began 2018-09)
OTC_FACILITY = "FORF"                          # OTC Reporting Facility: OTC (non-NMS) stocks only


def facilities(day: date) -> tuple[str, ...]:
    """Files that make up one day: exchange-listed (consolidated, or its parts) plus OTC."""
    return (("CNMS",) if day >= CNMS_START else FACILITIES_BEFORE_CNMS) + (OTC_FACILITY,)


def short_interest_frame(settlement: date, rows: list[dict[str, Any]],
                         fetched_at: datetime | None = None) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=SI_SCHEMA)
    fetched_at = fetched_at or datetime.now(UTC)
    data = {k: [r.get(src) for r in rows] for k, src in _SI_FIELDS.items()}
    df = pl.DataFrame(data, schema={k: SI_SCHEMA[k] for k in _SI_FIELDS}, strict=False)
    df = df.with_columns(pl.lit(settlement).alias("settlement_date"),
                         pl.lit(si_published(settlement)).alias("published_date"),
                         pl.lit(fetched_at).cast(SI_SCHEMA["fetched_at"]).alias("fetched_at"),
                         pl.col("symbol").str.strip_chars(), pl.col("issue_name").str.strip_chars())
    return df.select(list(SI_SCHEMA)).unique(list(SI_KEYS), keep="first").sort("symbol", "market")


_OLD_HEADER = "Date|Symbol|ShortVolume|TotalVolume|Market"
_NEW_HEADER = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"


def parse_daily_file(text: str) -> pl.DataFrame:
    """One Reg SHO daily file -> date, symbol, short_volume, short_exempt_volume, total_volume,
    market. Old files (before 2011) have no ShortExemptVolume column; the trailer line is dropped.
    Tolerates Windows line endings and a missing header (both occur in 2010 files)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    first = text.split("\n", 1)[0]
    if not first.startswith("Date|"):  # a few files lack the header (FNSQ 2010-11-22)
        text = (_OLD_HEADER if first.count("|") == 4 else _NEW_HEADER) + "\n" + text
    df = pl.read_csv(io.StringIO(text), separator="|", infer_schema=False, null_values=[],
                     truncate_ragged_lines=True)
    if "ShortExemptVolume" not in df.columns:
        df = df.with_columns(pl.lit(None, pl.String).alias("ShortExemptVolume"))
    num = lambda c: pl.col(c).str.strip_chars().cast(pl.Float64, strict=False).round(0).cast(pl.Int64)  # noqa: E731
    return (df.filter(pl.col("Date").str.contains(r"^\d{8}$") & (pl.col("Symbol").str.strip_chars() != ""))
            .select(pl.col("Date").str.to_date("%Y%m%d").alias("date"),
                    pl.col("Symbol").str.strip_chars().alias("symbol"),
                    num("ShortVolume").alias("short_volume"), num("ShortExemptVolume").alias("short_exempt_volume"),
                    num("TotalVolume").alias("total_volume"), pl.col("Market").str.strip_chars().alias("market")))


def combine_facilities(frames: list[pl.DataFrame], source: str) -> pl.DataFrame:
    """Sum facility files of one day per symbol; `markets` lists the reporting markets.
    `source` labels exchange-listed rows; OTC rows (market "O", from the ORF) are "ORF"."""
    frames = [f for f in frames if f.height]
    if not frames:
        return pl.DataFrame(schema=SV_SCHEMA)
    df = pl.concat(frames)
    out = df.group_by("date", "symbol").agg(
        pl.col("short_volume").sum(),
        pl.when(pl.col("short_exempt_volume").is_null().all()).then(None)
        .otherwise(pl.col("short_exempt_volume").sum()).alias("short_exempt_volume"),
        pl.col("total_volume").sum(),
        pl.col("market").str.split(",").list.explode(keep_nulls=False, empty_as_null=False).unique().sort()
        .str.join(",").alias("markets"))
    label = pl.when(pl.col("markets") == "O").then(pl.lit("ORF")).otherwise(pl.lit(source))
    return out.with_columns(label.alias("source")).select(list(SV_SCHEMA)).cast(SV_SCHEMA).sort("symbol")


def finra_symbols(tickers: list[str], dataset: str) -> list[str]:
    """Massive-style tickers -> FINRA symbols. Short interest writes class shares without a
    separator (BRK.B -> BRKB) and preferreds with PR (ABRpD -> ABRPRD); daily volume writes
    classes with a slash (BRK.B -> BRK/B) and preferreds as Massive does (ABRpD). Other
    lowercase suffixes pass through unchanged."""
    out = []
    for t in tickers:
        t = t.strip()
        if re.search(r"[a-oq-z]", t):
            # Massive's other lowercase suffixes (rights r, warrants w, units u, ...) have no
            # known FINRA spelling; upper-casing them would hit another company (NTGr -> NTGR).
            out.append(t)
        elif dataset == "short_interest":
            out.append(t.replace("p", "PR").upper().replace(".", "").replace("/", ""))
        else:
            out.append(t.replace(".", "/"))
    return out
