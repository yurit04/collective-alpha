"""finra-sensor-array: FINRA short interest and daily short sale volume (free public data).

Public API:
    FinraStore(root=None, settings=None)   reads: short_interest(), short_volume(), scan(), status()
    Ingestor(settings)                     .short_interest(start, end), .short_volume(start, end), .sync()
    load_settings(), Settings, ConfigError, JobResult
    finra_symbols(tickers, dataset)        Massive-style tickers -> FINRA symbols (BRK.B -> BRKB / BRK/B)
"""

from ._config import ConfigError, Settings, load_settings
from ._ingest import Ingestor, JobResult
from ._parse import finra_symbols
from ._store import FinraStore

__all__ = ["ConfigError", "FinraStore", "Ingestor", "JobResult", "Settings", "finra_symbols", "load_settings"]
