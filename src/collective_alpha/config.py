"""Settings: defaults < config/settings.toml < environment (CA_*) < CLI overrides."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_FILE = REPO_ROOT / "config" / "settings.toml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CA_", extra="ignore")

    data_root: Path = Path("/Users/yuriturygin/Documents/market_data/massive")
    api_key_file: Path = Path("/Users/yuriturygin/Documents/massive_key.txt")
    s3_key_file: Path = Path("/Users/yuriturygin/Documents/massive_s3_key.txt")

    s3_endpoint: str = "https://files.massive.com"
    s3_bucket: str = "flatfiles"
    rest_base_url: str = "https://api.massive.com"

    history_years: int = 5
    flatfile_datasets: list[str] = Field(default_factory=lambda: ["day_aggs_v1", "minute_aggs_v1"])
    max_workers: int = 6

    # Derived paths -----------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw"

    @property
    def curated_dir(self) -> Path:
        return self.data_root / "curated"

    @property
    def manifest_path(self) -> Path:
        return self.data_root / "manifest.sqlite"

    @property
    def log_dir(self) -> Path:
        return self.data_root / "logs"

    def ensure_dirs(self) -> None:
        for p in (self.raw_dir, self.curated_dir, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)


def _toml_overrides(path: Path = SETTINGS_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


@lru_cache(maxsize=1)
def get_settings(**overrides: Any) -> Settings:
    """Build settings. Explicit kwargs win over env vars, which win over the TOML file."""
    file_values = _toml_overrides()
    # pydantic-settings applies env vars on top of init kwargs only for fields not passed,
    # so feed TOML values as init kwargs and let env override by re-reading them.
    merged = {**file_values, **overrides}
    env_settings = Settings()  # env + defaults
    env_set = env_settings.model_fields_set
    for key in list(merged):
        if key in env_set and key not in overrides:
            merged.pop(key)  # env var takes precedence over TOML
    return Settings(**merged)
