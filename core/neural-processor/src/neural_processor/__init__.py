"""neural-processor: point-in-time signals per security over vinculum universes."""

from ._lineage import predecessors, relabel, security_links
from ._processor import Processor, add_lags, resolve, short_signals
from ._sectors import SECTORS, sic_sector
from ._signals import GROUPS, SIGNALS, Signal, catalogue
from ._transforms import composite, neutralize, transform, winsorize

__all__ = ["GROUPS", "SECTORS", "SIGNALS", "Processor", "Signal", "add_lags", "catalogue", "composite",
           "neutralize", "predecessors", "relabel", "resolve", "security_links", "short_signals", "sic_sector",
           "transform", "winsorize"]
