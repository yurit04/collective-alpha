"""macOS launchd job for the daily EDGAR sync (Tue-Sat morning, after the SEC's nightly files)."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from ._config import Settings
from ._layout import Layout

LABEL = "com.collective-alpha.cortical.sync"
AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()


def cortical_executable() -> str:
    candidate = Path(sys.executable).parent / "cortical"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("cortical")
    if not found:
        raise RuntimeError("cannot find the `cortical` executable; run `uv sync --all-packages` first")
    return found


def plist_path() -> Path:
    return AGENTS_DIR / f"{LABEL}.plist"


def build_plist(settings: Settings) -> dict:
    layout = Layout(settings.data_root)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "EDGAR_DATA_ROOT": str(layout.root)}
    if settings.config_file:
        env["COLLECTIVE_CONFIG"] = str(Path(settings.config_file).resolve())
    return {
        "Label": LABEL,
        "ProgramArguments": [cortical_executable(), "sync"],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(layout.root),
        "StandardOutPath": str(layout.logs_dir / "sync.launchd.log"),
        "StandardErrorPath": str(layout.logs_dir / "sync.launchd.log"),
        "ProcessType": "Background",
        "StartCalendarInterval": [{"Weekday": wd, "Hour": settings.sync_hour, "Minute": settings.sync_minute}
                                  for wd in range(2, 7)],
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(settings: Settings) -> Path:
    Layout(settings.data_root).ensure()
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    if path.exists():
        _launchctl("bootout", f"{domain}/{LABEL}")
    path.write_bytes(plistlib.dumps(build_plist(settings)))
    result = _launchctl("bootstrap", domain, str(path))
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
    return path


def remove() -> bool:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if plist_path().exists():
        plist_path().unlink()
        return True
    return False


def status() -> dict:
    result = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    info = {"installed": plist_path().exists(), "loaded": result.returncode == 0}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(("state =", "pid =", "last exit code =")):
            key, _, value = line.partition(" = ")
            info.setdefault(key.replace(" ", "_"), value)
    return info
