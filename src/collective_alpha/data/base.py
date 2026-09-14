"""Provider interface. Every data vendor implements this so research code stays vendor-agnostic."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from typing import Any


class DataProvider(ABC):
    name: str

    @abstractmethod
    def probe(self) -> dict[str, Any]:
        """Return a description of what the account is entitled to."""

    @abstractmethod
    def sync_reference(self, asof: dt.date) -> None: ...

    @abstractmethod
    def sync_corporate_actions(self, asof: dt.date) -> None: ...

    @abstractmethod
    def sync_bars(self, dataset: str, start: dt.date, end: dt.date) -> None: ...
