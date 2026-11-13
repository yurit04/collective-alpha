"""unimatrix-zero: point-in-time backtests over vinculum universes and neural-processor signals."""

from ._analytics import by_year, exposures, stats, summary
from ._costs import NO_COSTS, SPREAD_FIT, CostModel, SpreadFit
from ._engine import Backtest, Engine
from ._returns import daily_returns
from ._weights import cap_weights, quantile_weights, rank_weights, top_n_weights

__all__ = ["NO_COSTS", "SPREAD_FIT", "Backtest", "CostModel", "SpreadFit", "Engine", "by_year", "cap_weights", "daily_returns", "exposures",
           "quantile_weights", "rank_weights", "stats", "summary", "top_n_weights"]
