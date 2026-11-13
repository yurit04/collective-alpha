"""`collective status` on fake stores: fresh, stale and missing cases, and the CLI exit code."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from typer.testing import CliRunner
from unicomplex import Check, collect, render
from unicomplex._checks import today_et

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)          # Wednesday 10:00 ET
TODAY = today_et(NOW)                                    # 2026-11-04
YESTERDAY = date(2026, 11, 3)


@pytest.fixture(autouse=True)
def _own_root(tmp_path_factory, monkeypatch):
    """Never read or write the real unicomplex root (run history, lock) from a test."""
    monkeypatch.setenv("UNICOMPLEX_DATA_ROOT", str(tmp_path_factory.mktemp("unicomplex")))


def weekdays(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class FakeMassive:
    def __init__(self, last=YESTERDAY, running=True, last_message=None):
        self.last, self.running = last, running
        self.last_message = last_message or (NOW - timedelta(minutes=1)).isoformat()

    def trading_days(self, start, end):
        return weekdays(start, end)

    def coverage(self):
        return pl.DataFrame({"dataset": ["bars_day", "bars_minute", "tickers"], "partitions": [5825, 5825, 1],
                             "first": ["2003-09-10"] * 2 + ["all"], "last": [self.last.isoformat()] * 2 + ["all"],
                             "rows": [1, 2, 3], "bytes": [1, 2, 3], "updated_at": ["x"] * 3})

    def stream_status(self):
        return {"running": self.running, "pid": 1,
                "last_status": {"connected": True, "last_message_at": self.last_message, "last_error": None}}


class FakeEdgar:
    def __init__(self, through="2026-11-03", current=True):
        self.through, self.current = through, current

    def status(self):
        return {"sync_through": self.through, "companies": 20000, "bulk_loaded_at": "2026-11-01T12:08:40+00:00",
                "last_sync": "2026-11-04T12:19:43+00:00", "series_current": self.current}


class FakeFinra:
    def __init__(self, si="2026-10-15", sv="2026-11-03"):
        self.si, self.sv = si, sv

    def status(self):
        return {"short_interest": {"days": 212, "last": self.si}, "short_volume": {"days": 4341, "last": self.sv}}


class FakeVinculum:
    def __init__(self, through="2026-11-03"):
        self.through = through

    def status(self):
        return {"through": self.through, "built_at": "2026-11-04T12:45:00+00:00", "rows": 123}


class FakeNanoprobe:
    def __init__(self, last="2026-11-03", built=True, behind=False):
        self.last, self.built, self.behind = last, built, behind

    def status(self):
        if not self.built:
            return {"built_at": None, "years": 0, "rows": 0, "first_date": None, "last_date": None,
                    "minute_bars_last": "2026-11-03", "behind": True}
        return {"built_at": "2026-11-04T11:50:00+00:00", "years": 24, "rows": 49_500_000, "first_date": "2003-09-10",
                "last_date": self.last, "minute_bars_last": "2026-11-03", "behind": self.behind}


class FakeRisk:
    def __init__(self, last_month="2026-11-02", built=True):
        self.last_month, self.built = last_month, built

    def status(self):
        return {"built_at": "2026-11-03T19:50:31", "last_month": self.last_month, "days": 3962} if self.built else {}


class FakeAdaptation:
    def __init__(self, days=0):
        self.days = days

    def status(self):
        return {"days_done": self.days, "by_set": {"recent": self.days}, "last": "2026-11-03", "store_gb": 1.5,
                "failed": []}


class Broken:
    def status(self):
        raise FileNotFoundError("not built yet. Run: vinculum build")


def _run(**kw):
    base = {"massive": FakeMassive(), "edgar": FakeEdgar(), "finra": FakeFinra(), "vinculum": FakeVinculum(),
            "nanoprobe": FakeNanoprobe(), "risk": FakeRisk(), "adaptation": FakeAdaptation(), "now": NOW}
    return collect(**{**base, **kw})


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {f"{c.source}/{c.name}": c for c in checks}


def test_everything_fresh(tmp_path):
    c = by_name(_run(conduit_root=tmp_path))
    assert {k: v.state for k, v in c.items()} == {
        "massive/bars_day": "ok", "massive/bars_minute": "ok", "massive/stream": "ok", "edgar/facts": "ok",
        "edgar/series": "ok", "finra/short_interest": "ok", "finra/short_volume": "ok", "vinculum/market_cap": "ok",
        "nanoprobe/summaries": "ok", "borg-queen/risk_model": "ok", "adaptation-matrix/costs": "info", "transwarp-conduit/orders": "info",
        "unicomplex/nightly_run": "info"}
    assert c["massive/bars_day"].expected == "2026-11-03" and c["massive/bars_day"].last == "2026-11-03"
    text = render(list(c.values()), NOW)
    assert "everything is as fresh" in text and "collective status at 2026-11-04 10:00 ET" in text


def test_stale_and_missing(tmp_path):
    two_days_ago = date(2026, 11, 2)
    c = by_name(_run(massive=FakeMassive(last=two_days_ago, running=False), edgar=FakeEdgar("2026-10-30", current=False),
                     finra=FakeFinra(si="2026-10-01"), vinculum=Broken(), nanoprobe=FakeNanoprobe(built=False),
                     risk=FakeRisk(built=False), conduit_root=tmp_path))
    assert c["massive/bars_day"].state == "stale" and c["massive/bars_day"].expected == "2026-11-03"
    assert c["massive/stream"].state == "stale" and "not running" in c["massive/stream"].detail
    assert c["edgar/facts"].state == "stale" and c["edgar/series"].state == "stale"
    assert c["finra/short_interest"].state == "stale" and c["finra/short_volume"].state == "ok"
    assert c["vinculum/market_cap"].state == "missing" and "vinculum build" in c["vinculum/market_cap"].detail
    assert c["nanoprobe/summaries"].state == "missing" and "nanoprobe build" in c["nanoprobe/summaries"].detail
    assert c["borg-queen/risk_model"].state == "missing"
    assert "need attention" in render(list(c.values()), NOW)


def test_monday_expects_friday_and_edgar_slack():
    monday = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)
    c = by_name(collect(massive=FakeMassive(last=date(2026, 11, 6)), edgar=FakeEdgar("2026-11-06"),
                        finra=FakeFinra(sv="2026-11-06"), vinculum=FakeVinculum("2026-11-06"),
                        nanoprobe=FakeNanoprobe("2026-11-06"), risk=FakeRisk(),
                        conduit_root="/nonexistent", now=monday))
    assert c["massive/bars_day"].state == "ok" and c["edgar/facts"].state == "ok" and c["finra/short_volume"].state == "ok"
    assert c["transwarp-conduit/orders"].state == "info" and "no order sheets" in c["transwarp-conduit/orders"].detail


def test_quiet_stream_in_market_hours_is_stale():
    c = by_name(_run(massive=FakeMassive(last_message=(NOW - timedelta(hours=2)).isoformat()), conduit_root="/x"))
    assert c["massive/stream"].state == "stale"
    night = datetime(2026, 11, 4, 2, 0, tzinfo=UTC)
    c = by_name(collect(massive=FakeMassive(last_message=(night - timedelta(hours=5)).isoformat()), edgar=FakeEdgar(),
                        finra=FakeFinra(), vinculum=FakeVinculum(), nanoprobe=FakeNanoprobe(), risk=FakeRisk(),
                        conduit_root="/x", now=night))
    assert c["massive/stream"].state == "ok"                 # outside market hours a quiet stream is fine


def test_nanoprobe_summaries_behind():
    c = by_name(_run(nanoprobe=FakeNanoprobe(last="2026-10-30", behind=True), conduit_root="/x"))["nanoprobe/summaries"]
    assert c.state == "stale" and c.last == "2026-10-30" and c.expected == "2026-11-03"
    assert "behind the minute bars (2026-11-03)" in c.detail


def test_nanoprobe_uses_the_given_massive_store(tmp_path, monkeypatch):
    """Without a nanoprobe fake, the default Nanoprobe reads the same Massive store as the rest."""
    monkeypatch.setenv("NANOPROBE_DATA_ROOT", str(tmp_path / "nanoprobe"))
    calls = []

    class Counting(FakeMassive):
        def coverage(self):
            calls.append(1)
            return super().coverage()

    c = by_name(_run(massive=Counting(), nanoprobe=None, conduit_root="/x"))["nanoprobe/summaries"]
    assert c.state == "missing" and "nanoprobe build" in c.detail      # empty root: not built
    assert len(calls) == 2                                               # massive_checks, then Nanoprobe.status


def test_conduit_journal_is_read(tmp_path):
    from transwarp_conduit import Journal

    Journal(tmp_path).event("plan", sheet_id="x")             # events only, no sheet folders
    assert by_name(_run(conduit_root=tmp_path))["transwarp-conduit/orders"].detail == "no order sheets yet"


def test_cli_exit_code(monkeypatch):
    from unicomplex import cli

    monkeypatch.setattr(cli, "collect", lambda: [Check("massive", "bars_day", "ok", "2026-11-03", "2026-11-03", "")])
    r = CliRunner().invoke(cli.app, ["status"])
    assert r.exit_code == 0 and "everything is as fresh" in r.output
    monkeypatch.setattr(cli, "collect", lambda: [Check("edgar", "series", "stale", None, "current", "rebuild")])
    r = CliRunner().invoke(cli.app, ["status", "--json"])
    assert r.exit_code == 1 and '"state": "stale"' in r.output


def test_cost_archive_is_context_only():
    c = by_name(_run(conduit_root="/x"))["adaptation-matrix/costs"]
    assert c.state == "info" and "adapt run" in c.detail
    c = by_name(_run(adaptation=FakeAdaptation(days=3), conduit_root="/x"))["adaptation-matrix/costs"]
    assert c.state == "info" and c.last == "2026-11-03" and "3 days (recent 3)" in c.detail
