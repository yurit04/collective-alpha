"""Freshness checks. Each check reads one store through its public API and compares what it
holds with what the schedule should have produced by now (and the nightly run's own history);
nothing here writes or connects to a broker. A store that is missing or unreadable is reported, never raised."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
STATES = ("ok", "stale", "info", "missing")
EDGAR_SYNC_SLACK_DAYS = 3        # the sync runs Tue-Sat through yesterday: Monday shows Friday
SHORT_INTEREST_MAX_DAYS = 26     # two settlement cycles plus FINRA's 7-business-day publication lag
RISK_MODEL_MAX_DAYS = 45         # month ends; rebuilt by the nightly run (or by hand)
STREAM_QUIET_MINUTES = 30        # a running stream without a message this long (in market hours)


@dataclass
class Check:
    """One line of the status: `state` is ok (as fresh as the schedule allows), stale (behind
    it), info (no expectation, shown for context) or missing (no store or unreadable)."""

    source: str
    name: str
    state: str
    last: str | None = None       # the latest date or time the store holds
    expected: str | None = None   # what it should hold by now
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def today_et(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(ET).date()


def _iso(d: Any) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.astimezone(UTC).isoformat(timespec="minutes")
    return str(d)[:10] if not isinstance(d, date) else d.isoformat()


def _date(s: Any) -> date | None:
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _guard(source: str, name: str, fn: Callable[[], list[Check]]) -> list[Check]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one missing store must not hide the others
        return [Check(source, name, "missing", detail=f"{type(exc).__name__}: {exc}")]


def _vs(last: date | None, expected: date | None) -> str:
    if last is None:
        return "missing"
    if expected is None or last >= expected:
        return "ok"
    return "stale"


# ---------------------------------------------------------------------------- Massive
def last_trading_day(store, today: date) -> date:
    """The most recent completed session before `today` (what a morning sync should hold)."""
    days = store.trading_days(today - timedelta(days=14), today - timedelta(days=1))
    days = [d if isinstance(d, date) else d.date() for d in days]
    return days[-1]


def massive_checks(store, today: date, now: datetime) -> list[Check]:
    out: list[Check] = []
    expected = last_trading_day(store, today)
    cov = {r["dataset"]: r for r in store.coverage().iter_rows(named=True)}
    for ds in ("bars_day", "bars_minute"):
        last = _date(cov.get(ds, {}).get("last"))
        out.append(Check("massive", ds, _vs(last, expected), _iso(last), _iso(expected),
                         f"{cov[ds]['partitions']} days stored" if ds in cov else "not downloaded"))
    st = store.stream_status()
    ls = st.get("last_status") or {}
    last_msg = ls.get("last_message_at")
    if not st.get("running"):
        out.append(Check("massive", "stream", "stale", _iso(last_msg), "running",
                         "stream service not running (`subspace stream status`)"))
    else:
        age = None
        if last_msg:
            age = (now - datetime.fromisoformat(last_msg)).total_seconds() / 60
        quiet = age is not None and age > STREAM_QUIET_MINUTES and _market_open(now)
        detail = f"pid {st.get('pid')}, connected={ls.get('connected')}"
        if ls.get("last_error"):
            detail += f", last error: {str(ls['last_error'])[:60]}"
        out.append(Check("massive", "stream", "stale" if quiet else "ok", _iso(last_msg), "running", detail))
    return out


def _market_open(now: datetime) -> bool:
    """A rough regular-session test (weekday, 09:30-16:00 ET); holidays count as open."""
    t = now.astimezone(ET)
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)


# ---------------------------------------------------------------------------- EDGAR
def edgar_checks(edgar, today: date) -> list[Check]:
    st = edgar.status()
    through = _date(st.get("sync_through"))
    expected = today - timedelta(days=EDGAR_SYNC_SLACK_DAYS)
    out = [Check("edgar", "facts", _vs(through, expected), _iso(through), f"{_iso(expected)} or later",
                 f"{st.get('companies', 0):,} companies, bulk {str(st.get('bulk_loaded_at', ''))[:10]}, "
                 f"last sync {str(st.get('last_sync', ''))[:16]}")]
    cur = st.get("series_current")
    out.append(Check("edgar", "series", "ok" if cur else "stale", None, "built from the current facts",
                     "materialized series current" if cur else "rebuild pending (`cortical series`)"))
    return out


# ---------------------------------------------------------------------------- FINRA
def finra_checks(finra, today: date, expected_session: date) -> list[Check]:
    st = finra.status()
    si, sv = st.get("short_interest", {}), st.get("short_volume", {})
    si_last = _date(si.get("last"))
    si_expected = today - timedelta(days=SHORT_INTEREST_MAX_DAYS)
    sv_last = _date(sv.get("last"))
    return [
        Check("finra", "short_interest", _vs(si_last, si_expected), _iso(si_last), f"{_iso(si_expected)} or later",
              f"{si.get('days', 0)} settlement dates"),
        Check("finra", "short_volume", _vs(sv_last, expected_session), _iso(sv_last), _iso(expected_session),
              f"{sv.get('days', 0)} days"),
    ]


# ---------------------------------------------------------------------------- vinculum
def vinculum_checks(v, expected_session: date) -> list[Check]:
    st = v.status()
    through = _date(st.get("through"))
    built = str(st.get("built_at", ""))[:16]
    return [Check("vinculum", "market_cap", _vs(through, expected_session), _iso(through), _iso(expected_session),
                  f"built {built}, {st.get('rows', 0):,} rows" if through else "not built (`vinculum build`)")]


# ---------------------------------------------------------------------------- intraday summaries
def nanoprobe_checks(probe, expected_session: date) -> list[Check]:
    """The daily intraday summaries, built by the nightly run after vinculum, should reach the
    last completed session."""
    st = probe.status()
    if not st.get("built_at"):
        return [Check("nanoprobe", "summaries", "missing", detail="not built (`nanoprobe build`)")]
    last = _date(st.get("last_date"))
    detail = f"built {str(st.get('built_at', ''))[:16]}, {st.get('years', 0)} years, {st.get('rows', 0):,} rows"
    if st.get("behind"):
        detail += f"; behind the minute bars ({st.get('minute_bars_last')})"
    return [Check("nanoprobe", "summaries", _vs(last, expected_session), _iso(last), _iso(expected_session), detail)]


# ---------------------------------------------------------------------------- risk model
def risk_checks(risk, today: date) -> list[Check]:
    st = risk.status()
    if not st.get("built_at"):
        return [Check("borg-queen", "risk_model", "missing", detail="not built (`queen build`)")]
    last = _date(st.get("last_month"))
    expected = today - timedelta(days=RISK_MODEL_MAX_DAYS)
    return [Check("borg-queen", "risk_model", _vs(last, expected), _iso(last), f"{_iso(expected)} or later",
                  f"built {str(st.get('built_at', ''))[:16]}, {st.get('days', 0):,} days of factor returns")]


# ---------------------------------------------------------------------------- execution
# ---------------------------------------------------------------------------- cost archive
def adaptation_checks(matrix) -> list[Check]:
    """The cost archive is built once (`adapt run`), not nightly: shown for context."""
    st = matrix.status()
    if not st.get("days_done"):
        return [Check("adaptation-matrix", "costs", "info", detail="no days measured yet (`adapt plan`, `adapt run`)")]
    sets = ", ".join(f"{k} {v}" for k, v in sorted((st.get("by_set") or {}).items()))
    detail = f"{st['days_done']} days ({sets}), {st.get('store_gb', 0):.1f} GB"
    if st.get("failed"):
        detail += f"; {len(st['failed'])} failed (`adapt status`)"
    return [Check("adaptation-matrix", "costs", "info", _iso(_date(st.get("last"))), None, detail)]


def conduit_checks(root: Path) -> list[Check]:
    from transwarp_conduit import Journal, OrderSheet

    j = Journal(root)
    ids = sorted(p.name for p in j.sheets.glob("*") if p.is_dir()) if j.sheets.exists() else []
    if not ids:
        return [Check("transwarp-conduit", "orders", "info", detail="no order sheets yet")]
    sheet = OrderSheet.load(j.sheets, ids[-1])
    fills = j.fills()
    last_fill = fills["time"].max() if fills.height else None
    detail = f"last sheet {sheet.sheet_id} {sheet.status}, {sheet.to_send.height} orders; {fills.height} fills journaled"
    return [Check("transwarp-conduit", "orders", "info", _iso(last_fill), None, detail)]


# ---------------------------------------------------------------------------- nightly run
NIGHTLY_GRACE_MINUTES = 15       # a run due less than this long ago may not have started yet
RUN_WEEKDAYS = (1, 2, 3, 4, 5)   # Tue-Sat (Python weekday numbers), as the launchd job


def last_scheduled(now: datetime, hour: int, minute: int, tz=None) -> datetime:
    """The latest scheduled start (Tue-Sat at hour:minute, local time) at least the grace
    period before `now`."""
    local = (now - timedelta(minutes=NIGHTLY_GRACE_MINUTES)).astimezone(tz)
    for back in range(8):
        day = local.date() - timedelta(days=back)
        at = datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute), local.tzinfo)
        if day.weekday() in RUN_WEEKDAYS and at <= local:
            return at
    raise AssertionError("unreachable: a scheduled day falls in every week")


def ready_at(started: datetime, settings, tz=None) -> datetime:
    """The `ready_by` time on the local day a run started."""
    from ._config import ready_by_time

    local = started.astimezone(tz)
    hour, minute = ready_by_time(settings)
    return datetime.combine(local.date(), datetime.min.time().replace(hour=hour, minute=minute), local.tzinfo)


def ran_late(started: datetime, finished: datetime | None, settings, tz=None, now: datetime | None = None) -> bool:
    """A run is late when it was under way at its day's `ready_by` time: it started before it
    and finished (or, still running, `now` is) after it. A run started by hand later that day
    is not late."""
    deadline = ready_at(started, settings, tz)
    end = finished or now
    return started < deadline and end is not None and end > deadline


def nightly_checks(runner, now: datetime, tz=None) -> list[Check]:
    """Did the last scheduled full run happen, succeed and finish by `ready_by`? A run that
    finished up to `skip_if_ok_hours` before the scheduled time counts (the scheduled one then
    skips)."""
    s = runner.settings
    due = last_scheduled(now, s.run_hour, s.run_minute, tz)
    expected = _iso(due)
    held = runner.running()
    if held is not None:
        started = datetime.fromisoformat(held["started"]) if held.get("started") else None
        if started is not None and ran_late(started, None, s, tz, now):
            return [Check("unicomplex", "nightly_run", "stale", held.get("started"), expected,
                          f"still running past the {s.ready_by} ready-by time ({held.get('run_id', '?')}); "
                          "`pmset -g log` shows whether the Mac slept")]
        return [Check("unicomplex", "nightly_run", "info", held.get("started"), expected,
                      f"running now ({held.get('run_id', '?')}: {', '.join(held.get('steps', []))})")]
    last = runner.last(full=True)
    if last is None:
        return [Check("unicomplex", "nightly_run", "info", None, expected,
                      "no full run recorded yet (`collective run`, or `collective service install`)")]
    finished = datetime.fromisoformat(last["finished"]) if last.get("finished") else None
    when = _iso(finished)
    if finished is None or finished < due - timedelta(hours=s.skip_if_ok_hours):
        return [Check("unicomplex", "nightly_run", "stale", when, expected,
                      f"no full run since the one scheduled at {due:%a %H:%M} (last: {last.get('run_id')})")]
    if last.get("status") != "ok":
        failed = last.get("failed") or []
        return [Check("unicomplex", "nightly_run", "stale", when, expected,
                      f"run {last.get('run_id')} {last.get('status')}: " + (", ".join(failed) or "?")
                      + (f"; log {last['log']}" if last.get("log") else ""))]
    secs = sum(st.get("seconds", 0) for st in last.get("steps", []))
    started = datetime.fromisoformat(last["started"]) if last.get("started") else None
    if started is not None and ran_late(started, finished, s, tz):
        return [Check("unicomplex", "nightly_run", "stale", when, expected,
                      f"run {last.get('run_id')} finished {finished.astimezone(tz):%H:%M}, after the {s.ready_by} "
                      f"ready-by time ({secs / 60:.0f} min of steps); `pmset -g log` shows whether the Mac slept")]
    return [Check("unicomplex", "nightly_run", "ok", when, expected,
                  f"run {last.get('run_id')}: {len(last.get('steps', []))} steps OK in {secs / 60:.0f} min")]


# ---------------------------------------------------------------------------- all together
def collect(massive=None, edgar=None, finra=None, vinculum=None, risk=None, conduit_root: Path | str | None = None,
            now: datetime | None = None, runner=None, nanoprobe=None, adaptation=None) -> list[Check]:
    """Every check. Stores default to each package's configured one, `runner` to the nightly
    `Runner`; pass fakes in tests. `now` (UTC) defaults to the current time."""
    now = now or datetime.now(UTC)
    today = today_et(now)
    checks: list[Check] = []

    def _massive():
        from massive_subspace_stream import Store

        return massive if massive is not None else Store()

    store = None
    try:
        store = _massive()
        expected_session = last_trading_day(store, today)
        checks += _guard("massive", "bars", lambda: massive_checks(store, today, now))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("massive", "bars", "missing", detail=f"{type(exc).__name__}: {exc}"))
        expected_session = today - timedelta(days=1)

    def _edgar():
        from edgar_cortical_node import EdgarStore

        return edgar_checks(edgar if edgar is not None else EdgarStore(), today)

    def _finra():
        from finra_sensor_array import FinraStore

        return finra_checks(finra if finra is not None else FinraStore(), today, expected_session)

    def _vinculum():
        from vinculum import Vinculum

        return vinculum_checks(vinculum if vinculum is not None else Vinculum(), expected_session)

    def _nanoprobe():
        from nanoprobe_array import Nanoprobe

        probe = nanoprobe if nanoprobe is not None else Nanoprobe(store=store)
        return nanoprobe_checks(probe, expected_session)

    def _risk():
        from borg_queen import RiskModel

        return risk_checks(risk if risk is not None else RiskModel(), today)

    def _adaptation():
        from adaptation_matrix import AdaptationMatrix

        return adaptation_checks(adaptation if adaptation is not None else AdaptationMatrix(store=store))

    def _conduit():
        from transwarp_conduit import load_settings

        root = Path(conduit_root) if conduit_root is not None else load_settings().data_root()
        return conduit_checks(root)

    checks += _guard("edgar", "facts", _edgar)
    checks += _guard("finra", "short", _finra)
    checks += _guard("vinculum", "market_cap", _vinculum)
    checks += _guard("nanoprobe", "summaries", _nanoprobe)
    checks += _guard("borg-queen", "risk_model", _risk)

    def _nightly():
        from ._runner import Runner

        return nightly_checks(runner if runner is not None else Runner(), now)

    checks += _guard("adaptation-matrix", "costs", _adaptation)
    checks += _guard("transwarp-conduit", "orders", _conduit)
    checks += _guard("unicomplex", "nightly_run", _nightly)
    return checks


def render(checks: list[Check], now: datetime | None = None) -> str:
    """A fixed-width table, one check per line, with a one-line verdict."""
    now = now or datetime.now(UTC)
    rows = [("source", "check", "state", "last", "expected", "detail")]
    rows += [(c.source, c.name, c.state, c.last or "-", c.expected or "-", c.detail) for c in checks]
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    lines = ["  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r[:5])) + "  " + r[5] for r in rows]
    bad = [c for c in checks if c.state in ("stale", "missing")]
    verdict = (f"{len(bad)} check(s) need attention: " + ", ".join(f"{c.source}/{c.name}" for c in bad)
               if bad else "everything is as fresh as the schedule allows")
    stamp = now.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    return "\n".join([f"collective status at {stamp}", *lines, verdict])


def to_json(checks: list[Check]) -> str:
    return json.dumps([c.to_dict() for c in checks], indent=1)
