# Getting started

## Requirements

* macOS or Linux, Python 3.12 (installed for you by `uv`)
* [uv](https://docs.astral.sh/uv/) for environment management
* A [Massive](https://massive.com) stocks subscription. Everything here was built against **Stocks
  Starter**: five years of history, day and minute flat files, reference data, corporate actions,
  news, short interest. Trades, quotes and financial statements are not included at that tier and the
  code skips them automatically.
* About 35 GB of disk for five years of data, most of it minute bars.

## Install

```bash
git clone git@github.com:yurit04/collective-alpha.git
cd collective-alpha
uv sync                 # creates .venv with every dependency
uv run pytest -q        # 76 offline tests, no network needed
```

## Credentials

Two secrets live outside the repository and are never committed.

| what | default path | format |
|---|---|---|
| REST API key | `~/Documents/massive_key.txt` | the key on one line |
| S3 flat-file credentials | `~/Documents/massive_s3_key.txt` | dashboard paste, two lines, `k=v`, or JSON |

The S3 file accepts whatever the Massive dashboard gives you: a label line followed by a value line,
two bare lines (access key id then secret), `key = value` pairs, or a JSON object.

Point the platform somewhere else with `config/settings.toml` (copy `config/settings.example.toml`)
or environment variables prefixed `CA_`:

```bash
export CA_DATA_ROOT=/Volumes/market/massive
export CA_API_KEY_FILE=~/keys/massive.txt
export CA_MAX_WORKERS=8
```

**SEC access.** The fundamentals feed uses SEC EDGAR, which rejects requests whose user agent has no
contact address. The default is a placeholder that works; their policy asks for a real one, so set
yours before running the SEC sync:

```toml
# config/settings.toml
sec_user_agent = "your name your.email@example.com"
```

## First run

Check what your subscription reaches, then build everything:

```bash
uv run ca config                 # effective settings, and whether the key files were found
uv run ca probe                  # which endpoints and flat-file datasets this account can reach
uv run ca backfill               # the whole store, back to the entitlement horizon
```

`backfill` is idempotent and resumable: interrupt it and run it again. Expect a few hours on a first
run, dominated by downloading minute bars. Then build the derived layers in order, each of which
depends on the one before:

```bash
uv run ca master build           # ticker -> security id over time          ~3 min first time
uv run ca universe build all     # point-in-time universes                  ~40 s
uv run ca panel build            # daily returns, splits, dividends         ~10 s
uv run ca intraday build         # per-session aggregates from minute bars  ~2 min
uv run ca features build all     # six feature groups                       ~2 min
uv run ca eval forward           # forward returns for signal evaluation    ~10 s
```

## Verify

```bash
uv run ca status                 # what was downloaded and converted, plus disk free
uv run ca panel check            # return sanity and the largest moves
uv run ca intraday check         # session coverage against the exchange calendar
uv run ca intraday spreads       # estimated spreads must fall as liquidity rises
uv run ca features list          # every built feature group and its columns
```

A healthy five-year store looks roughly like this:

| table | rows | size |
|---|---|---|
| minute bars | 2.0 B | 26 GB |
| intraday aggregates | 13.4 M | 1.9 GB |
| daily panel | 13.9 M | 650 MB |
| features, six groups | 13.4 M each | 4.4 GB |
| forward returns | 13.9 M | 900 MB |

## Where to go next

* [Data dictionary](data.md) for what every table and column means
* [Research guide](research.md) for the workflow from idea to backtest
* [`examples/`](../examples) for runnable scripts
* [CLI reference](cli.md) for every command
* [Operations](operations.md) for the nightly refresh and paper trading
* [Architecture](architecture.md) for why things are built the way they are, and what to watch out for
