"""macOS launchd job for the nightly run (Tue-Sat, `collective run --quiet`).

It replaces the four per-package schedules (Massive sync, EDGAR sync, FINRA sync, vinculum
build), which started at fixed times and could overlap. Installing refuses while any of them
is installed; `replace=True` removes them first through each package's own `service remove`
command. The Massive stream service is never touched.

The plist carries each package's data root, resolved now through that package's public
settings, so the run sees the same stores the CLIs do. Credentials are not written into it:
the packages read their env files as usual.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

from ._config import ENV_ROOT, Settings
from ._runner import executable

LABEL = "com.collective-alpha.unicomplex.nightly"
AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# The schedules this one replaces, and how each package removes its own.
OLD_SERVICES: dict[str, tuple[str, tuple[str, ...]]] = {
    "massive sync": ("com.collective-alpha.subspace.sync", ("subspace", "service", "remove", "--only", "sync")),
    "edgar sync": ("com.collective-alpha.cortical.sync", ("cortical", "service", "remove")),
    "finra sync": ("com.collective-alpha.sensor.sync", ("sensor", "service", "remove")),
    "vinculum build": ("com.collective-alpha.vinculum.build", ("vinculum", "service", "remove")),
}


def plist_path(agents_dir: Path | None = None) -> Path:
    return (agents_dir or AGENTS_DIR) / f"{LABEL}.plist"


def old_installed(agents_dir: Path | None = None) -> list[str]:
    """The per-package schedules still installed (by their plist files)."""
    d = agents_dir or AGENTS_DIR
    return [name for name, (label, _) in OLD_SERVICES.items() if (d / f"{label}.plist").exists()]


def data_roots() -> dict[str, str]:
    """Each package's data-root variable and the root it resolves to right now."""
    import borg_queen
    import edgar_cortical_node
    import finra_sensor_array
    import massive_subspace_stream
    import nanoprobe_array
    import vinculum

    env = {
        "SUBSPACE_DATA_ROOT": massive_subspace_stream.load_settings().data_root,
        "EDGAR_DATA_ROOT": edgar_cortical_node.load_settings().data_root,
        "FINRA_DATA_ROOT": finra_sensor_array.load_settings().data_root,
        "VINCULUM_DATA_ROOT": vinculum.load_settings().data_root,
    }
    for var in (nanoprobe_array.ENV_ROOT, borg_queen.ENV_ROOT):  # no settings file: env or default
        if os.environ.get(var):
            env[var] = os.environ[var]
    return {k: str(Path(v).expanduser().resolve()) for k, v in env.items()}


def build_plist(settings: Settings, roots: dict[str, str] | None = None) -> dict[str, Any]:
    collective = executable("collective")
    if not collective:
        raise RuntimeError("cannot find the `collective` executable; run `uv sync --all-packages` first")
    root = Path(settings.data_root).expanduser().resolve()
    env = {"PATH": PATH, **(data_roots() if roots is None else roots), ENV_ROOT: str(root)}
    if settings.config_file:
        env["COLLECTIVE_CONFIG"] = str(Path(settings.config_file).resolve())
    log = str(root / "_logs" / "nightly.launchd.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [collective, "run", "--quiet"],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(root),
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
        "StartCalendarInterval": [{"Weekday": wd, "Hour": settings.run_hour, "Minute": settings.run_minute}
                                  for wd in range(2, 7)],  # launchd weekdays: 2 = Tuesday ... 6 = Saturday
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def remove_old(agents_dir: Path | None = None) -> list[str]:
    """Remove the per-package schedules through each package's CLI; returns what was removed."""
    removed = []
    for name in old_installed(agents_dir):
        label, command = OLD_SERVICES[name]
        exe = executable(command[0])
        if not exe:
            raise RuntimeError(f"cannot find `{command[0]}` to remove {label}")
        result = subprocess.run([exe, *command[1:]], capture_output=True, text=True)
        if result.returncode != 0 or ((agents_dir or AGENTS_DIR) / f"{label}.plist").exists():
            raise RuntimeError(f"`{' '.join(command)}` did not remove {label}: "
                               f"{result.stderr.strip() or result.stdout.strip()}")
        removed.append(name)
    return removed


def install(settings: Settings, replace: bool = False) -> tuple[Path, list[str]]:
    """Install (or reinstall) the nightly job; returns its plist and the old schedules removed."""
    old = old_installed()
    if old and not replace:
        raise RuntimeError(
            f"the per-package schedules are still installed ({', '.join(old)}); they would run next to "
            "this one. Use `collective service install --replace` to remove them first "
            "(the Massive stream service stays).")
    plist = build_plist(settings)
    removed = remove_old() if old else []
    root = Path(plist["WorkingDirectory"])
    (root / "_logs").mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    if path.exists():
        _launchctl("bootout", f"{domain}/{LABEL}")
    path.write_bytes(plistlib.dumps(plist))
    result = _launchctl("bootstrap", domain, str(path))
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
    return path, removed


def remove() -> bool:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if plist_path().exists():
        plist_path().unlink()
        return True
    return False


def status() -> dict[str, Any]:
    result = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    info: dict[str, Any] = {"installed": plist_path().exists(), "loaded": result.returncode == 0}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(("state =", "pid =", "last exit code =")):
            key, _, value = line.partition(" = ")
            info.setdefault(key.replace(" ", "_"), value)
    info["old_schedules"] = old_installed()
    return info
