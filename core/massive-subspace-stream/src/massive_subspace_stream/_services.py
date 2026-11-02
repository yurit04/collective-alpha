"""macOS launchd services: the daily sync (Tue-Sat morning) and the always-on live stream.

Credentials are not written into the plist: the services read the env file
(`env_file` setting, default ~/.massive.env) like the CLI does.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from ._config import Settings
from ._layout import Layout

SERVICE_ENV = "SUBSPACE_SERVICE"  # set for processes launchd starts
LABELS = {"sync": "com.collective-alpha.subspace.sync", "stream": "com.collective-alpha.subspace.stream"}
AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()


def subspace_executable() -> str:
    candidate = Path(sys.executable).parent / "subspace"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("subspace")
    if not found:
        raise RuntimeError("cannot find the `subspace` executable; run `uv sync --all-packages` first")
    return found


def plist_path(name: str) -> Path:
    return AGENTS_DIR / f"{LABELS[name]}.plist"


def build_plist(name: str, settings: Settings) -> dict:
    layout = Layout(settings.data_root)
    args = [subspace_executable()] + (["sync"] if name == "sync" else ["stream", "run"])
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SUBSPACE_DATA_ROOT": str(layout.root), SERVICE_ENV: "1"}
    if settings.config_file:
        env["COLLECTIVE_CONFIG"] = str(Path(settings.config_file).resolve())
    plist = {
        "Label": LABELS[name],
        "ProgramArguments": args,
        "EnvironmentVariables": env,
        "WorkingDirectory": str(layout.root),
        "StandardOutPath": str(layout.logs_dir / f"{name}.launchd.log"),
        "StandardErrorPath": str(layout.logs_dir / f"{name}.launchd.log"),
        "ProcessType": "Background",
    }
    if name == "sync":
        # Flat files for day D appear overnight; Tue-Sat covers Mon-Fri sessions.
        plist["StartCalendarInterval"] = [
            {"Weekday": wd, "Hour": settings.sync_hour, "Minute": settings.sync_minute} for wd in range(2, 7)
        ]
    else:
        plist["RunAtLoad"] = True
        # Restart after crashes and dropped connections, not after a clean stop (refused login).
        plist["KeepAlive"] = {"SuccessfulExit": False}
        plist["ThrottleInterval"] = 30
    return plist


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(name: str, settings: Settings) -> Path:
    Layout(settings.data_root).ensure()
    path = plist_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    if path.exists():
        _launchctl("bootout", f"{domain}/{LABELS[name]}")
    path.write_bytes(plistlib.dumps(build_plist(name, settings)))
    result = _launchctl("bootstrap", domain, str(path))
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
    return path


def remove(name: str) -> bool:
    path = plist_path(name)
    _launchctl("bootout", f"gui/{os.getuid()}/{LABELS[name]}")
    if path.exists():
        path.unlink()
        return True
    return False


def status(name: str) -> dict:
    result = _launchctl("print", f"gui/{os.getuid()}/{LABELS[name]}")
    info = {"installed": plist_path(name).exists(), "loaded": result.returncode == 0}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(("state =", "pid =", "last exit code =")):
            key, _, value = line.partition(" = ")
            info.setdefault(key.replace(" ", "_"), value)  # first match is the job itself
    return info
