"""Settings: where the cost store lives and how much of it a run may build.

Resolution order (later wins): defaults, the `[adaptation_matrix]` section of `collective.toml`,
environment variables (`ADAPTATION_DATA_ROOT`), keyword overrides. Massive credentials and the
plan come from massive-subspace-stream's own settings.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

SECTION = "adaptation_matrix"
CONFIG_FILENAME = "collective.toml"


class ConfigError(RuntimeError):
    """Raised when settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    data_root: Path = field(default_factory=lambda: Path("~/market_data/adaptation_matrix").expanduser())
    max_store_gb: float = 480.0     # a run stops before the store grows past this
    disk_margin_gb: float = 50.0    # and before free disk space falls below this
    keep_top: int = 1000            # matched trades kept for this many largest companies (vinculum)...
    keep_etfs: int = 100            # ...and this many ETFs by that day's dollar volume
    recent_months: int = 6          # the every-day window that ends at the last session
    chunk_quotes: int = 25_000_000  # quote rows measured at once (memory: ~2.5 GB per 25M)
    config_file: Path | None = None


_ENV = {"ADAPTATION_DATA_ROOT": "data_root"}
_FLOATS = {"max_store_gb", "disk_margin_gb"}
_INTS = {"keep_top", "keep_etfs", "recent_months", "chunk_quotes"}


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
    if name in _FLOATS:
        return float(value)
    if name in _INTS:
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
