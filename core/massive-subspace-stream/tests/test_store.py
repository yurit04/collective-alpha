from datetime import UTC, date, datetime, timedelta

import pandas as pd
import polars as pl
import pytest
from massive_subspace_stream import Store
from massive_subspace_stream._adjust import split_factor_table
from massive_subspace_stream._catalog import Catalog
from massive_subspace_stream._layout import Layout
from massive_subspace_stream._storage import write_parquet


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def bars_frame(rows):
    return pl.DataFrame(rows, schema={
        "ticker": pl.String, "date": pl.Date, "timestamp": pl.Datetime("ns", "UTC"), "open": pl.Float64,
        "high": pl.Float64, "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64,
        "transactions": pl.Int64}, orient="row")


@pytest.fixture
def store(settings):
    layout = Layout(settings.data_root)
    layout.ensure()
    cat = Catalog(layout.catalog_path)
    # Daily: AAPL across a 4:1 split on 2020-08-31, plus a dividend.
    days = [date(2020, 8, 27), date(2020, 8, 28), date(2020, 8, 31), date(2020, 9, 1)]
    closes = [500.0, 500.0, 125.0, 130.0]
    daily = bars_frame([("AAPL", d, utc(d.year, d.month, d.day, 4), c, c, c, c, 1000.0, 1) for d, c in zip(days, closes)]
                       + [("MSFT", d, utc(d.year, d.month, d.day, 4), 200.0, 200, 200, 200, 10.0, 1) for d in days])
    write_parquet(daily, layout.bars_day_file(2020))
    cat.mark_many("bars_day", [(d.isoformat(), 2, 0) for d in days])
    write_parquet(pl.DataFrame({"ticker": ["AAPL"], "execution_date": [date(2020, 8, 31)],
                                "split_from": [1.0], "split_to": [4.0]}),
                  layout.rest_file("corporate_actions/splits"))
    write_parquet(pl.DataFrame({"ticker": ["AAPL"], "ex_dividend_date": [date(2020, 9, 1)],
                                "cash_amount": [1.3], "historical_adjustment_factor": [0.99]}),
                  layout.rest_file("corporate_actions/dividends"))
    write_parquet(pl.DataFrame({"ticker": ["AAPL", "AAPL", "MSFT"], "active": [True, False, True],
                                "type": ["CS", "CS", "CS"], "name": ["Apple", "Old", "Microsoft"]}),
                  layout.rest_file("reference/tickers"))
    # Minute: 2024-11-29 is an early close (13:00 ET = 18:00 UTC).
    day = date(2024, 11, 29)
    minute = bars_frame([
        ("SPY", day, utc(2024, 11, 29, 14, 29), 1, 1, 1, 1, 1.0, 1),   # 09:29 ET pre-market
        ("SPY", day, utc(2024, 11, 29, 14, 30), 2, 2, 2, 2, 1.0, 1),   # 09:30 ET regular
        ("SPY", day, utc(2024, 11, 29, 17, 59), 3, 3, 3, 3, 1.0, 1),   # 12:59 ET regular
        ("SPY", day, utc(2024, 11, 29, 18, 30), 4, 4, 4, 4, 1.0, 1),   # 13:30 ET after early close
    ])
    write_parquet(minute, layout.bars_minute_file(day))
    cat.mark("bars_minute", day.isoformat(), minute.height, 0)
    return Store(settings=settings)


def test_daily_bars_filters_and_backends(store):
    df = store.bars("aapl", start="2020-08-28", end="2020-08-31")
    assert df["date"].to_list() == [date(2020, 8, 28), date(2020, 8, 31)]
    pdf = store.bars(["AAPL", "MSFT"], start="2020-08-27", backend="pandas")
    assert isinstance(pdf, pd.DataFrame) and len(pdf) == 8
    assert isinstance(store.bars("MSFT", start="2020-08-27", lazy=True), pl.LazyFrame)


def test_split_adjustment(store):
    df = store.bars("AAPL", start="2020-08-27", adjust="splits")
    assert df["close"].to_list() == [125.0, 125.0, 125.0, 130.0]
    assert df["volume"].to_list() == [4000.0, 4000.0, 1000.0, 1000.0]
    msft = store.bars("MSFT", start="2020-08-27", adjust="splits")
    assert msft["close"].to_list() == [200.0] * 4


def test_total_return_adjustment(store):
    df = store.bars("AAPL", start="2020-08-27", adjust="all")
    assert df["close"].to_list() == pytest.approx([125 * 0.99, 125 * 0.99, 125 * 0.99, 130.0])


def test_cumulative_split_factors():
    t = split_factor_table(pl.DataFrame({"ticker": ["X", "X"], "execution_date": [date(2014, 6, 9), date(2020, 8, 31)],
                                         "split_from": [1.0, 1.0], "split_to": [7.0, 4.0]}))
    got = dict(zip(t["eff_date"].to_list(), t["split_factor"].to_list()))
    assert got[date(2020, 8, 30)] == pytest.approx(0.25)
    assert got[date(2014, 6, 8)] == pytest.approx(1 / 28)


def test_session_filter_respects_early_close(store):
    regular = store.bars("SPY", start="2024-11-29", end="2024-11-29", freq="1min", session="regular")
    assert regular["close"].to_list() == [2.0, 3.0]
    extended = store.bars("SPY", start="2024-11-29", end="2024-11-29", freq="1min", session="extended")
    assert extended["close"].to_list() == [1.0, 4.0]


def test_minute_bars_need_a_start_for_all_tickers(store):
    with pytest.raises(ValueError, match="start"):
        store.bars(freq="1min")


def test_universe_includes_reference_and_type_filter(store):
    uni = store.universe("2020-08-31", types=["CS"])
    assert uni["ticker"].to_list() == ["AAPL", "MSFT"]
    assert uni.filter(pl.col("ticker") == "AAPL")["name"].item() == "Apple"  # active row preferred


def test_empty_results_and_missing_dataset(store):
    assert store.bars("AAPL", start="1999-01-01", end="1999-12-31").height == 0
    with pytest.raises(FileNotFoundError, match="subspace backfill rest news"):
        store.news("AAPL")


def test_trades_warn_when_not_covered(store):
    with pytest.warns(UserWarning, match="backfill history --dataset trades"):
        df = store.trades("AAPL", start="2024-11-29")
    assert df.height == 0


def test_include_live_fills_days_without_official_files(store, settings):
    from massive_subspace_stream._ingest.stream import LiveWriter

    writer = LiveWriter(Layout(settings.data_root))
    start = int(utc(2024, 12, 2, 14, 30).timestamp() * 1000)
    writer.add({"ev": "AM", "sym": "SPY", "s": start, "e": start + 60_000, "o": 5, "h": 5, "l": 5, "c": 5, "v": 9})
    writer.flush(now=utc(2024, 12, 2, 15, 0))
    df = store.bars("SPY", start="2024-11-29", end="2024-12-02", freq="1min", include_live=True)
    assert df["close"].to_list() == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert store.live("bars_1m", "SPY", start="2024-12-02").height == 1


def test_coverage_and_trading_days(store):
    cov = store.coverage()
    assert set(cov["dataset"]) == {"bars_day", "bars_minute"}
    days = store.trading_days("2024-11-27", "2024-12-02")
    assert days == [date(2024, 11, 27), date(2024, 11, 29), date(2024, 12, 2)]
    assert date(2024, 11, 28) not in days and timedelta(days=1)
