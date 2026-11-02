import httpx
import polars as pl
import pytest
from massive_subspace_stream import Ingestor
from massive_subspace_stream._datasets.rest import to_frame
from massive_subspace_stream._sources.rest import NotEntitledError, RestClient

from .conftest import fake_rest


def client(handler) -> RestClient:
    return RestClient("k", "https://api.massive.com", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_pagination_auth_header_and_retry():
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer k"
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if "cursor=2" in str(request.url):
            return httpx.Response(200, json={"results": [{"x": 3}]})
        return httpx.Response(200, json={"results": [{"x": 1}, {"x": 2}],
                                         "next_url": "https://api.massive.com/v3/thing?cursor=2"})

    pages = list(client(handler).paginate("/v3/thing", {"limit": 2}))
    assert pages == [[{"x": 1}, {"x": 2}], [{"x": 3}]]
    assert "apiKey" not in "".join(calls)


def test_403_raises_not_entitled():
    with pytest.raises(NotEntitledError):
        client(lambda r: httpx.Response(403, json={"message": "NOT_AUTHORIZED"})).get("/v3/quotes/X")


def test_to_frame_types_dates_and_nested():
    df = to_frame([
        {"ticker": "A", "ex_dividend_date": "2024-01-02", "published_utc": "2024-01-02T15:04:05Z",
         "publisher": {"name": "Wire"}, "insights": [{"ticker": "A", "sentiment": "positive"}], "tickers": ["A"],
         "cash_amount": 1},
        {"ticker": "B", "ex_dividend_date": None, "published_utc": "2024-01-03T00:00:00Z", "cash_amount": 0.5},
    ])
    assert df.schema["ex_dividend_date"] == pl.Date
    assert isinstance(df.schema["published_utc"], pl.Datetime)
    assert df["publisher_name"].to_list() == ["Wire", None]
    assert df.schema["cash_amount"] == pl.Float64
    assert df.schema["insights"] == pl.String


def test_rest_snapshot_timeseries_and_incremental(settings):
    rest = fake_rest({
        "/stocks/v1/splits": [[{"ticker": "AAPL", "execution_date": "2020-08-31", "split_from": 1, "split_to": 4}]],
        "/stocks/v1/short-interest": [[
            {"ticker": "A", "settlement_date": "2023-12-29", "short_interest": 10},
            {"ticker": "A", "settlement_date": "2024-01-12", "short_interest": 11},
        ]],
        "/v3/reference/tickers": [[{"ticker": "AAPL", "active": True}]],
        "/v3/quotes": NotEntitledError(403, "/x", "no"),
    })
    ing = Ingestor(settings, rest_client=rest)
    results = {r.dataset: r for r in ing.rest(["splits", "short_interest", "tickers"])}
    assert all(r.ok for r in results.values())
    assert pl.read_parquet(ing.layout.rest_file("corporate_actions/splits")).height == 1
    assert pl.read_parquet(ing.layout.rest_file("short_interest", 2024))["short_interest"].to_list() == [11]
    assert ing.catalog.get_state("short_interest", "last_date") == "2024-01-12"
    # tickers pulls both active and delisted
    assert [c[1]["active"] for c in rest.calls if c[0] == "/v3/reference/tickers"] == ["true", "false"]

    # Incremental: the next pull starts from last_date minus the overlap, and upserts.
    rest2 = fake_rest({"/stocks/v1/short-interest": [[
        {"ticker": "A", "settlement_date": "2024-01-12", "short_interest": 12},
        {"ticker": "A", "settlement_date": "2024-01-31", "short_interest": 13},
    ]]})
    ing2 = Ingestor(settings, rest_client=rest2)
    ing2.rest(["short_interest"])
    assert rest2.calls[0][1]["settlement_date.gte"] == "2023-12-12"
    assert pl.read_parquet(ing2.layout.rest_file("short_interest", 2024))["short_interest"].to_list() == [12, 13]


def test_rest_not_entitled_is_skipped(settings):
    rest = fake_rest({"/stocks/v1/short-volume": NotEntitledError(403, "/x", "no")})
    (result,) = Ingestor(settings, rest_client=rest).rest(["short_volume"])
    assert result.ok and "not in your plan" in result.note


def test_ticker_details_daily_snapshot(settings):
    rest = fake_rest({"/v3/reference/tickers": [[
        {"ticker": "TSM", "type": "ADRC", "active": True}, {"ticker": "BRK.B", "type": "CS", "active": True},
        {"ticker": "SPY", "type": "ETF", "active": True}, {"ticker": "GONE", "type": "CS", "active": True}]]})
    details = {"TSM": {"ticker": "TSM", "type": "ADRC", "market_cap": 2.1e12, "share_class_shares_outstanding": 5186474000,
                       "weighted_shares_outstanding": 5186474013, "list_date": "1997-10-09", "homepage_url": "x"},
               "BRK.B": {"ticker": "BRK.B", "type": "CS", "market_cap": 1.16e12, "sic_code": "6331",
                         "share_class_shares_outstanding": 1408035161, "weighted_shares_outstanding": 2140709794}}
    gets = []

    def get(path, params=None):
        gets.append(path)
        t = path.rsplit("/", 1)[1].replace("%2E", ".")
        if t not in details:
            from massive_subspace_stream._sources.rest import RestError
            raise RestError(404, path, "not found")
        return {"results": details[t]}

    rest.get = get
    ing = Ingestor(settings, rest_client=rest)
    ing.rest(["tickers"])
    (res,) = ing.rest(["ticker_details"])
    assert res.ok and res.rows == 2 and len(gets) == 3          # ETF skipped, 404 left out
    from massive_subspace_stream import Store
    df = Store(settings=settings).ticker_details().sort("ticker")
    assert df["ticker"].to_list() == ["BRK.B", "TSM"]
    assert df.filter(pl.col("ticker") == "TSM")["share_class_shares_outstanding"][0] == 5186474000
    assert "homepage_url" not in df.columns and df.schema["as_of"] == pl.Date
    (again,) = ing.rest(["ticker_details"])                    # once per day
    assert again.note == "already fetched today" and len(gets) == 3
