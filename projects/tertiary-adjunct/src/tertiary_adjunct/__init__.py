"""tertiary-adjunct: a twice-monthly cross-sectional strategy at $100k, long-short and long-only
against SPY, under the protocol in configs/protocol.toml (README.md, "Protocol")."""

from ._book import FORMS, benchmark, constraints, ib_commission, round_to_shares, with_commission
from ._protocol import CONFIG, PROJECT, load, signal_dates, signals
from ._scores import (
    equal_groups,
    month_end_ics,
    score_names,
    scores,
    signal_panel,
    walk_forward_weights,
    weighted_signals,
)
from ._stats import (
    ann_return,
    deflated_sharpe,
    expected_max_sharpe,
    figures,
    max_drawdown,
    psr,
    relative_drawdown,
    sharpe,
)
from ._study import (
    Result,
    Study,
    choose,
    development,
    doubled_costs,
    selection_figure,
    selection_series,
)

__all__ = ["CONFIG", "FORMS", "PROJECT", "Result", "Study", "ann_return", "benchmark", "constraints", "deflated_sharpe",
           "choose", "development", "doubled_costs", "selection_figure", "selection_series", "equal_groups", "expected_max_sharpe", "figures", "ib_commission", "load", "max_drawdown",
           "month_end_ics", "psr", "relative_drawdown", "round_to_shares", "score_names", "scores", "sharpe",
           "signal_dates", "signal_panel", "signals", "walk_forward_weights", "weighted_signals", "with_commission"]
