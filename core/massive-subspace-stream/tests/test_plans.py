"""Massive plan limits (Starter: 5 years, delayed stream, no trades/quotes/financials) and the
snapshot guard that keeps a shorter answer from deleting stored history."""

import asyncio
import json
from datetime import date

import polars as pl
import pytest
from massive_subspace_stream import ConfigError, Ingestor, StreamService, load_settings
from massive_subspace_stream._config import DELAYED_WS_URL, REALTIME_WS_URL
from massive_subspace_stream._datasets.rest import REST_DATASETS
from massive_subspace_stream._ingest.backfill import guard_snapshot
from massive_subspace_stream._ingest.stream import StreamRefused
from massive_subspace_stream._services import build_plist

from .conftest import FakeFlatClient, FakeWS, bars_csv, day_rows, fake_rest, minute_rows

TODAY = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr("massive_subspace_stream._config.today_et", lambda: TODAY)


@pytest.fixture
def starter(settings):
    return load_settings(data_root=settings.data_root, env_file=None, api_key="test-key",
                         s3_access_key_id="id", s3_secret_access_key="secret", plan="starter")


# ---------------------------------------------------------------- settings
def test_plan_setting(tmp_path, monkeypatch):
    assert load_settings(env_file=None).plan == "advanced"
    assert load_settings(env_file=None, plan="Starter").plan == "starter"
    with pytest.raises(ConfigError, match="Unknown Massive plan"):
        load_settings(env_file=None, plan="premium")
    monkeypatch.setenv("MASSIVE_PLAN", "developer")
    assert load_settings(env_file=None).plan == "developer"
    env = tmp_path / "m.env"
    env.write_text('export MASSIVE_PLAN="starter"\n')
    monkeypatch.delenv("MASSIVE_PLAN")
    assert load_settings(env_file=env).plan == "starter"


def test_history_start():
    assert load_settings(env_file=None).history_start() is None
    assert load_settings(env_file=None, plan="starter").history_start() == date(2021, 11, 2)
    assert load_settings(env_file=None, plan="developer").history_start(date(2028, 2, 29)) == date(2018, 2, 28)


def test_stream_url_follows_the_plan():
    assert load_settings(env_file=None).stream_url == REALTIME_WS_URL
    assert load_settings(env_file=None, plan="starter").stream_url == DELAYED_WS_URL
    # A real-time URL left in an env file from a bigger plan is not used on a delayed plan.
    assert load_settings(env_file=None, plan="starter", ws_url=REALTIME_WS_URL + "/").stream_url == DELAYED_WS_URL
    assert load_settings(env_file=None, plan="starter", ws_url="wss://other.example").stream_url == "wss://other.example"


# ---------------------------------------------------------------- snapshot guard
SPLITS = REST_DATASETS["splits"]


def _splits(*rows):
    return pl.DataFrame([{"id": i, "ticker": t, "execution_date": date.fromisoformat(d)} for i, t, d in rows])


def test_guard_replaces_when_the_answer_is_complete():
    old = _splits(("a", "AAPL", "2005-02-28"), ("b", "AAPL", "2020-08-31"))
    new = _splits(("a", "AAPL", "2005-02-28"), ("c", "NVDA", "2024-06-10"))  # b withdrawn by Massive
    df, note = guard_snapshot(old, new, SPLITS, tolerance=0.5)
    assert note == "" and df["id"].to_list() == ["a", "c"]
    assert guard_snapshot(None, new, SPLITS) == (new, "")


def test_guard_keeps_history_before_a_shorter_answer():
    old = _splits(("a", "AAPL", "2005-02-28"), ("b", "AAPL", "2014-06-09"), ("c", "AAPL", "2020-08-31"))
    new = _splits(("c", "AAPL", "2020-08-31"), ("d", "NVDA", "2024-06-10"))
    df, note = guard_snapshot(old, new, SPLITS)
    assert sorted(df["id"].to_list()) == ["a", "b", "c", "d"]
    assert "kept 2 stored rows dated before 2020-08-31" in note


def test_guard_keeps_rows_missing_from_a_truncated_answer():
    old = _splits(*[(str(i), f"T{i}", "2024-01-02") for i in range(100)])
    new = _splits(*[(str(i), f"T{i}", "2024-01-02") for i in range(50)])  # same span, half the rows
    df, note = guard_snapshot(old, new, SPLITS)
    assert df.height == 100 and "kept 50 stored rows missing from it" in note


def test_guard_refuses_a_short_answer_without_keys():
    spec = REST_DATASETS["exchanges"]
    df, note = guard_snapshot(pl.DataFrame({"id": [1, 2, 3]}), pl.DataFrame({"id": [1]}), spec)
    assert df is None and "refused" in note


def test_rest_refresh_keeps_old_splits(starter):
    old = fake_rest({"/stocks/v1/splits": [[
        {"id": "a", "ticker": "AAPL", "execution_date": "2005-02-28", "split_from": 1, "split_to": 2},
        {"id": "b", "ticker": "AAPL", "execution_date": "2020-08-31", "split_from": 1, "split_to": 4}]]})
    Ingestor(starter, rest_client=old).rest(["splits"])
    capped = fake_rest({"/stocks/v1/splits": [[
        {"id": "b", "ticker": "AAPL", "execution_date": "2020-08-31", "split_from": 1, "split_to": 4},
        {"id": "c", "ticker": "NVDA", "execution_date": "2024-06-10", "split_from": 1, "split_to": 10}]]})
    ing = Ingestor(starter, rest_client=capped)
    (res,) = ing.rest(["splits"])
    assert res.ok and "kept 1 stored rows" in res.note
    stored = pl.read_parquet(ing.layout.rest_file("corporate_actions/splits"))
    assert sorted(stored["id"].to_list()) == ["a", "b", "c"]


def test_rest_refresh_refused_snapshot_keeps_file(starter):
    full = fake_rest({"/v3/reference/exchanges": [[{"id": i} for i in range(10)]]})
    Ingestor(starter, rest_client=full).rest(["exchanges"])
    short = fake_rest({"/v3/reference/exchanges": [[{"id": 1}]]})
    ing = Ingestor(starter, rest_client=short)
    (res,) = ing.rest(["exchanges"])
    assert not res.ok and "refused" in res.failed["snapshot"]
    assert pl.read_parquet(ing.layout.rest_file("reference/exchanges")).height == 10


# ---------------------------------------------------------------- what the plan downloads
def test_rest_defaults_skip_financials_on_starter(settings, starter):
    assert "ratios" in Ingestor(settings).rest_defaults()
    defaults = Ingestor(starter).rest_defaults()
    assert not {"income_statements", "balance_sheets", "cash_flow_statements", "ratios"} & set(defaults)
    assert {"splits", "ticker_details", "short_interest"} <= set(defaults)


def _flat_ingestor(settings):
    flat = FakeFlatClient()
    for day in (date(2019, 6, 3), date(2024, 1, 2)):
        flat.add("minute_aggs_v1", day, bars_csv(minute_rows(day)))
        flat.add("day_aggs_v1", day, bars_csv(day_rows(day, {"AAPL": 10.0})))
    return Ingestor(settings, flat_client=flat), flat


def test_flat_files_start_at_the_plan_horizon(starter):
    ing, flat = _flat_ingestor(starter)
    r = ing.flat("bars_minute", "2019-01-01", "2024-12-31")
    assert r.done == 1 and flat.downloads == ["us_stocks_sip/minute_aggs_v1/2024/01/2024-01-02.csv.gz"]
    r = ing.flat("bars_day", "2019-01-01", "2019-12-31")
    assert r.done == 0 and "outside the starter plan" in r.note and len(flat.downloads) == 1
    r = ing.flat("trades", "2024-01-02", "2024-01-02", tickers=["AAPL"])
    assert r.ok and r.note == "skipped: not in the starter plan"


def test_verify_lists_days_the_plan_cannot_repair(settings, starter):
    ing, flat = _flat_ingestor(settings)  # downloaded on the bigger plan
    ing.flat("bars_minute", "2019-01-01", "2024-12-31")
    for day in (date(2019, 6, 3), date(2024, 1, 2)):
        ing.layout.bars_minute_file(day).unlink()
    ing2 = Ingestor(starter, flat_client=flat)
    report = ing2.verify("bars_minute", repair=True)
    assert report["broken"] == ["2019-06-03", "2024-01-02"] and report["unrepairable"] == ["2019-06-03"]
    assert ing2.layout.bars_minute_file(date(2024, 1, 2)).exists()
    assert not ing2.layout.bars_minute_file(date(2019, 6, 3)).exists()
    assert ing2.catalog.get("bars_minute", "2019-06-03") is not None  # still listed as broken


# ---------------------------------------------------------------- stream
def test_stream_drops_channels_outside_the_plan(starter):
    svc = StreamService(starter, channels=["AM", "A"], watchlist=["AAPL"], watch_channels=["T", "Q"])
    assert svc.subscriptions() == ["AM.*", "A.*"]
    with pytest.raises(ValueError, match="nothing to subscribe"):
        StreamService(starter, channels=["T"], watchlist=[])


def test_refused_login_stops_the_stream(starter, monkeypatch):
    import orjson
    import websockets

    svc = StreamService(starter, channels=["AM"], watchlist=[], flush_seconds=0.05)
    ws = FakeWS(svc, [orjson.dumps([{"ev": "status", "status": "auth_failed", "message": "plan"}])])
    urls = []
    monkeypatch.setattr(websockets, "connect", lambda url, **k: urls.append(url) or ws)
    with pytest.raises(StreamRefused):
        asyncio.run(svc.run())
    assert urls == [DELAYED_WS_URL + "/stocks"]
    assert "auth_failed" in json.loads(svc.status_file.read_text())["stopped_reason"]


def test_stream_service_restarts_only_after_failures(settings):
    plist = build_plist("stream", settings)
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["EnvironmentVariables"]["SUBSPACE_SERVICE"] == "1"
