"""vinculum: the link between Massive securities and SEC EDGAR companies.

Validated CIKs per security, split-aware share counts, daily market cap, size universes and
point-in-time fundamentals per security. See CLAUDE.md for the public API.
"""

from ._config import ConfigError, Settings, load_settings
from ._store import RATIOS, SIZE_BUCKETS, Vinculum

__all__ = ["RATIOS", "SIZE_BUCKETS", "ConfigError", "Settings", "Vinculum", "load_settings"]
