"""unicomplex: orchestration for the collective.

- `collective status`: one read-only view of how fresh every store is (Massive, EDGAR, FINRA,
  vinculum, the intraday summaries, the risk model, the execution journal, the nightly run itself).
- `collective run`: every package's nightly sync or build, one after another, under one lock,
  with one log per run (`Runner`). `collective service install` schedules it with launchd.
- `collective roots`: where every store lives (`roots()`), for tools/reset.sh and tools/bootstrap.sh.
"""

from ._checks import STATES, Check, collect, render
from ._config import ConfigError, Settings, load_settings
from ._roots import Root, roots
from ._runner import STEPS, Runner, RunResult, Step, StepResult

__all__ = ["STATES", "STEPS", "Check", "ConfigError", "RunResult", "Root", "Runner", "Settings", "Step", "StepResult",
           "collect", "load_settings", "render", "roots"]
