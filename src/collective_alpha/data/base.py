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

    def close(self) -> None:  # noqa: B027
        """Release network/database handles."""

    # Optional capabilities; a provider implements the ones its vendor offers.
    def sync_reference(self, asof: dt.date) -> None:
        raise NotImplementedError(f"{self.name} has no reference data")

    def sync_corporate_actions(self, asof: dt.date) -> None:
        raise NotImplementedError(f"{self.name} has no corporate actions")

    def sync_bars(self, dataset: str, start: dt.date, end: dt.date) -> None:
        raise NotImplementedError(f"{self.name} has no bars")
