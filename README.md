# collective-alpha

Assimilation of multi-asset market data, quantitative signals, and systematic execution algorithms.
Resistance to alpha decay is futile.

An equity alpha research platform built for one investor: pull everything a
[Massive](https://massive.com) subscription entitles you to, key it to stable security identifiers,
turn it into point-in-time features, measure whether a signal predicts returns, backtest it with
honest transaction costs, and trade it on paper.

Five years of US equities, 13.9 million security-days, 2 billion minute bars, six feature groups,
and a daily trading pipeline. Everything runs locally against Parquet on disk.

## Quick start

```bash
uv sync                          # environment, Python 3.12
uv run pytest -q                 # 76 offline tests
uv run ca probe                  # what your subscription reaches
uv run ca backfill               # the whole store (hours, resumable)
uv run ca master build && uv run ca universe build all && uv run ca panel build
uv run ca intraday build && uv run ca features build all && uv run ca eval forward
```

Then ask a question of it:

```bash
uv run ca eval feature mom_12_1 --universe liquid_1500 --horizon 21
uv run ca backtest feature mom_12_1 --universe all_common --spreads
uv run ca portfolio feature mom_12_1 --vol-target 0.10
```

Full setup, including credentials, is in [getting started](docs/getting-started.md).

## What it does

**Ingestion.** Daily and minute bars from S3 flat files, reference data, corporate actions, news and
short interest from REST, company facts from SEC EDGAR. Every download is recorded in a manifest, so
syncs are idempotent and resumable.

**Identity.** Tickers are reused and renamed, so everything is keyed by a stable `security_id`. The
security master resolves `FB` to Meta until June 2022 and to an ETF from June 2025, and maps 100% of
bar-days to a security.

**Point-in-time discipline.** Universe membership uses lagged data and is frozen between rebalances.
SEC values become usable on their filing date. News after 16:00 counts toward the next session.
Delisted names stay in the store, so studies are survivorship free.

**Honest costs.** There are no quotes on the Starter plan, so effective spreads are estimated from
minute bars and validated against known names: SPY at 0.2 basis points, Apple at 0.75, Ford and
Coca-Cola at exactly one cent, Plug Power at 44. Charging each name its own spread turns a weekly
reversal strategy from marginally profitable into a 15.8% annual loss.

**Research tools.** Rank information coefficient with decay and quantile spreads, a vectorised
backtester with drift, delisting and borrow costs, a statistical risk model, sector-neutral and
mean-variance portfolio construction, and a paper broker with a persistent ledger.

## Documentation

| guide | what is in it |
|---|---|
| [Getting started](docs/getting-started.md) | install, credentials, first build, verification |
| [Data dictionary](docs/data.md) | every table and column, and the coverage caveats |
| [Research guide](docs/research.md) | idea to backtest, and the traps that remain yours |
| [CLI reference](docs/cli.md) | every command |
| [Operations](docs/operations.md) | nightly refresh, paper trading, troubleshooting |
| [Architecture](docs/architecture.md) | design decisions, what was tried and rejected, limitations |
| [`examples/`](examples) | three runnable scripts |
| [`notebooks/`](notebooks) | data inspection and a research template |

## Layout

```
src/collective_alpha/
  config.py            settings from TOML and CA_* environment variables
  calendar.py          NYSE sessions
  cli.py               the `ca` command
  data/massive/        auth, REST client, S3 flat files, transforms, provider
  data/sec/            EDGAR client, company-facts parser, provider
  storage/             path layout, manifest ledger, Parquet writer, DuckDB catalog
  universe/            security master, point-in-time attributes, universes, market cap
  panel/               corporate actions, daily panel with returns
  intraday/            session bounds, per-session aggregates, spread estimators
  features/            six feature groups and cross-sectional signal transforms
  eval/                forward returns, IC and quantile diagnostics, reports
  backtest/            weight schemes, vectorised engine with costs
  portfolio/           sectors, risk model, heuristic and mean-variance construction
  execution/           orders, paper broker, strategies, daily pipeline
config/                settings, universes, strategies
docs/  examples/  notebooks/  scripts/  tests/
```

## Status and limitations

Every planned layer is built. Known gaps, in the order I would close them:

* no live broker adapter; `ca trade export` writes a CSV for manual execution
* spreads are estimated from bars rather than quotes, and validated only cross-sectionally
* the risk model is statistical, with no fundamental factor structure
* revenue resolves for about 78% of names, and there are no analyst estimates or earnings surprises
* walk-forward windows exist but nothing fits parameters yet, so overfitting control is your discipline

Not investment advice. Backtested results are not evidence that a strategy will make money.
