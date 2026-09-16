# Command reference

Everything runs through `ca`. Add `--help` to any command for its options.

## Data ingestion

| command | what it does |
|---|---|
| `ca config` | print effective settings and whether the credential files were found |
| `ca probe` | report which REST endpoints and flat-file datasets the account can reach |
| `ca backfill` | download and convert everything back to the entitlement horizon |
| `ca update [--days-back 5]` | incremental refresh, safe to run nightly |
| `ca status [--errors]` | per-dataset manifest summary and disk free |
| `ca coverage <dataset>` | trading days with no curated file |
| `ca convert <dataset>` | convert raw files that have no Parquet yet |

Individual syncs, all idempotent:

```bash
ca sync bars day_aggs_v1 --start 2024-01-01 --end 2024-12-31
ca sync bars minute_aggs_v1          # 26 GB for five years
ca sync reference [--details]        # tickers, types, exchanges, conditions, holidays
ca sync tickers-pit                  # monthly point-in-time ticker snapshots
ca sync corporate-actions            # splits, dividends, IPOs
ca sync monthly news|short_interest|short_volume
ca sync sec-facts [--source bulk|api]  # SEC company facts
ca sync fundamentals                 # vendor statements and ratios, if your tier has them
```

## Derived layers

```bash
ca master build [--gap-days 30]      # security master + securities
ca master lookup FB                  # every security a ticker has referred to

ca universe build [name|all]         # membership from config/universes.toml
ca universe stats liquid_1500        # members and turnover per rebalance

ca panel build                       # daily panel with split/dividend-aware returns
ca panel check [--threshold 1.0]     # sanity report and the largest returns

ca intraday build [--start --end --force]   # per-session aggregates from minute bars
ca intraday check                    # session coverage
ca intraday spreads [--universe]     # spread validation table

ca features build [group|all]        # price, size, fund, short, news, intra
ca features list                     # built groups and their columns

ca eval forward [--delist-return 0]  # forward returns over 1..63 sessions
```

## Research

```bash
ca eval feature mom_12_1 --universe liquid_1500 --horizon 21
ca eval feature ret_5d --sign -1 --horizon 5           # flip the sign for reversal
ca eval feature earnings_yield --neutralize-by primary_exchange --as-json

ca backtest feature mom_12_1 --universe liquid_1500 --rebalance 21
ca backtest feature mom_12_1 --spreads                 # per-name estimated spreads
ca backtest feature ret_5d --sign -1 --rebalance 5 --scheme signal_weighted --impact

ca portfolio feature mom_12_1 --vol-target 0.10        # sector neutral, capped, vol targeted
ca portfolio feature mom_12_1 --method mvo --turnover-penalty 0.002
```

Useful backtest options: `--scheme ls_quantile|long_top|signal_weighted`, `--rebalance` in sessions
or `monthly`/`weekly`, `--gross`, `--max-weight`, `--delay`, `--commission-bps`, `--half-spread-bps`,
`--slippage-bps`, `--borrow-rate`, `--spreads`, `--impact`, `--start`, `--end`, `--as-json`.

## Research sprints

```bash
ca research screen --universe liquid_1500 --end 2024-12-31 --tag is_liquid1500
```

Screens every feature against forward returns at four horizons, applies a Benjamini-Hochberg
correction across the whole family, and writes the table to `<data_root>/research/`. See
[research sprint 1](research-sprint.md) for a worked example including its negative result.

## Trading

```bash
ca trade run momentum_ls [--date 2026-09-11] [--force-rebalance]
ca trade replay momentum_ls --start 2026-06-01 --end 2026-09-11
ca trade status momentum_ls
ca trade export momentum_ls --path orders.csv
```

Strategies are TOML files in `config/strategies/`. See [operations](operations.md).
