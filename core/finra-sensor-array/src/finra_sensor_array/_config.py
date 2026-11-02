"""Settings: where FINRA data lives and how politely to fetch it.

Resolution order (later wins): defaults, the `[finra_sensor_array]` section of
`collective.toml`, environment variables (`FINRA_DATA_ROOT`), keyword overrides.
FINRA's public data needs no credentials.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

SECTION = "finra_sensor_array"
CONFIG_FILENAME = "collective.toml"


class ConfigError(RuntimeError):
    """Raised when settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    data_root: Path = field(default_factory=lambda: Path("~/market_data/finra").expanduser())
    user_agent: str = "collective-alpha research"
    requests_per_second: float = 5.0   # shared by all workers
    workers: int = 4
    sync_hour: int = 7                 # launchd daily sync (local), Tue-Sat
    sync_minute: int = 15
    config_file: Path | None = None


_ENV = {"FINRA_DATA_ROOT": "data_root"}


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
    if name == "requests_per_second":
        return float(value)
    if name in ("workers", "sync_hour", "sync_minute"):
        return int(value)
    return value


def load_settings(config_file: Path | str | None = None, **overrides: Any) -> Settings:
    settings = Settings()
    path = Path(config_file).expanduser() if config_file else find_config_file()
    if path is not None:
        data = tomllib.loads(path.read_text()).get(SECTION, {})
        settings = replace(settings, config_file=path, **{k: _coerce(k, v) for k, v in data.items()})
    env = {attr: os.environ[var] for var, attr in _ENV.items() if os.environ.get(var)}
    if env:
        settings = replace(settings, **{k: _coerce(k, v) for k, v in env.items()})
    if overrides:
        settings = replace(settings, **{k: _coerce(k, v) for k, v in overrides.items()})
    return settings
