import datetime as dt
import json
from pathlib import Path

import polars as pl

from collective_alpha.data.sec.facts import FACT_SCHEMA, dedupe_facts, parse_companyfacts
from collective_alpha.universe.marketcap import shares_asof, shares_series

FIX = Path(__file__).parent / "fixtures" / "companyfacts_aapl_small.json"
D = dt.date


def test_parse_companyfacts():
    doc = json.loads(FIX.read_text())
    df = parse_companyfacts(doc)
    assert df.schema == pl.Schema(FACT_SCHEMA)
    assert df["cik"].unique().to_list() == ["0000320193"]
    assert set(df["concept"].unique().to_list()) == {"shares_outstanding", "common_shares", "net_income"}
    sh = df.filter(pl.col("concept") == "shares_outstanding").sort("filed")
    assert sh["filed"].dtype == pl.Date and sh["start"].null_count() == sh.height  # instants have no start
    ni = df.filter(pl.col("concept") == "net_income")
    assert ni["start"].null_count() == 0  # durations have a start
    assert dedupe_facts(pl.concat([df, df])).height == df.height


def test_parse_empty():
    df = parse_companyfacts({"cik": 1, "entityName": "x", "facts": {}})
    assert df.height == 0 and df.schema == pl.Schema(FACT_SCHEMA)


def _fact(cik, concept, end, filed, val, unit="shares"):
    return {
        "cik": cik,
        "entity_name": "x",
        "concept": concept,
        "taxonomy": "t",
        "tag": concept,
        "unit": unit,
        "start": None,
        "end": end,
        "val": float(val),
        "filed": filed,
        "form": "10-Q",
        "fy": 2024,
        "fp": "Q1",
        "frame": None,
        "accn": "a",
    }


def test_shares_series_precedence_and_asof():
    facts = pl.DataFrame(
        [
            _fact("1", "wavg_shares_basic", D(2024, 3, 31), D(2024, 5, 1), 90),
            _fact("1", "common_shares", D(2024, 3, 31), D(2024, 5, 1), 95),
            _fact("1", "shares_outstanding", D(2024, 4, 20), D(2024, 5, 1), 100),  # dei wins on 2024-05-01
            _fact("1", "common_shares", D(2024, 6, 30), D(2024, 8, 1), 110),  # only concept in the next filing
            _fact("2", "shares_outstanding", D(2022, 1, 1), D(2022, 2, 1), 50),  # old -> ages out
        ],
        schema_overrides={"val": pl.Float64, "fy": pl.Int32},
    ).with_columns([pl.col(c).cast(pl.Date) for c in ("start", "end", "filed")])
    ser = shares_series(facts)
    assert ser.filter(pl.col("cik") == "1").sort("filed")["shares"].to_list() == [
        95.0,
        110.0,
    ]  # common_shares reported most often -> used throughout
    keys = pl.DataFrame(
        {"cik": ["1", "1", "1", "2"], "date": [D(2024, 4, 30), D(2024, 5, 1), D(2024, 9, 1), D(2024, 1, 1)]}
    )
    out = shares_asof(ser, keys).sort(["cik", "date"])
    # before the first filing -> null; on the filing date -> usable; later filing supersedes; stale -> null
    assert out["shares"].to_list() == [None, 95.0, 110.0, None]
    assert out["shares_source"].to_list()[1:3] == ["common_shares", "common_shares"]


def test_ads_ratio_and_plausibility_guard():
    from collective_alpha.universe.marketcap import _snap_ratio

    assert _snap_ratio(5.02) == 5.0 and _snap_ratio(0.251) == 0.25 and _snap_ratio(1.49) == 1.49
    facts = pl.DataFrame(
        [
            _fact("A", "shares_outstanding", D(2020, 3, 31), D(2020, 3, 31), 2.4e11),  # 1000x filing error
            _fact("A", "shares_outstanding", D(2021, 3, 31), D(2021, 3, 31), 2.5e8),
            _fact("A", "shares_outstanding", D(2022, 3, 31), D(2022, 3, 31), 2.6e8),
            _fact("T", "shares_outstanding", D(2024, 12, 31), D(2025, 4, 1), 25.9e9),  # TSM-like: 5 ordinary / ADS
        ],
        schema_overrides={"val": pl.Float64, "fy": pl.Int32},
    ).with_columns([pl.col(c).cast(pl.Date) for c in ("start", "end", "filed")])
    details = pl.DataFrame(
        {"ticker": ["TSM"], "type": ["ADRC"], "cik": ["T"], "share_class_shares_outstanding": [5.19e9]}
    )
    ser = shares_series(facts, details)
    a = ser.filter(pl.col("cik") == "A")
    assert a["shares_reported"].to_list() == [2.5e8, 2.6e8]  # the 1000x value is dropped
    t = ser.filter(pl.col("cik") == "T").row(0, named=True)
    assert t["ads_ratio"] == 5.0 and abs(t["shares"] - 25.9e9 / 5) < 1
