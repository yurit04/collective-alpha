import datetime as dt
import gzip
from pathlib import Path

import polars as pl

from collective_alpha.data.massive.datasets import FLATFILE_DATASETS
from collective_alpha.data.massive.transform import convert_flatfile, records_to_frame

FIXTURE = Path(__file__).parent / "fixtures" / "minute_aggs_sample.csv"


def _gz(tmp_path: Path) -> Path:
    p = tmp_path / "2024-04-05.csv.gz"
    with gzip.open(p, "wt") as fh:
        fh.write(FIXTURE.read_text())
    return p


def test_convert_minute_bars(tmp_path):
    src = _gz(tmp_path)
    dest = tmp_path / "out.parquet"
    n = convert_flatfile(src, "minute_aggs_v1", dt.date(2024, 4, 5), dest)
    df = pl.read_parquet(dest)
    assert n == df.height == 4
    assert df.columns[:10] == [
        "ticker",
        "date",
        "ts",
        "ts_ny",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "transactions",
    ]
    assert "window_start" not in df.columns
    # 2024-04-05 13:30 UTC == 09:30 New York (EDT)
    aapl = df.filter(pl.col("ticker") == "AAPL").sort("ts")
    assert aapl["ts_ny"][0] == dt.datetime(2024, 4, 5, 9, 30)
    assert str(df["ts"].dtype) == "Datetime(time_unit='ns', time_zone='UTC')"
    assert df["date"].unique().to_list() == [dt.date(2024, 4, 5)]
    # sorted by ticker then ts
    assert df["ticker"].to_list() == sorted(df["ticker"].to_list())


def test_convert_day_bars(tmp_path):
    src = _gz(tmp_path)
    dest = tmp_path / "out.parquet"
    spec = FLATFILE_DATASETS["day_aggs_v1"]
    assert spec.table == "day_aggs"
    n = convert_flatfile(src, "day_aggs_v1", dt.date(2024, 4, 5), dest)
    assert n == 4


def test_records_to_frame_flattens():
    recs = [
        {"ticker": "AAPL", "address": {"city": "Cupertino"}, "tags": ["a", "b"], "n": 1},
        {"ticker": "MSFT", "n": 2},
    ]
    df = records_to_frame(recs, asof=dt.date(2024, 4, 5))
    assert set(df.columns) == {"ticker", "address.city", "tags", "n", "asof"}
    assert df.filter(pl.col("ticker") == "AAPL")["tags"][0] == '["a","b"]'
    assert df["asof"].unique().to_list() == [dt.date(2024, 4, 5)]
