"""Settings: defaults, the [transwarp_conduit] section of collective.toml, environment.

No credentials: IB Gateway (or TWS) holds the login; this package only connects to it."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

SECTION = "transwarp_conduit"
ENV_ROOT = "TRANSWARP_DATA_ROOT"
ENV_PREFIX = "TRANSWARP_"
PAPER_PORTS = (4002, 7497)      # IB Gateway / TWS paper defaults
LIVE_PORTS = (4001, 7496)
ORDER_TYPES = ("MOC", "MKT")    # LOC needs a limit price per order, which sheets do not carry


class ConduitError(RuntimeError):
    """A check failed; nothing was sent."""


@dataclass(frozen=True)
class Limits:
    """Order-sheet checks. Weights are fractions of capital."""

    max_weight: float = 0.03           # |post-trade weight| per name
    max_gross: float = 2.1             # sum of |post-trade weights|
    max_net: float = 0.10              # |sum of post-trade weights| (long-short); long-only: set 1.05
    max_turnover: float = 2.5          # sum of |trades| / capital per sheet
    max_order_usd: float = 250_000.0   # |trade value| per order
    max_participation: float = 0.10    # |trade| / 21-day average dollar volume
    min_trade_usd: float = 200.0       # smaller trades are skipped (closing a position never is)
    min_trade_weight: float = 0.0005   # ... or smaller than this x capital


@dataclass(frozen=True)
class ConduitSettings:
    host: str = "127.0.0.1"
    port: int = 4002                    # IB Gateway paper
    client_id: int = 17
    account: str | None = None          # expected IB account id; required for live orders
    live: bool = False                  # allow a live (non-paper) account at all
    capital: float | None = None        # dollars run by the strategy; None: net liquidation value
    order_type: str = "MOC"
    cutoff_minutes: int = 15            # submit at least this long before the close
    contract_max_age_days: int = 30     # re-check a cached contract after this
    timeout: float = 10.0
    root: str | None = None
    limits: Limits = Limits()

    def __post_init__(self):
        kind = str(self.order_type).strip().upper()
        if kind not in ORDER_TYPES:
            raise ConduitError(f"order_type {self.order_type!r} is not supported; use one of {', '.join(ORDER_TYPES)}")
        object.__setattr__(self, "order_type", kind)

    def data_root(self) -> Path:
        return Path(self.root or os.environ.get(ENV_ROOT) or "~/market_data/transwarp_conduit").expanduser()


def find_config_file(start: Path | None = None) -> Path | None:
    """Same lookup as the other packages: COLLECTIVE_CONFIG, collective.toml in this or a parent
    directory, then ~/.config/collective-alpha/collective.toml."""
    explicit = os.environ.get("COLLECTIVE_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "collective.toml").is_file():
            return d / "collective.toml"
    user = Path("~/.config/collective-alpha/collective.toml").expanduser()
    return user if user.is_file() else None


def _coerce(kind: Any, value: Any) -> Any:
    if kind in (int, "int"):
        return int(value)
    if kind in (float, "float", "float | None"):
        return None if value in (None, "", "none") else float(value)
    if kind in (bool, "bool"):
        return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes")
    return value


def load_settings(config_file: Path | str | None = None, **overrides: Any) -> ConduitSettings:
    """Defaults < collective.toml [transwarp_conduit] (and its [transwarp_conduit.limits]) <
    environment (TRANSWARP_PORT, TRANSWARP_ACCOUNT, ...) < keyword overrides."""
    values: dict[str, Any] = {}
    limits: dict[str, Any] = {}
    path = Path(config_file) if config_file else find_config_file()
    if path and path.is_file():
        section = tomllib.loads(path.read_text()).get(SECTION, {})
        limits = dict(section.pop("limits", {}))
        values.update(section)
    kinds = {f.name: f.type for f in fields(ConduitSettings)}
    for name in kinds:
        env = os.environ.get(ENV_PREFIX + name.upper())
        if env is not None and name != "limits":
            values[name] = env
    lim_kinds = {f.name: f.type for f in fields(Limits)}
    limits.update(overrides.pop("limits", {}) or {})
    values.update(overrides)
    unknown = set(values) - set(kinds) | set(limits) - set(lim_kinds)
    if unknown:
        raise ConduitError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    s = ConduitSettings(**{k: _coerce(kinds[k], v) for k, v in values.items()})
    return replace(s, limits=Limits(**{k: _coerce(lim_kinds[k], v) for k, v in limits.items()}))


def is_paper(account: str) -> bool:
    """IB paper account ids start with D (DU..., DF...); live ones with U or F."""
    return account.upper().startswith("D")


def mask(account: str | None) -> str:
    return "?" if not account else account[:2] + "*" * max(0, len(account) - 6) + account[-4:]
