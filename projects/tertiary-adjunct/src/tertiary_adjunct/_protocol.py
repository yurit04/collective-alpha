"""The protocol (configs/protocol.toml) and the twice-monthly signal dates."""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT / "configs" / "protocol.toml"


def load(path: Path | str | None = None) -> dict[str, Any]:
    """The protocol as a dict (the toml file, unchanged)."""
    return tomllib.loads(Path(path or CONFIG).read_text())


def signals(protocol: dict[str, Any], groups: list[str] | None = None) -> list[str]:
    """Every signal of the given groups (default: all), in protocol order."""
    g = protocol["signals"]
    return [s for name in (groups or list(g)) for s in g[name]]


def signal_dates(days: list[date], start: date | None = None, end: date | None = None,
                 kinds: tuple[str, ...] = ("month_end", "mid_month")) -> list[date]:
    """Signal dates from a trading calendar: the last trading day of each month ("month_end")
    and the last trading day on or before the 15th ("mid_month"). The calendar must run past
    `end`, so a month's last trading day is known (an exchange calendar does)."""
    days = sorted(days)
    by_month: dict[tuple[int, int], list[date]] = {}
    for d in days:
        by_month.setdefault((d.year, d.month), []).append(d)
    out = set()
    for month in by_month.values():
        if "month_end" in kinds:
            out.add(month[-1])
        if "mid_month" in kinds:
            mid = [d for d in month if d.day <= 15]
            if mid:
                out.add(mid[-1])
    return sorted(d for d in out if (start is None or d >= start) and (end is None or d <= end))
