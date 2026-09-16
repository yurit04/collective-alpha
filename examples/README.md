# Examples

Runnable scripts against your built store. Each takes a minute or two.

```bash
uv run python examples/01_evaluate_a_signal.py
uv run python examples/02_portfolio_and_costs.py
uv run python examples/03_point_in_time_data.py
```

**01, evaluate a signal.** Builds a two-part signal from intraday features, measures its information
coefficient at three horizons, then backtests it with per-name spreads. The idea does not work, which
is the point: the whole loop takes two minutes and tells you so.

**02, portfolio and costs.** The same momentum signal as a raw decile spread, then with honest
spreads, then as a sector-neutral volatility-targeted book. Shows costs roughly doubling on the broad
universe and risk management cutting the drawdown from 34% to 7%.

**03, point-in-time data.** How to read the store: why tickers are not identifiers, how to attach a
security id, universe membership on a date, joining the panel to features, and proof that delisted
names are present.

The notebooks in [`../notebooks`](../notebooks) cover the same ground interactively: `inspection/` for
data quality, `research/` for a study template.
