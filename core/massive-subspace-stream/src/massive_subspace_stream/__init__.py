"""massive-subspace-stream: Massive US equity data for the Collective.

Public API (import only these from other packages):

    Store          query stored data into polars or pandas DataFrames
    Settings       resolved configuration
    load_settings  build Settings from collective.toml + environment
    Ingestor       programmatic backfills / sync (the CLI wraps it)
    StreamService  programmatic live stream (the CLI wraps it)
    ConfigError    raised for missing settings or credentials
    BusyError      another job is writing to the data root (nothing was done)
"""

from ._config import ConfigError, Settings, load_settings
from ._ingest.backfill import BusyError, Ingestor, JobResult
from ._ingest.stream import StreamService
from ._store import Store

__all__ = ["BusyError", "ConfigError", "Ingestor", "JobResult", "Settings", "Store", "StreamService", "load_settings"]
__version__ = "0.1.0"
