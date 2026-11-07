# collective-alpha

Personal quant research, backtesting and execution platform (Borg-themed). One
uv workspace: reusable **core** packages plus independent **projects**.

## Layout

```
core/<package>/        reusable building blocks (src layout, own tests, own CLAUDE.md)
projects/<project>/    research / strategies; consume core packages only
projects/subspace-probe/notebooks/<core-package>/   demo notebooks, one folder per core module (rule 5)
tools/check_architecture.py   enforces the dependency rules below
tests/                 repo-level tests (architecture rules)
.github/workflows/ci.yml      lint, dependency rules and offline tests on push and pull request (3.12, 3.13)
docs/decisions/        short design records: why X over Y
.claude/skills/        new-core-module, new-project, query-market-data
```

## Dependency rules (enforced)

1. Core packages may depend on other core packages; the graph must stay acyclic.
2. Projects depend on core packages only, never on another project.
3. Core never depends on a project.
4. Import another workspace package only if your `pyproject.toml` declares it, and only
   its public API (names exported from its top-level `__init__`). Modules starting with
   `_` are private.
5. Demo notebooks live in `projects/subspace-probe/notebooks/<core-package>/`, one folder per
   core package. A notebook there shows that package and may use only it, the packages it
   declares as dependencies, and the project's own helpers. Outputs are committed (the
   notebooks are read on GitHub); never strip them.

`python tools/check_architecture.py` checks all of this, notebooks included (also run by pytest
and by a Claude Code hook after every edit). `--graph` prints the current graph.

Current core graph:

| Package | Purpose | Depends on |
|---|---|---|
| `massive-subspace-stream` | Massive market data: backfill, live stream, queries, security master | nothing in repo |
| `edgar-cortical-node` | SEC EDGAR company facts: point-in-time fundamentals (31 items) and share counts by CIK | nothing in repo |
| `finra-sensor-array` | FINRA short interest (from 2017-12) and daily short sale volume (from 2009-08), with publication dates | nothing in repo |
| `vinculum` | Massive x EDGAR x FINRA join: checked CIKs, successors, market cap, public float, size universes, fundamentals and short data per security | massive-subspace-stream, edgar-cortical-node, finra-sensor-array |
| `neural-processor` | Point-in-time signals per security (value, profitability, quality, leverage, growth, price, short interest), per-date transforms and composites | vinculum, edgar-cortical-node, massive-subspace-stream |
| `unimatrix-zero` | Backtests: daily returns across successors with delisting exits, portfolio builders, daily engine with lag and costs, analytics | neural-processor, vinculum, massive-subspace-stream |
| `borg-queen` | Risk model: market, sector and style factors, daily factor returns, EWMA covariance with regime adjustment, specific risk, forecasts, attribution; cost-aware optimizer (cvxpy) and rebalance loop | unimatrix-zero, neural-processor, vinculum, massive-subspace-stream |
| `transwarp-conduit` | Execution through Interactive Brokers: IB contract mapping, order sheets with limits, paper-first MOC orders, journal, reconciliation | unimatrix-zero, massive-subspace-stream |
| `nanoprobe-array` | Intraday: regular-session bar clock, minute bars resampled with gap rules, auction prices, per-bar features, daily intraday summaries store | massive-subspace-stream, vinculum |
| `tactical-cube` | Intraday backtests: decisions at bar ends, lagged VWAP or auction fills, participation caps, flat-by times or overnight holds, intraday costs, look-ahead check; daily output in unimatrix-zero's format | nanoprobe-array, unimatrix-zero, vinculum |
| `unicomplex` | Orchestration: `collective run`, every nightly sync and build in order under one lock and log (one launchd job); `collective status`, a read-only freshness report over every store | massive-subspace-stream, edgar-cortical-node, finra-sensor-array, vinculum, nanoprobe-array, borg-queen, transwarp-conduit |

Planned (not built):
intraday cost calibration from quotes and trades (decision 0014 phase 3),
`transwarp-conduit` phases 2-3 (cost calibration, live).

## Commands

```bash
uv sync --all-packages                 # install every workspace package
uv run pytest                          # offline tests (live API tests: uv run pytest -m live)
uv run ruff check .                    # lint
uv run python tools/check_architecture.py
uv run subspace --help                 # data CLI (see core/massive-subspace-stream/README.md)
uv run cortical --help                 # SEC EDGAR CLI (see core/edgar-cortical-node/README.md)
uv run vinculum --help                 # market cap / universes (see core/vinculum/README.md)
uv run sensor --help                   # FINRA short data (see core/finra-sensor-array/README.md)
uv run queen --help                    # risk model (see core/borg-queen/README.md)
uv run conduit --help                  # IB execution (see core/transwarp-conduit/README.md)
uv run nanoprobe --help                # intraday bars, features, daily intraday summaries (core/nanoprobe-array/README.md)
uv run collective status               # is every store as fresh as the schedule allows? (core/unicomplex/README.md)
uv run collective run --dry-run        # the nightly steps in order (`run` runs them; `runs` shows history)
```

## Market data

- Data root: `~/market_data/massive` (override: `SUBSPACE_DATA_ROOT` or `collective.toml`).
- Read data through `massive_subspace_stream.Store`, never by globbing Parquet paths
  in project code. See `.claude/skills/query-market-data/SKILL.md`.
- The Massive plan is the `plan` setting (`starter`, `developer` or `advanced`). Downloads,
  the stream and refreshes follow it. Never work around a plan limit by changing endpoints.
- Bars are stored unadjusted. Pass `adjust="splits"` or `adjust="all"` when a
  study needs continuous prices.
- Market cap, size universes and fundamentals per security come from `vinculum.Vinculum`;
  never join EDGAR to Massive by ticker.

## Safety rules for agents

- Credentials live in environment variables / `~/.massive.env`. Never print, log, or commit
  them, and never put them in `collective.toml` (the code rejects that).
- Do not delete anything under the data root without asking.
- Ask before starting long or large jobs: `backfill initial`, minute-bar history, any
  `trades`/`quotes` download without `--symbols` (3–9 GB per day), `cortical backfill` /
  `cortical profiles` (1.4 / 1.6 GB), `sensor backfill short-volume` (~13,000 files, ~3 GB),
  `service install` (any package, including `collective service install`) or `collective run`
  (the whole nightly sequence, about 15 minutes).
- Only one websocket connection per API key: use the stream service, don't open another.
- Orders (transwarp-conduit): `conduit status`, `contracts`, `plan`, `show`, `orders`, `fills` and
  `reconcile` only read. Never run `submit`, `cancel` or `cancel-all` (or the same `Conduit`
  methods) without the user's explicit go-ahead for that sheet, and never pass `--live` or set
  `live = true` unless the user asks for live trading in so many words.

## Conventions

- Python 3.12+, src layout, hatchling build, ruff (line length 100).
- Tests must run offline; mark real-API tests `@pytest.mark.live`.
- Every core package has a `CLAUDE.md` describing its public API, so other packages
  never need to read its internals.
- Record notable design choices in `docs/decisions/NNNN-title.md`.
