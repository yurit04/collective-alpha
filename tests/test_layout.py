import datetime as dt

from collective_alpha.storage import layout


def test_flatfile_key_roundtrip(a_day):
    key = layout.flatfile_key("minute_aggs_v1", a_day)
    assert key == "us_stocks_sip/minute_aggs_v1/2024/04/2024-04-05.csv.gz"
    assert layout.date_from_flatfile_key(key) == a_day
    assert layout.dataset_from_flatfile_key(key) == "minute_aggs_v1"


def test_curated_paths(settings, a_day):
    assert layout.curated_bars_path(settings, "day_aggs_v1", a_day).as_posix().endswith(
        "curated/day_aggs/year=2024/2024-04-05.parquet"
    )
    assert layout.curated_bars_path(settings, "minute_aggs_v1", a_day).as_posix().endswith(
        "curated/minute_aggs/year=2024/month=04/2024-04-05.parquet"
    )
    assert layout.curated_snapshot_path(settings, "tickers", a_day).as_posix().endswith(
        "curated/tickers/asof=2024-04-05/data.parquet"
    )
    assert layout.curated_month_path(settings, "news", dt.date(2024, 4, 1)).as_posix().endswith(
        "curated/news/year=2024/month=04/data.parquet"
    )
