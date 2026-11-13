"""adaptation-matrix: transaction costs measured from Massive trades and NBBO quotes.

Public API:
    AdaptationMatrix(root=None, settings=None, store=None, ingestor=None, vinculum=None)
        reads: daily(), minute(), buckets(), trades(), days(), status()
        work:  schedule(), estimate(), run(), measure_stored()
    load_settings(), Settings, ConfigError, BusyError, RunResult
    measure_block(trades, quotes, open_ns, close_ns, interp, keep, official) -> DayBlock
    schedule(sessions, end, recent_months)       the day plan as a frame
    MEASURE_VERSION
"""

from ._config import ConfigError, Settings, load_settings
from ._layout import BusyError
from ._measure import DayBlock, measure_block
from ._run import MEASURE_VERSION, RunResult
from ._schedule import schedule
from ._store import AdaptationMatrix

__all__ = ["MEASURE_VERSION", "AdaptationMatrix", "BusyError", "ConfigError", "DayBlock", "RunResult", "Settings",
           "load_settings", "measure_block", "schedule"]
