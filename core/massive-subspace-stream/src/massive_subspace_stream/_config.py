"""Settings: where data lives, how to reach Massive, and service defaults.

Resolution order (later wins):
1. Defaults in `Settings`.
2. `[massive_subspace_stream]` section of `collective.toml`.
3. Environment variables (`SUBSPACE_DATA_ROOT`, `MASSIVE_REST_URL`, ...).
4. Keyword overrides passed to `load_settings()`.

Credentials come only from environment variables, or from the env file
(default `~/.massive.env`, lines like `export MASSIVE_API_KEY="..."`). They
are never read from `collective.toml` and never shown in `repr()`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

SECTION = "massive_subspace_stream"
CONFIG_FILENAME = "collective.toml"
FIRST_AVAILABLE_DATE = "2003-09-10"  # earliest Massive stock flat file


class ConfigError(RuntimeError):
    """Raised when settings or credentials are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    data_root: Path = field(default_factory=lambda: Path("~/market_data/massive").expanduser())
    env_file: Path | None = field(default_factory=lambda: Path("~/.massive.env").expanduser())

    rest_url: str = "https://api.massive.com"
    ws_url: str = "wss://socket.massive.com"
    s3_endpoint: str = "https://files.massive.com"
    s3_bucket: str = "flatfiles"

    # Backfill
    backfill_since: str = FIRST_AVAILABLE_DATE
    download_workers: int = 4
    tick_workers: int = 1
    keep_raw: bool = False

    # Live stream
    stream_channels: tuple[str, ...] = ("AM", "A")
    stream_watchlist: tuple[str, ...] = ()
    stream_watchlist_channels: tuple[str, ...] = ("T", "Q")
    stream_flush_seconds: float = 30.0
    keep_live_days: int = 7

    # Daily sync schedule for the launchd service (local time; runs Tue-Sat)
    sync_hour: int = 6
    sync_minute: int = 30

    config_file: Path | None = None

    api_key: str | None = field(default=None, repr=False)
    s3_access_key_id: str | None = field(default=None, repr=False)
    s3_secret_access_key: str | None = field(default=None, repr=False)

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "No Massive API key found. Set MASSIVE_API_KEY in your environment "
                f"or in the env file ({self.env_file})."
            )
        return self.api_key

    def require_s3(self) -> tuple[str, str]:
        if not (self.s3_access_key_id and self.s3_secret_access_key):
            raise ConfigError(
                "No Massive flat-file (S3) credentials found. Set MASSIVE_S3_ACCESS_KEY_ID and "
                f"MASSIVE_S3_SECRET_ACCESS_KEY in your environment or env file ({self.env_file})."
            )
        return self.s3_access_key_id, self.s3_secret_access_key


_ENV_SETTINGS = {
    "SUBSPACE_DATA_ROOT": "data_root",
    "MASSIVE_REST_URL": "rest_url",
    "MASSIVE_WS_URL": "ws_url",
    "MASSIVE_S3_ENDPOINT": "s3_endpoint",
    "MASSIVE_S3_BUCKET": "s3_bucket",
}
_ENV_SECRETS = {
    "api_key": ("MASSIVE_API_KEY", "POLYGON_API_KEY"),
    "s3_access_key_id": ("MASSIVE_S3_ACCESS_KEY_ID",),
    "s3_secret_access_key": ("MASSIVE_S3_SECRET_ACCESS_KEY",),
}
_SECRET_FIELDS = set(_ENV_SECRETS)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse `KEY=value` / `export KEY="value"` lines. Does not touch os.environ."""
    values: dict[str, str] = {}
    if not path or not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


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
    kinds = {f.name: f for f in fields(Settings)}
    if name not in kinds:
        raise ConfigError(f"Unknown setting '{name}' in [{SECTION}]")
    if name in _SECRET_FIELDS:
        raise ConfigError(f"'{name}' is a credential; put it in the environment or env file, not collective.toml")
    if name in ("data_root", "env_file", "config_file"):
        return Path(str(value)).expanduser() if value not in (None, "") else None
    if name.startswith("stream_") and name != "stream_flush_seconds" and isinstance(value, (list, tuple, str)):
        items = value.split(",") if isinstance(value, str) else value
        return tuple(str(v).strip() for v in items if str(v).strip())
    return value


def load_settings(config_file: Path | str | None = None, **overrides: Any) -> Settings:
    """Build Settings from defaults, collective.toml, environment, and overrides."""
    settings = Settings()
    path = Path(config_file).expanduser() if config_file else find_config_file()
    if path is not None:
        data = tomllib.loads(path.read_text()).get(SECTION, {})
        settings = replace(settings, config_file=path, **{k: _coerce(k, v) for k, v in data.items()})

    env_values = {attr: os.environ[var] for var, attr in _ENV_SETTINGS.items() if os.environ.get(var)}
    if env_values:
        settings = replace(settings, **{k: _coerce(k, v) for k, v in env_values.items()})

    if overrides:
        secret_overrides = {k: overrides.pop(k) for k in list(overrides) if k in _SECRET_FIELDS}
        settings = replace(settings, **{k: _coerce(k, v) for k, v in overrides.items()}, **secret_overrides)

    file_values = parse_env_file(settings.env_file) if settings.env_file else {}
    secrets = {}
    for attr, names in _ENV_SECRETS.items():
        if getattr(settings, attr):
            continue
        for var in names:
            value = os.environ.get(var) or file_values.get(var)
            if value:
                secrets[attr] = value
                break
    # Non-secret endpoints may also live in the env file.
    for var, attr in _ENV_SETTINGS.items():
        if attr not in env_values and attr not in overrides and var in file_values and attr != "data_root":
            secrets[attr] = file_values[var]
    return replace(settings, **secrets) if secrets else settings
