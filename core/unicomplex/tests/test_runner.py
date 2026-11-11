"""`collective run` with fake commands, clock and sleep: order, failures, the lock, busy
retries, dry runs, skip-if-recent; the nightly status check; the launchd plist and the
refusal to install next to the per-package schedules."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from unicomplex import STEPS, Runner, Settings, Step, _services
from unicomplex._checks import last_scheduled, nightly_checks
from unicomplex._runner import StepTimeout

ET = ZoneInfo("America/New_York")
START = datetime(2026, 11, 6, 11, 30, tzinfo=UTC)  # Friday 06:30 ET


@pytest.fixture(autouse=True)
def _own_root(tmp_path_factory, monkeypatch):
    """Never read or write the real unicomplex root (run history, lock) from a test."""
    monkeypatch.setenv("UNICOMPLEX_DATA_ROOT", str(tmp_path_factory.mktemp("unicomplex")))


class Clock:
    def __init__(self, start=START):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Fake:
    """Records each command; exit codes per step name (a list is consumed one per attempt)."""

    def __init__(self, clock, codes=None, seconds=60):
        self.clock, self.codes, self.seconds, self.calls = clock, dict(codes or {}), seconds, []
        self.timeouts = {}

    def __call__(self, argv, out, timeout=None):
        name = Path(argv[0]).name
        self.calls.append([name, *argv[1:]])
        self.timeouts[name] = timeout
        out(f"{name} working")
        self.clock.advance(self.seconds)
        code = self.codes.get(name, 0)
        if isinstance(code, list):
            return code.pop(0) if len(code) > 1 else code[0]
        return code


def make(tmp_path, codes=None, steps=STEPS, **settings):
    clock = Clock()
    fake = Fake(clock, codes)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.advance(s)

    runner = Runner(Settings(data_root=tmp_path, **settings), steps=steps, execute=fake, clock=clock,
                    sleep=sleep, resolve=lambda name: f"/bin/{name}")
    return runner, fake, clock, sleeps


def test_runs_every_step_in_order_and_records_it(tmp_path):
    runner, fake, _, _ = make(tmp_path)
    lines = []
    res = runner.run(echo=lines.append)
    assert res.status == "ok" and res.exit_code == 0
    assert fake.calls == [["subspace", "sync"], ["cortical", "sync"], ["sensor", "sync"],
                          ["vinculum", "build"], ["nanoprobe", "build"], ["queen", "build"]]
    log = Path(res.log).read_text()
    assert "subspace working" in log and "queen: ok in 60s" in log
    assert "subspace working" in lines                       # not quiet: step output is echoed
    [row] = runner.history()
    assert row["status"] == "ok" and row["full"] and row["failed"] == []
    assert [s["seconds"] for s in row["steps"]] == [60.0] * 6
    assert runner.running() is None                          # the lock was released


def test_quiet_echoes_progress_only(tmp_path):
    runner, _, _, _ = make(tmp_path)
    lines = []
    runner.run(echo=lines.append, quiet=True)
    assert not any("working" in x for x in lines) and any("queen: ok" in x for x in lines)


def test_a_failed_step_does_not_stop_the_rest(tmp_path):
    runner, fake, _, _ = make(tmp_path, codes={"cortical": 1})
    res = runner.run()
    assert len(fake.calls) == 6                               # vinculum still rebuilt
    assert res.status == "failed" and res.exit_code == 1 and res.failed == ["edgar"]
    assert runner.last()["failed"] == ["edgar"]


def test_missing_executable_fails_that_step(tmp_path):
    runner, fake, _, _ = make(tmp_path)
    runner.resolve = lambda name: None if name == "nanoprobe" else f"/bin/{name}"
    res = runner.run()
    assert res.failed == ["nanoprobe"] and res.steps[4].exit_code == 127
    assert ["queen", "build"] in fake.calls


def test_busy_step_is_retried_then_succeeds(tmp_path):
    runner, fake, _, sleeps = make(tmp_path, codes={"subspace": [2, 2, 0]})
    res = runner.run()
    assert res.status == "ok"
    assert res.steps[0].attempts == 3 and sleeps == [300, 300]
    assert res.steps[0].seconds == 60 * 3 + 600


def test_busy_step_gives_up_after_the_wait(tmp_path):
    runner, fake, _, sleeps = make(tmp_path, codes={"subspace": 2}, busy_retry_minutes=5, busy_wait_minutes=15)
    res = runner.run()
    assert res.steps[0].status == "busy" and res.steps[0].attempts == 4 and len(sleeps) == 3
    assert res.failed == ["massive"] and len(fake.calls) == 4 + 5   # the other steps still ran


def test_busy_code_only_counts_for_the_step_that_has_one(tmp_path):
    runner, _, _, sleeps = make(tmp_path, codes={"cortical": 2})
    res = runner.run()
    assert res.failed == ["edgar"] and sleeps == [] and res.steps[1].attempts == 1


def test_only_and_skip(tmp_path):
    runner, fake, _, _ = make(tmp_path)
    res = runner.run(only=["vinculum", "massive"])
    assert fake.calls == [["subspace", "sync"], ["vinculum", "build"]] and not res.full
    fake.calls.clear()
    runner.run(skip=["queen"], force=True)
    assert [c[0] for c in fake.calls] == ["subspace", "cortical", "sensor", "vinculum", "nanoprobe"]
    with pytest.raises(ValueError, match="unknown step"):
        runner.run(only=["bogus"])
    assert runner.last(full=True) is None                     # neither was a full run


def test_dry_run_runs_and_writes_nothing(tmp_path):
    root = tmp_path / "uc"
    runner, fake, _, _ = make(root)
    res = runner.run(dry_run=True)
    assert res.status == "dry-run" and fake.calls == [] and not root.exists()
    assert res.steps[0].command == ["/bin/subspace", "sync"]


def test_skips_when_a_full_run_succeeded_recently(tmp_path):
    runner, fake, clock, _ = make(tmp_path)
    runner.run()
    clock.advance(3600)
    fake.calls.clear()
    res = runner.run()
    assert res.status == "skipped" and res.exit_code == 0 and fake.calls == []
    assert runner.run(force=True).status == "ok" and len(fake.calls) == 6
    clock.advance(4 * 3600)
    fake.calls.clear()
    assert runner.run().status == "ok"                       # older than 3 hours: runs


def test_a_failed_run_is_not_skipped(tmp_path):
    runner, fake, clock, _ = make(tmp_path, codes={"sensor": 1})
    runner.run()
    clock.advance(600)
    fake.codes = {}
    assert runner.run().status == "ok"


def test_second_runner_exits_while_the_lock_is_held(tmp_path):
    runner, fake, _, _ = make(tmp_path)
    seen = {}

    def nested(argv, out, timeout=None):
        if not seen:
            other, _, _, _ = make(tmp_path)
            seen["held"] = other.running()
            seen["res"] = other.run()
        return fake(argv, out)

    runner.execute = nested
    assert runner.run().status == "ok"
    assert seen["held"]["steps"][0] == "massive" and seen["held"]["pid"]
    assert seen["res"].status == "locked" and seen["res"].exit_code == 0
    assert len(runner.history()) == 1                        # the refused run records nothing


def test_lock_holds_across_processes(tmp_path):
    runner, _, _, _ = make(tmp_path)
    code = (
        "import sys, time\n"
        "from unicomplex import Runner, Settings\n"
        f"r = Runner(Settings(data_root={str(tmp_path)!r}), execute=lambda a, o, t: (print('in', flush=True), "
        "time.sleep(3), 0)[-1], resolve=lambda n: n, steps=[__import__('unicomplex').Step('x', ('x',))])\n"
        "r.run()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "in"
    try:
        assert runner.running() is not None
        assert runner.run(only=["massive"]).status == "locked"
    finally:
        proc.wait()
    assert runner.running() is None


def test_real_subprocess_execute(tmp_path):
    steps = [Step("yes", (sys.executable, "-c", "print('hello')")),
             Step("no", (sys.executable, "-c", "import sys; sys.exit(3)"))]
    runner = Runner(Settings(data_root=tmp_path), steps=steps, resolve=lambda n: n)
    res = runner.run()
    assert [s.status for s in res.steps] == ["ok", "failed"] and res.steps[1].exit_code == 3
    assert "hello" in Path(res.log).read_text()


def test_interrupted_run_is_recorded(tmp_path):
    runner, fake, _, _ = make(tmp_path)

    def stop(argv, out, timeout=None):
        if Path(argv[0]).name == "sensor":
            raise KeyboardInterrupt
        return fake(argv, out)

    runner.execute = stop
    with pytest.raises(KeyboardInterrupt):
        runner.run()
    row = runner.last()
    assert row["status"] == "interrupted" and row["steps"][2]["status"] == "interrupted"
    assert runner.running() is None


def test_unreadable_history_lines_are_skipped(tmp_path):
    runner, _, _, _ = make(tmp_path)
    runner.run()
    with open(runner.history_path, "a") as fh:
        fh.write('{"run_id": "cut sho')
    assert len(runner.history()) == 1


# ------------------------------------------------------------------ status check
def test_last_scheduled_follows_the_tue_sat_schedule():
    sat = datetime(2026, 11, 7, 12, 0, tzinfo=ET)
    assert last_scheduled(sat, 6, 30, ET) == datetime(2026, 11, 7, 6, 30, tzinfo=ET)
    early = datetime(2026, 11, 7, 6, 40, tzinfo=ET)            # inside the grace period
    assert last_scheduled(early, 6, 30, ET) == datetime(2026, 11, 6, 6, 30, tzinfo=ET)
    monday = datetime(2026, 11, 9, 20, 0, tzinfo=ET)          # no runs Sun/Mon
    assert last_scheduled(monday, 6, 30, ET) == datetime(2026, 11, 7, 6, 30, tzinfo=ET)


def status_of(runner, now):
    [c] = nightly_checks(runner, now, tz=ET)
    return c


def test_status_nightly_run(tmp_path):
    runner, fake, clock, _ = make(tmp_path)
    assert status_of(runner, START).state == "info"           # nothing recorded yet
    runner.run()                                              # Friday 06:30-06:36 ET
    fri_noon = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)
    c = status_of(runner, fri_noon)
    assert c.state == "ok" and "6 steps OK" in c.detail
    sat_noon = datetime(2026, 11, 7, 17, 0, tzinfo=UTC)
    assert status_of(runner, sat_noon).state == "stale"       # Saturday's run never happened
    monday = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)
    assert status_of(runner, monday).state == "stale"         # still Saturday's that is missing

    clock.now = datetime(2026, 11, 7, 11, 30, tzinfo=UTC)
    fake.codes = {"vinculum": 1}
    runner.run()
    c = status_of(runner, sat_noon)
    assert c.state == "stale" and "vinculum" in c.detail and ".log" in c.detail


def test_status_accepts_a_manual_run_shortly_before_the_schedule(tmp_path):
    runner, _, clock, _ = make(tmp_path)
    clock.now = datetime(2026, 11, 7, 9, 45, tzinfo=UTC)     # 04:45 ET, before the 06:30 run
    runner.run()
    assert status_of(runner, datetime(2026, 11, 7, 17, 0, tzinfo=UTC)).state == "ok"


def test_status_says_running_while_the_lock_is_held(tmp_path):
    runner, fake, _, _ = make(tmp_path)
    seen = {}

    def peek(argv, out, timeout=None):
        seen.setdefault("c", status_of(runner, START + timedelta(hours=1)))
        return fake(argv, out)

    runner.execute = peek
    runner.run()
    assert seen["c"].state == "info" and seen["c"].detail.startswith("running now")


def test_partial_runs_do_not_count_as_the_nightly_run(tmp_path):
    runner, _, _, _ = make(tmp_path)
    runner.run(only=["vinculum"])
    assert status_of(runner, datetime(2026, 11, 6, 17, 0, tzinfo=UTC)).state == "info"


# ------------------------------------------------------------------ launchd
def test_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(_services, "executable", lambda name: f"/venv/bin/{name}")
    cfg = tmp_path / "collective.toml"
    cfg.write_text("")
    plist = _services.build_plist(Settings(data_root=tmp_path / "uc", config_file=cfg),
                                  roots={"SUBSPACE_DATA_ROOT": "/data/massive"})
    assert plist["Label"] == "com.collective-alpha.unicomplex.nightly"
    assert plist["ProgramArguments"] == ["/usr/bin/caffeinate", "-i", "-s", "/venv/bin/collective", "run", "--quiet"]
    env = plist["EnvironmentVariables"]
    assert env["SUBSPACE_DATA_ROOT"] == "/data/massive" and env["UNICOMPLEX_DATA_ROOT"].endswith("uc")
    assert env["COLLECTIVE_CONFIG"] == str(cfg.resolve())
    assert not any("KEY" in k or "SECRET" in k for k in env)
    assert [d["Weekday"] for d in plist["StartCalendarInterval"]] == [2, 3, 4, 5, 6]
    assert {(d["Hour"], d["Minute"]) for d in plist["StartCalendarInterval"]} == {(6, 30)}


def test_data_roots_come_from_each_package(monkeypatch, tmp_path):
    monkeypatch.setenv("VINCULUM_DATA_ROOT", str(tmp_path / "v"))
    monkeypatch.setenv("NANOPROBE_DATA_ROOT", str(tmp_path / "n"))
    monkeypatch.delenv("BORG_QUEEN_DATA_ROOT", raising=False)
    roots = _services.data_roots()
    assert roots["VINCULUM_DATA_ROOT"] == str((tmp_path / "v").resolve())
    assert roots["NANOPROBE_DATA_ROOT"] == str((tmp_path / "n").resolve())
    assert "BORG_QUEEN_DATA_ROOT" not in roots
    assert {"SUBSPACE_DATA_ROOT", "EDGAR_DATA_ROOT", "FINRA_DATA_ROOT"} <= set(roots)


def test_install_refuses_next_to_the_old_schedules(tmp_path, monkeypatch):
    monkeypatch.setattr(_services, "AGENTS_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(_services, "_launchctl", lambda *a: calls.append(a))
    (tmp_path / "com.collective-alpha.cortical.sync.plist").write_text("")
    (tmp_path / "com.collective-alpha.subspace.stream.plist").write_text("")
    assert _services.old_installed() == ["edgar sync"]          # the stream is not one of them
    with pytest.raises(RuntimeError, match="--replace"):
        _services.install(Settings(data_root=tmp_path / "uc"))
    assert calls == [] and not _services.plist_path().exists()


def test_replace_removes_old_schedules_through_their_own_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(_services, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(_services, "executable", lambda name: f"/venv/bin/{name}")
    monkeypatch.setattr(_services, "data_roots", lambda: {})
    for label in ("subspace.sync", "vinculum.build", "subspace.stream"):
        (tmp_path / f"com.collective-alpha.{label}.plist").write_text("")
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        label = {"subspace": "subspace.sync", "vinculum": "vinculum.build"}[Path(argv[0]).name]
        (tmp_path / f"com.collective-alpha.{label}.plist").unlink()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(_services.subprocess, "run", fake_run)
    monkeypatch.setattr(_services, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    path, removed = _services.install(Settings(data_root=tmp_path / "uc"), replace=True)
    assert removed == ["massive sync", "vinculum build"]
    assert ran == [["/venv/bin/subspace", "service", "remove", "--only", "sync"],
                   ["/venv/bin/vinculum", "service", "remove"]]
    assert (tmp_path / "com.collective-alpha.subspace.stream.plist").exists()   # stream untouched
    assert path.exists() and json.dumps(_services.old_installed()) == "[]"


# ------------------------------------------------------------------ CLI
def test_cli_run(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from unicomplex import cli

    monkeypatch.setenv("UNICOMPLEX_DATA_ROOT", str(tmp_path / "uc"))
    r = CliRunner().invoke(cli.app, ["run", "--dry-run"])
    assert r.exit_code == 0 and "subspace sync" in r.output and "queen build" in r.output
    assert not (tmp_path / "uc").exists()
    r = CliRunner().invoke(cli.app, ["run", "--only", "bogus"])
    assert r.exit_code == 2 and "unknown step" in r.output
    r = CliRunner().invoke(cli.app, ["runs"])
    assert r.exit_code == 0 and "no runs recorded yet" in r.output


def test_each_step_gets_its_time_limit_and_settings_override_it(tmp_path):
    runner, fake, _, _ = make(tmp_path, step_timeout_minutes={"edgar": 0, "queen": 5})
    runner.run()
    assert fake.timeouts["subspace"] == 240 * 60 and fake.timeouts["cortical"] is None
    assert fake.timeouts["queen"] == 5 * 60
    with pytest.raises(ValueError, match="unknown step"):
        Runner(Settings(data_root=tmp_path, step_timeout_minutes={"edgr": 10}))


def test_a_step_past_its_limit_fails_and_the_run_goes_on(tmp_path):
    runner, fake, _, _ = make(tmp_path)

    def slow_edgar(argv, out, timeout=None):
        if Path(argv[0]).name == "cortical":
            raise StepTimeout(timeout / 60, -15)
        return fake(argv, out, timeout)

    runner.execute = slow_edgar
    res = runner.run()
    edgar = res.steps[1]
    assert edgar.status == "timeout" and "240 min" in edgar.detail and edgar.attempts == 1
    assert [s.status for s in res.steps[2:]] == ["ok"] * 4
    assert res.status == "failed" and res.failed == ["edgar"] and res.exit_code == 1


def test_real_subprocess_timeout_stops_the_whole_process_group(tmp_path):
    """The step and the worker it spawned are both stopped (spawned build workers would
    otherwise keep running after the step was given up)."""
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, time\n"
            f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
            "print('started', flush=True)\n"
            "time.sleep(60)\n")
    steps = [Step("slow", (sys.executable, "-c", code), timeout_minutes=0.02),   # 1.2 s
             Step("next", (sys.executable, "-c", "print('next ran')"))]
    runner = Runner(Settings(data_root=tmp_path), steps=steps, resolve=lambda n: n)
    res = runner.run()
    assert [s.status for s in res.steps] == ["timeout", "ok"]
    log = Path(res.log).read_text()
    assert "started" in log and "next ran" in log and "TIMEOUT" in log
    child = int(pidfile.read_text())
    import os
    import time
    for _ in range(50):                      # the child is reaped by init; give it a moment
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        raise AssertionError("the spawned worker outlived its step")


def test_status_flags_a_run_that_finished_after_ready_by(tmp_path):
    runner, fake, clock, _ = make(tmp_path)
    fake.seconds = 2 * 3600                                    # 6 steps x 2 h: 06:30 -> 18:30 ET
    runner.run()
    c = status_of(runner, datetime(2026, 11, 6, 23, 30, tzinfo=UTC))
    assert c.state == "stale" and "18:30" in c.detail and "09:00 ready-by" in c.detail


def test_status_accepts_a_late_manual_run_started_after_ready_by(tmp_path):
    runner, _, clock, _ = make(tmp_path, skip_if_ok_hours=12)
    clock.now = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)      # 14:00 ET, by hand
    runner.run()
    assert status_of(runner, datetime(2026, 11, 6, 21, 0, tzinfo=UTC)).state == "ok"


def test_status_flags_a_run_still_going_past_ready_by(tmp_path):
    runner, fake, _, _ = make(tmp_path)
    seen = {}

    def peek(argv, out, timeout=None):
        seen.setdefault("early", status_of(runner, START + timedelta(hours=1)))
        seen.setdefault("late", status_of(runner, START + timedelta(hours=4)))  # 10:30 ET
        return fake(argv, out, timeout)

    runner.execute = peek
    runner.run()
    assert seen["early"].state == "info"
    assert seen["late"].state == "stale" and "still running past the 09:00" in seen["late"].detail


def test_after_run_notifies_only_when_something_needs_attention(tmp_path):
    from unicomplex._checks import Check
    from unicomplex._notify import after_run

    sent = []
    runner, fake, clock, _ = make(tmp_path)
    ok = runner.run()
    fresh = [Check("massive", "bars_day", "ok"), Check("unicomplex", "nightly_run", "ok")]
    assert after_run(ok, runner.settings, collect=lambda: fresh, send=lambda t, m: sent.append((t, m)), tz=ET) == []
    assert sent == []

    clock.now = datetime(2026, 11, 7, 11, 30, tzinfo=UTC)
    fake.codes, fake.seconds = {"cortical": 1}, 2 * 3600       # edgar fails and the run ends 18:30
    bad = runner.run()
    stale = [*fresh, Check("finra", "short_volume", "stale", detail="last 2026-11-05")]
    found = after_run(bad, runner.settings, collect=lambda: stale, send=lambda t, m: sent.append((t, m)), tz=ET)
    assert found == ["edgar failed: exit code 1", "finished 18:30, after the 09:00 ready-by time",
                     "finra short_volume stale: last 2026-11-05"]
    [(title, message)] = sent
    assert "3 problem(s)" in title and message.splitlines() == found

    partial = runner.run(only=["finra"], force=True)
    assert after_run(partial, runner.settings, collect=lambda: stale, send=lambda t, m: sent.append((t, m))) == []
    quiet = Settings(data_root=tmp_path, notify=False)
    assert after_run(bad, quiet, collect=lambda: stale, send=lambda t, m: sent.append((t, m)), tz=ET)
    assert len(sent) == 1


def test_ready_by_must_follow_the_run_time(tmp_path):
    from unicomplex import ConfigError, load_settings

    cfg = tmp_path / "collective.toml"
    cfg.write_text('[unicomplex]\nready_by = "06:00"\n')
    with pytest.raises(ConfigError, match="after the run time"):
        load_settings(cfg)
    cfg.write_text('[unicomplex]\nready_by = "10:15"\nnotify = false\n[unicomplex.step_timeout_minutes]\nedgar = 300\n')
    s = load_settings(cfg)
    assert s.ready_by == "10:15" and s.notify is False and s.step_timeout_minutes == {"edgar": 300.0}
