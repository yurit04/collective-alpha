"""Settings: where EDGAR data lives and how to reach the SEC.

Resolution order (later wins): defaults, the `[edgar_cortical_node]` section of
`collective.toml`, environment variables (`EDGAR_DATA_ROOT`, `EDGAR_USER_AGENT`), keyword
overrides. The SEC needs no credentials, only a User-Agent with a contact address.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

SECTION = "edgar_cortical_node"
CONFIG_FILENAME = "collective.toml"
PLACEHOLDER_AGENT = "collective-alpha research@collective-alpha.invalid"


class ConfigError(RuntimeError):
    """Raised when settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    data_root: Path = field(default_factory=lambda: Path("~/market_data/edgar").expanduser())
    # The SEC rejects requests without a contact address in the User-Agent. Set your own in
    # collective.toml (outside the repo) if you like; the placeholder is accepted.
    user_agent: str = PLACEHOLDER_AGENT
    requests_per_second: float = 8.0    # SEC limit is 10
    workers: int = 8                    # parallel company-facts requests
    parse_workers: int = 8              # processes for the bulk conversion
    bulk_refresh_days: int = 30         # re-load the full bulk file this often
    sync_hour: int = 7
    sync_minute: int = 0
    config_file: Path | None = None


_ENV = {"EDGAR_DATA_ROOT": "data_root", "EDGAR_USER_AGENT": "user_agent"}


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
    known = {f.name for f in fields(Settings)}
    if name not in known:
        raise ConfigError(f"Unknown setting '{name}' in [{SECTION}]")
    if name in ("data_root", "config_file"):
        return Path(str(value)).expanduser() if value not in (None, "") else None
    if name in ("requests_per_second",):
        return float(value)
    if name in ("workers", "parse_workers", "bulk_refresh_days", "sync_hour", "sync_minute"):
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
    if "@" not in settings.user_agent:
        raise ConfigError("user_agent must contain a contact e-mail address (the SEC requires one)")
    return settings
