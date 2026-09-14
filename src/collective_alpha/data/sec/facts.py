"""Parse SEC companyfacts JSON into a long table of point-in-time facts."""

from __future__ import annotations

import polars as pl

# taxonomy, concept -> short name. Kept deliberately small; extend as research needs grow.
DEFAULT_CONCEPTS: dict[tuple[str, str], str] = {
    ("dei", "EntityCommonStockSharesOutstanding"): "shares_outstanding",
    ("dei", "EntityPublicFloat"): "public_float",
    ("us-gaap", "CommonStockSharesOutstanding"): "common_shares",
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"): "wavg_shares_basic",
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"): "wavg_shares_diluted",
    ("us-gaap", "Revenues"): "revenue",
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): "revenue_asc606",
    ("us-gaap", "NetIncomeLoss"): "net_income",
    ("us-gaap", "StockholdersEquity"): "equity",
    ("us-gaap", "Assets"): "assets",
    ("us-gaap", "Liabilities"): "liabilities",
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"): "cfo",
    ("us-gaap", "EarningsPerShareDiluted"): "eps_diluted",
    ("us-gaap", "LongTermDebt"): "lt_debt",
    ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"): "cash",
}

FACT_SCHEMA = {
    "cik": pl.Utf8,
    "entity_name": pl.Utf8,
    "concept": pl.Utf8,
    "taxonomy": pl.Utf8,
    "tag": pl.Utf8,
    "unit": pl.Utf8,
    "start": pl.Date,
    "end": pl.Date,
    "val": pl.Float64,
    "filed": pl.Date,
    "form": pl.Utf8,
    "fy": pl.Int32,
    "fp": pl.Utf8,
    "frame": pl.Utf8,
    "accn": pl.Utf8,
}


def parse_companyfacts(doc: dict, concepts: dict[tuple[str, str], str] | None = None) -> pl.DataFrame:
    concepts = concepts or DEFAULT_CONCEPTS
    cik = str(doc.get("cik", "")).zfill(10)
    name = doc.get("entityName")
    facts = doc.get("facts") or {}
    rows: list[dict] = []
    for (tax, tag), short in concepts.items():
        node = (facts.get(tax) or {}).get(tag)
        if not node:
            continue
        for unit, items in (node.get("units") or {}).items():
            for it in items:
                rows.append(
                    {
                        "cik": cik,
                        "entity_name": name,
                        "concept": short,
                        "taxonomy": tax,
                        "tag": tag,
                        "unit": unit,
                        "start": it.get("start"),
                        "end": it.get("end"),
                        "val": it.get("val"),
                        "filed": it.get("filed"),
                        "form": it.get("form"),
                        "fy": it.get("fy"),
                        "fp": it.get("fp"),
                        "frame": it.get("frame"),
                        "accn": it.get("accn"),
                    }
                )
    if not rows:
        return pl.DataFrame(schema=FACT_SCHEMA)
    raw_schema = {**FACT_SCHEMA, "start": pl.Utf8, "end": pl.Utf8, "filed": pl.Utf8}
    df = pl.DataFrame(rows, schema=raw_schema, strict=False)
    return df.with_columns([pl.col(c).str.to_date(strict=False) for c in ("start", "end", "filed")]).select(
        list(FACT_SCHEMA)
    )


def dedupe_facts(df: pl.DataFrame) -> pl.DataFrame:
    """The same (concept, end, start) is restated in later filings; keep every filing (point-in-time),
    but drop exact duplicates."""
    return df.unique(subset=["cik", "concept", "unit", "start", "end", "filed", "val"], keep="first")
