"""The nightly run: every package's sync or build command, one after another, under one lock,
with one log per run and one history line per run.

Each step is the package's own CLI started as a subprocess, so a step behaves exactly as it
does by hand and nothing here reimplements a package. A failed step does not stop the later
ones (vinculum rebuilds from whatever is stored), but the run counts as failed. A step that
exits with its `busy_exit` code (another job holds that package's writer lock) is retried
every `busy_retry_minutes` for up to `busy_wait_minutes`.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ._config import Settings, load_settings


@dataclass(frozen=True)
class Step:
    name: str                      # short name, used by --only / --skip
    command: tuple[str, ...]       # executable then arguments
    busy_exit: int | None = None   # exit code meaning "another job holds the writer lock"
    note: str = ""


STEPS: tuple[Step, ...] = (
    Step("massive", ("subspace", "sync"), busy_exit=2,
         note="bars, flat files, reference data and the security master through yesterday"),
    Step("edgar", ("cortical", "sync"), note="SEC company facts filed since the last sync"),
    Step("finra", ("sensor", "sync"), note="FINRA short interest and daily short sale volume"),
    Step("vinculum", ("vinculum", "build"), note="market cap, universes and the joins (runs even if a sync failed)"),
    Step("nanoprobe", ("nanoprobe", "build"), note="intraday summaries for the years behind the minute bars"),
    Step("queen", ("queen", "build"), note="the risk model, rebuilt from 2011"),
)
EXIT_NOT_FOUND = 127


@dataclass
class StepResult:
    name: str
    command: list[str]
    status: str = "pending"         # ok | failed | busy | interrupted | skipped
    exit_code: int | None = None
    started: str | None = None
    seconds: float = 0.0
    attempts: int = 0
    detail: str = ""


@dataclass
class RunResult:
    """status: ok, failed, skipped (a full run succeeded recently), locked (another run holds
    the lock) or dry-run. `exit_code` is 1 only when a step failed."""

    status: str
    run_id: str | None = None
    started: str | None = None
    finished: str | None = None
    full: bool = True
    steps: list[StepResult] = field(default_factory=list)
    log: str | None = None
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return 1 if self.status in ("failed", "interrupted") else 0

    @property
    def failed(self) -> list[str]:
        return [s.name for s in self.steps if s.status not in ("ok", "skipped")]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["failed"] = self.failed
        return d


def executable(name: str) -> str | None:
    """The workspace's own script next to this interpreter, else whatever is on PATH."""
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    return shutil.which(name)


def _subprocess(argv: list[str], out: Callable[[str], None]) -> int:
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            out(line.rstrip("\n"))
        return proc.wait()
    except BaseException:
        proc.terminate()
        proc.wait()
        raise


class Runner:
    """`execute(argv, out) -> exit code` runs one command and passes each output line to `out`;
    `clock()` returns an aware datetime; `sleep(seconds)` waits. Tests replace all three."""

    def __init__(self, settings: Settings | None = None, steps: Iterable[Step] = STEPS,
                 execute: Callable[[list[str], Callable[[str], None]], int] | None = None,
                 clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
                 resolve: Callable[[str], str | None] = executable):
        self.settings = settings or load_settings()
        self.steps = tuple(steps)
        self.execute = execute or _subprocess
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.resolve = resolve

    # ------------------------------------------------------------------ paths
    @property
    def root(self) -> Path:
        return Path(self.settings.data_root)

    @property
    def lock_path(self) -> Path:
        return self.root / "_run" / "nightly.lock"

    @property
    def history_path(self) -> Path:
        return self.root / "runs.jsonl"

    def log_path(self, run_id: str) -> Path:
        return self.root / "runs" / f"{run_id}.log"

    # ------------------------------------------------------------------ reads
    def plan(self, only: Iterable[str] | None = None, skip: Iterable[str] | None = None) -> list[Step]:
        """The steps a run would take, in order."""
        names = [s.name for s in self.steps]
        only, skip = list(only or []), list(skip or [])
        unknown = [n for n in only + skip if n not in names]
        if unknown:
            raise ValueError(f"unknown step(s) {', '.join(unknown)}; steps are {', '.join(names)}")
        return [s for s in self.steps if (not only or s.name in only) and s.name not in skip]

    def history(self, n: int | None = None) -> list[dict[str, Any]]:
        """Recorded runs, oldest first (the last `n` if given)."""
        if not self.history_path.exists():
            return []
        rows = []
        for line in self.history_path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line cut short by a crash; the others still count
        return rows[-n:] if n else rows

    def last(self, full: bool = True) -> dict[str, Any] | None:
        """The latest recorded run (only runs of every step when `full`)."""
        rows = [r for r in self.history() if r.get("full") or not full]
        return rows[-1] if rows else None

    def running(self) -> dict[str, Any] | None:
        """The run holding the lock (what it wrote into the lock file), or None."""
        if not self.lock_path.exists():
            return None
        with open(self.lock_path) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    return json.loads(fh.read() or "{}")
                except json.JSONDecodeError:
                    return {}
            fcntl.flock(fh, fcntl.LOCK_UN)
        return None

    # ------------------------------------------------------------------ the run
    @contextmanager
    def _lock(self, info: dict[str, Any]) -> Iterator[bool]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(info).encode())
            os.fsync(fd)
            try:
                yield True
            finally:
                os.ftruncate(fd, 0)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _recent_ok(self, now: datetime) -> dict[str, Any] | None:
        last = self.last(full=True)
        if not last or last.get("status") != "ok" or not last.get("finished"):
            return None
        age = (now - datetime.fromisoformat(last["finished"])).total_seconds() / 3600
        return last if age < self.settings.skip_if_ok_hours else None

    def run(self, only: Iterable[str] | None = None, skip: Iterable[str] | None = None, force: bool = False,
            dry_run: bool = False, echo: Callable[[str], None] | None = None, quiet: bool = False) -> RunResult:
        """Run the steps in order. `echo` gets the progress lines (and, unless `quiet`, every
        line the steps print); the full output always goes to the run's log."""
        echo = echo or (lambda _line: None)
        steps = self.plan(only, skip)
        full = len(steps) == len(self.steps)
        now = self.clock()
        if dry_run:
            res = RunResult("dry-run", full=full, detail="nothing was run")
            for s in steps:
                path = self.resolve(s.command[0])
                res.steps.append(StepResult(s.name, [path or s.command[0], *s.command[1:]], "skipped",
                                            detail=s.note if path else f"cannot find `{s.command[0]}`"))
            return res
        if full and not force and (recent := self._recent_ok(now)):
            return RunResult("skipped", recent.get("run_id"), detail=(
                f"the last full run ({recent.get('run_id')}) succeeded under "
                f"{self.settings.skip_if_ok_hours:g} hours ago; use --force to run anyway"))

        run_id = now.astimezone().strftime("%Y%m%d-%H%M%S")
        info = {"run_id": run_id, "pid": os.getpid(), "host": socket.gethostname(), "started": now.isoformat(),
                "steps": [s.name for s in steps]}
        with self._lock(info) as got:
            if not got:
                held = self.running() or {}
                return RunResult("locked", held.get("run_id"), detail=(
                    f"another run holds the lock ({held.get('run_id', '?')}, pid {held.get('pid', '?')}); exiting"))
            return self._run_locked(run_id, now, steps, full, echo, quiet)

    def _run_locked(self, run_id: str, now: datetime, steps: list[Step], full: bool,
                    echo: Callable[[str], None], quiet: bool) -> RunResult:
        log_path = self.log_path(run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        res = RunResult("running", run_id, now.isoformat(), full=full, log=str(log_path))
        with open(log_path, "a", buffering=1) as log:
            def say(line: str) -> None:
                stamp = self.clock().astimezone().strftime("%Y-%m-%d %H:%M:%S")
                log.write(f"=== {stamp} {line}\n")
                echo(f"=== {stamp} {line}")

            say(f"run {run_id}: {', '.join(s.name for s in steps)}")
            step = None
            try:
                for step in steps:
                    res.steps.append(self._step(step, log, say, echo, quiet))
                res.status = "failed" if res.failed else "ok"
            except KeyboardInterrupt:
                if step is not None and len(res.steps) < steps.index(step) + 1:
                    res.steps.append(StepResult(step.name, list(step.command), "interrupted"))
                res.status = "interrupted"
                say("interrupted")
                raise
            finally:
                res.finished = self.clock().isoformat()
                self._record(res)
                say(f"done: {res.status}" + (f" ({', '.join(res.failed)})" if res.failed else ""))
        return res

    def _step(self, step: Step, log: IO[str], say: Callable[[str], None], echo: Callable[[str], None],
              quiet: bool) -> StepResult:
        start = self.clock()
        path = self.resolve(step.command[0])
        r = StepResult(step.name, list(step.command), started=start.isoformat())
        if path is None:
            r.status, r.exit_code, r.detail = "failed", EXIT_NOT_FOUND, f"cannot find `{step.command[0]}`"
            say(f"{step.name}: FAILED, {r.detail} (run `uv sync --all-packages`)")
            return r

        def out(line: str) -> None:
            log.write(line + "\n")
            if not quiet:
                echo(line)

        argv = [path, *step.command[1:]]
        waited = 0.0
        while True:
            r.attempts += 1
            say(f"{step.name}: {' '.join(step.command)}" + (f" (attempt {r.attempts})" if r.attempts > 1 else ""))
            code = self.execute(argv, out)
            r.exit_code = code
            if step.busy_exit is None or code != step.busy_exit:
                break
            if waited >= self.settings.busy_wait_minutes:
                r.status, r.detail = "busy", f"another job still held the writer lock after {waited:g} min"
                break
            say(f"{step.name}: another job holds the writer lock; retrying in {self.settings.busy_retry_minutes} min")
            self.sleep(self.settings.busy_retry_minutes * 60)
            waited += self.settings.busy_retry_minutes
        r.seconds = round((self.clock() - start).total_seconds(), 1)
        if r.status == "pending":
            r.status = "ok" if r.exit_code == 0 else "failed"
            if r.status == "failed":
                r.detail = f"exit code {r.exit_code}"
        say(f"{step.name}: {r.status.upper() if r.status != 'ok' else 'ok'} in {r.seconds:g}s"
            + (f", {r.detail}" if r.detail else ""))
        return r

    def _record(self, res: RunResult) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a") as fh:
            fh.write(json.dumps(res.to_dict()) + "\n")
