"""Settings: where vinculum keeps its built tables and when the nightly build runs.

Resolution order (later wins): defaults, the `[vinculum]` section of `collective.toml`, the
`VINCULUM_DATA_ROOT` environment variable, keyword overrides. The Massive and EDGAR packages
read their own settings.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

SECTION = "vinculum"
CONFIG_FILENAME = "collective.toml"


class ConfigError(RuntimeError):
    """Raised when settings are invalid."""


@dataclass(frozen=True)
class Settings:
    data_root: Path = field(default_factory=lambda: Path("~/market_data/vinculum").expanduser())
    start_year: int = 2003           # first year of the market-cap table
    build_hour: int = 7              # nightly build, after the Massive (06:30) and EDGAR (07:00) syncs
    build_minute: int = 45
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
    if name in ("start_year", "build_hour", "build_minute"):
        return int(value)
    return value


def load_settings(config_file: Path | str | None = None, **overrides: Any) -> Settings:
    settings = Settings()
    path = Path(config_file).expanduser() if config_file else find_config_file()
    if path is not None:
        data = tomllib.loads(path.read_text()).get(SECTION, {})
        settings = replace(settings, config_file=path, **{k: _coerce(k, v) for k, v in data.items()})
    if os.environ.get("VINCULUM_DATA_ROOT"):
        settings = replace(settings, data_root=Path(os.environ["VINCULUM_DATA_ROOT"]).expanduser())
    if overrides:
        settings = replace(settings, **{k: _coerce(k, v) for k, v in overrides.items()})
    return settings
