"""edgar-cortical-node: SEC EDGAR XBRL company facts, kept per filing, served point in time by CIK.

Public API:
    EdgarStore(root=None, settings=None)   reads (facts, series, shares, as_of, companies)
    Ingestor(settings)                     .bulk(), .sync(), .refresh_ciks(ciks)
    load_settings(), Settings, ConfigError, JobResult
    CONCEPTS                               short item names -> tags in priority order
"""

from ._concepts import CONCEPTS, Concept
from ._config import ConfigError, Settings, load_settings
from ._ingest import Ingestor, JobResult
from ._store import EdgarStore

__all__ = ["CONCEPTS", "Concept", "ConfigError", "EdgarStore", "Ingestor", "JobResult", "Settings",
           "load_settings"]
