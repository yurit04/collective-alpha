"""Company-facts JSON -> one long table of numeric facts, and the SEC daily index format."""

from __future__ import annotations

import os
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import orjson
import polars as pl

from ._layout import BUCKETS

FACT_SCHEMA = {
    "cik": pl.String,        # zero-padded to 10 digits, like Massive's reference data
    "taxonomy": pl.String,   # us-gaap, dei, ifrs-full, srt, ...
    "tag": pl.String,
    "unit": pl.String,       # USD, shares, USD/shares, EUR, pure, ...
    "start": pl.Date,        # null for instants (balances, share counts)
    "end": pl.Date,
    "val": pl.Float64,
    "accn": pl.String,       # accession number of the filing
    "fy": pl.Int32,
    "fp": pl.String,         # FY, Q1, Q2, Q3
    "form": pl.String,       # 10-K, 10-Q, 20-F, 10-K/A, ...
    "filed": pl.Date,
    "frame": pl.String,      # SEC's calendar frame label (e.g. CY2023Q1), when assigned
}
SORT = ["cik", "taxonomy", "tag", "unit", "end", "start", "filed", "accn"]

# Forms whose filing can change company facts (financial reports and their amendments).
FINANCIAL_FORMS = {"10-K", "10-Q", "10-KT", "10-QT", "20-F", "40-F", "6-K", "8-K",
                   "10-K/A", "10-Q/A", "10-KT/A", "20-F/A", "40-F/A", "6-K/A", "8-K/A"}


def pad_cik(cik: Any) -> str:
    return f"{int(cik):010d}"


def parse_companyfacts(doc: dict[str, Any]) -> pl.DataFrame:
    """Flatten one company-facts document into FACT_SCHEMA rows (numeric facts only)."""
    cik = pad_cik(doc.get("cik", 0))
    cols: dict[str, list] = {k: [] for k in FACT_SCHEMA}
    for taxonomy, concepts in (doc.get("facts") or {}).items():
        for tag, body in concepts.items():
            for unit, rows in (body.get("units") or {}).items():
                for r in rows:
                    val = r.get("val")
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        continue
                    cols["cik"].append(cik)
                    cols["taxonomy"].append(taxonomy)
                    cols["tag"].append(tag)
                    cols["unit"].append(unit)
                    cols["start"].append(r.get("start"))
                    cols["end"].append(r.get("end"))
                    cols["val"].append(float(val))
                    cols["accn"].append(r.get("accn"))
                    cols["fy"].append(r.get("fy"))
                    cols["fp"].append(r.get("fp"))
                    cols["form"].append(r.get("form"))
                    cols["filed"].append(r.get("filed"))
                    cols["frame"].append(r.get("frame"))
    df = pl.DataFrame(cols, schema={**FACT_SCHEMA, "start": pl.String, "end": pl.String, "filed": pl.String})
    return df.with_columns(
        pl.col("start").str.to_date("%Y-%m-%d", strict=False),
        pl.col("end").str.to_date("%Y-%m-%d", strict=False),
        pl.col("filed").str.to_date("%Y-%m-%d", strict=False),
    ).filter(pl.col("end").is_not_null() & pl.col("filed").is_not_null())


def company_row(doc: dict[str, Any], facts: pl.DataFrame) -> dict[str, Any]:
    return {"cik": pad_cik(doc.get("cik", 0)), "name": doc.get("entityName"), "facts": facts.height,
            "last_filed": facts["filed"].max() if facts.height else None}


COMPANY_SCHEMA = {"cik": pl.String, "name": pl.String, "facts": pl.Int64, "last_filed": pl.Date,
                  "fetched_at": pl.Datetime("us", "UTC")}


def parse_chunk(zip_path: str, members: list[str], out_dir: str, chunk_id: int) -> tuple[int, list[dict]]:
    """Worker (runs in a subprocess): parse some zip members, write one file per CIK bucket."""
    frames: list[pl.DataFrame] = []
    companies: list[dict] = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in members:
            try:
                doc = orjson.loads(zf.read(name))
            except (orjson.JSONDecodeError, KeyError):
                continue
            df = parse_companyfacts(doc)
            companies.append(company_row(doc, df))
            if df.height:
                frames.append(df)
    rows = 0
    if frames:
        all_facts = pl.concat(frames).with_columns(
            (pl.col("cik").cast(pl.Int64) % BUCKETS).alias("__bucket"))
        for (bucket,), part in all_facts.partition_by("__bucket", as_dict=True).items():
            path = Path(out_dir) / f"bucket={bucket:02d}" / f"chunk-{chunk_id:05d}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            part.drop("__bucket").write_parquet(path, compression="zstd", compression_level=3)
            rows += part.height
    return rows, companies


def parse_master_index(text: str) -> pl.DataFrame:
    """SEC daily master index: CIK|Company Name|Form Type|Date Filed|Filename."""
    rows = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or not parts[0].strip().isdigit():
            continue
        cik, name, form, filed, filename = (p.strip() for p in parts)
        rows.append((pad_cik(cik), name, form, filed, filename))
    df = pl.DataFrame(rows, schema=["cik", "name", "form", "filed", "filename"], orient="row")
    if df.height:
        df = df.with_columns(pl.col("filed").str.to_date("%Y%m%d", strict=False)
                             .fill_null(pl.col("filed").str.to_date("%Y-%m-%d", strict=False)))
    return df


def write_atomic(df: pl.DataFrame, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        df.write_parquet(tmp, compression="zstd", compression_level=3, statistics=True, row_group_size=256_000)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path.stat().st_size


def quarter_of(d: date) -> int:
    return (d.month - 1) // 3 + 1
