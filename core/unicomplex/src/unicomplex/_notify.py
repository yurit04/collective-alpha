"""Tell the user when a nightly run needs attention: a macOS notification at the end of a full
run that failed, finished after `ready_by`, or left a store stale or missing.

Only full runs notify (the launchd job runs every step; a partial run by hand is watched).
Nothing is sent when all is well. The notification goes through `osascript`, so it needs no
credentials and shows under Script Editor in System Settings > Notifications.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import datetime

from ._checks import Check, ran_late
from ._config import Settings

log = logging.getLogger("unicomplex.notify")

TITLE = "collective run"
MAX_LINES = 4   # a notification shows about this many lines


def problems(res, checks: Iterable[Check], settings: Settings, tz=None) -> list[str]:
    """What needs attention after a run (`res` a RunResult), most important first; [] when nothing."""
    out = []
    for st in res.steps:
        if st.status not in ("ok", "skipped"):
            out.append(f"{st.name} {st.status}" + (f": {st.detail}" if st.detail else ""))
    if res.started and res.finished:
        started, finished = datetime.fromisoformat(res.started), datetime.fromisoformat(res.finished)
        if ran_late(started, finished, settings, tz):
            out.append(f"finished {finished.astimezone(tz):%H:%M}, after the {settings.ready_by} ready-by time")
    for c in checks:
        if c.state in ("stale", "missing") and (c.source, c.name) != ("unicomplex", "nightly_run"):
            out.append(f"{c.source} {c.name} {c.state}" + (f": {c.detail}" if c.detail else ""))
    return out


def osascript(title: str, message: str) -> bool:
    """Show a macOS notification; False where there is no osascript (Linux) or it failed."""
    exe = shutil.which("osascript") if sys.platform == "darwin" else None
    if not exe:
        return False
    script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
              "-e", "end run"]
    try:
        subprocess.run([exe, *script, title, message], check=True, capture_output=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("notification failed: %s", exc)
        return False
    return True


def after_run(res, settings: Settings, collect: Callable[[], list[Check]] | None = None,
              send: Callable[[str, str], bool] = osascript, echo: Callable[[str], None] | None = None,
              tz=None) -> list[str]:
    """Check a finished run and notify when something needs attention. Returns the problems.
    `collect` gives the freshness checks (default: `_checks.collect()`)."""
    if not res.full or res.status in ("dry-run", "skipped", "locked"):
        return []
    try:
        from ._checks import collect as collect_all

        checks = (collect or collect_all)()
    except Exception as exc:  # noqa: BLE001 - a broken status check must not hide the run's result
        checks = [Check("unicomplex", "status", "missing", detail=f"{type(exc).__name__}: {exc}")]
    found = problems(res, checks, settings, tz)
    if echo is not None:
        for line in found:
            echo(f"attention: {line}")
    if found and settings.notify:
        lines = found[:MAX_LINES] + ([f"... and {len(found) - MAX_LINES} more (collective status)"]
                                     if len(found) > MAX_LINES else [])
        send(f"{TITLE}: {len(found)} problem(s)", "\n".join(lines))
    return found
