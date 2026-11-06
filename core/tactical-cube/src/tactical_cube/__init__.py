"""tactical-cube: intraday backtests on nanoprobe-array bars (bar-lagged fills, auctions,
participation caps, flat-by times or overnight holds, intraday costs, a look-ahead check)."""

from ._aux import market_inputs
from ._costs import NO_INTRADAY_COSTS, IntradayCosts
from ._cube import Cube, IntradayBacktest
from ._lookahead import LookaheadError, check_lookahead
from ._sim import DayResult, simulate_day

__all__ = ["NO_INTRADAY_COSTS", "Cube", "DayResult", "IntradayBacktest", "IntradayCosts", "LookaheadError",
           "check_lookahead", "market_inputs", "simulate_day"]
