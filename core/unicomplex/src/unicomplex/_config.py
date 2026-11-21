"""Settings: where unicomplex keeps its run log and lock, and when the nightly run starts.

Resolution order (later wins): defaults, the `[unicomplex]` section of `collective.toml`, the
`UNICOMPLEX_DATA_ROOT` environment variable, keyword overrides. Every other package reads its
own settings; the runner only starts their commands.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

SECTION = "unicomplex"
ENV_ROOT = "UNICOMPLEX_DATA_ROOT"
CONFIG_FILENAME = "collective.toml"


class ConfigError(RuntimeError):
    """Raised when settings are invalid."""


@dataclass(frozen=True)
class Settings:
    data_root: Path = field(default_factory=lambda: Path("~/market_data/unicomplex").expanduser())
    run_hour: int = 6                # nightly run, Tue-Sat, machine local time
    run_minute: int = 30
    busy_retry_minutes: int = 5      # a step refused because another job holds its writer lock
    busy_wait_minutes: int = 120     # ... is retried this long before it counts as failed
    skip_if_ok_hours: float = 3.0    # a full run that succeeded this recently makes `run` a no-op
    step_timeout_minutes: dict[str, float] = field(default_factory=dict)  # per step name; 0 = no limit
    ready_by: str = "09:00"          # a nightly run finishing later (machine local time) is late
    notify: bool = True              # macOS notification when a run fails, is late or leaves stale data
    # The scheduled job also starts at these times (Tue-Sat, machine local time); each start
    # re-runs only what the day's run left failed or deferred, and does nothing when it is all
    # done. () = no catch-ups.
    catch_up_times: tuple[str, ...] = ("08:30", "10:30", "12:30", "14:30", "18:30")
    require_ac: bool = True          # macOS: a scheduled run defers its steps while on battery
    config_file: Path | None = None

    def schedule_times(self) -> list[str]:
        """Every scheduled start as HH:MM: the run time, then the catch-up times."""
        times = [(self.run_hour, self.run_minute), *(hhmm(t, "catch_up_times entry") for t in self.catch_up_times)]
        return [f"{h:02d}:{m:02d}" for h, m in times]


def find_config_file(start: Path | None = None) -> Path | None:
    explicit = os.environ.get("COLLECTIVE_CONFIG")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"COLLECTIVE_CONFIG points to a missing file: {path}")
        return path
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    user = Path("~/.config/collective-alpha").expanduser() / CONFIG_FILENAME
    return user if user.is_file() else None


def _coerce(name: str, value: Any) -> Any:
    if name not in {f.name for f in fields(Settings)}:
        raise ConfigError(f"Unknown setting '{name}' in [{SECTION}]")
    if name in ("data_root", "config_file"):
        return Path(str(value)).expanduser() if value not in (None, "") else None
    if name == "skip_if_ok_hours":
        return float(value)
    if name == "step_timeout_minutes":
        if not isinstance(value, dict):
            raise ConfigError(f"[{SECTION}] step_timeout_minutes must be a table of step name = minutes")
        return {str(k): float(v) for k, v in value.items()}
    if name == "ready_by":
        return str(value)
    if name in ("notify", "require_ac"):
        if not isinstance(value, bool):
            raise ConfigError(f"[{SECTION}] {name} must be true or false")
        return value
    if name == "catch_up_times":
        if isinstance(value, str) or not isinstance(value, list | tuple):
            raise ConfigError(f"[{SECTION}] catch_up_times must be a list of \"HH:MM\" times")
        return tuple(str(v) for v in value)
    return int(value)


def hhmm(value: str, name: str) -> tuple[int, int]:
    """"HH:MM" as (hour, minute); ConfigError naming the setting otherwise."""
    try:
        hour, minute = (int(x) for x in str(value).split(":"))
    except ValueError:
        raise ConfigError(f"{name} {value!r} is not HH:MM") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ConfigError(f"{name} {value!r} is not a time of day")
    return hour, minute


def ready_by_time(settings: Settings) -> tuple[int, int]:
    """`ready_by` as (hour, minute)."""
    return hhmm(settings.ready_by, "ready_by")


def load_settings(config_file: Path | str | None = None, **overrides: Any) -> Settings:
    settings = Settings()
    path = Path(config_file).expanduser() if config_file else find_config_file()
    if path is not None:
        data = tomllib.loads(path.read_text()).get(SECTION, {})
        settings = replace(settings, config_file=path, **{k: _coerce(k, v) for k, v in data.items()})
    if os.environ.get(ENV_ROOT):
        settings = replace(settings, data_root=Path(os.environ[ENV_ROOT]).expanduser())
    if overrides:
        settings = replace(settings, **{k: _coerce(k, v) for k, v in overrides.items()})
    if not (0 <= settings.run_hour < 24 and 0 <= settings.run_minute < 60):
        raise ConfigError(f"run time {settings.run_hour}:{settings.run_minute:02d} is not a time of day")
    if ready_by_time(settings) <= (settings.run_hour, settings.run_minute):
        raise ConfigError(f"ready_by {settings.ready_by} must be after the run time "
                          f"{settings.run_hour}:{settings.run_minute:02d}")
    previous = (settings.run_hour, settings.run_minute)
    for t in settings.catch_up_times:
        at = hhmm(t, "catch_up_times entry")
        if at <= previous:
            raise ConfigError(f"catch_up_times must be later than the run time and each other ({t})")
        previous = at
    if any(v < 0 for v in settings.step_timeout_minutes.values()):
        raise ConfigError("step_timeout_minutes must not be negative")
    return settings
