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
    config_file: Path | None = None


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
    return int(value)


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
    return settings
