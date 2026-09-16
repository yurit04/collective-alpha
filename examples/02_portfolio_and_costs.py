"""Take a signal that does work, run it through portfolio construction, and show what honest
transaction costs do to it.

    uv run python examples/02_portfolio_and_costs.py

Prints three books built on the same 12-1 momentum signal: raw decile spread, the same thing with
per-name spreads charged, and a risk-managed book that is sector neutral and volatility targeted.
"""

from __future__ import annotations

import datetime as dt

from collective_alpha.backtest.engine import BacktestConfig, CostModel
from collective_alpha.backtest.run import backtest_feature
from collective_alpha.portfolio.construct import PortfolioConfig
from collective_alpha.portfolio.run import exposure_report, portfolio_feature

UNIVERSE = "all_common"  # the broad universe, where cost assumptions actually matter
START = dt.date(2022, 1, 1)
COSTS = CostModel(commission_bps=0.5, half_spread_bps=3.0, slippage_bps=2.0, borrow_rate=0.005)


def row(label: str, summary: dict) -> str:
    return (
        f"{label:<34} net {summary['ann_return_net']:+.3f}  Sharpe {summary['sharpe_net']:+.2f}  "
        f"maxDD {summary['max_drawdown']:.3f}  costs/yr {summary['ann_cost_drag']:.4f}"
    )


def main() -> None:
    flat = backtest_feature("mom_12_1", UNIVERSE, rebalance=21, costs=COSTS, start=START)
    print(row("decile spread, flat 3bp", flat.summary()))

    real = backtest_feature("mom_12_1", UNIVERSE, rebalance=21, costs=COSTS, start=START, use_spreads=True)
    print(row("decile spread, per-name spreads", real.summary()))

    cfg = PortfolioConfig(
        method="heuristic",
        gross=2.0,
        max_weight=0.02,
        n_names=400,
        sector_neutral=True,
        vol_target=0.12,
    )
    managed, diag = portfolio_feature(
        "mom_12_1", UNIVERSE, cfg=cfg, rebalance=21, bt=BacktestConfig(delay=1, costs=COSTS), start=START
    )
    print(row("sector neutral, 12% vol target", managed.summary()))
    print("  exposures:", {k: v for k, v in exposure_report(diag).items() if k != "rebalances"})
    print("  by year:", managed.by_year().select("year", "ret_net").to_dicts())


if __name__ == "__main__":
    main()
