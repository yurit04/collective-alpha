# collective-alpha

Personal quant research, backtesting and execution platform (Borg-themed). One
uv workspace: reusable **core** packages plus independent **projects**.

## Layout

```
core/<package>/        reusable building blocks (src layout, own tests, own CLAUDE.md)
projects/<project>/    research / strategies; consume core packages only
tools/check_architecture.py   enforces the dependency rules below
tests/                 repo-level tests (architecture rules)
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

`python tools/check_architecture.py` checks all of this (also run by pytest and by a
Claude Code hook after every edit). `--graph` prints the current graph.

Current core graph:

| Package | Purpose | Depends on |
|---|---|---|
| `massive-subspace-stream` | Massive market data: backfill, live stream, queries, security master | nothing in repo |
| `edgar-cortical-node` | SEC EDGAR company facts: point-in-time fundamentals and share counts by CIK | nothing in repo |
| `vinculum` | Massive x EDGAR join: checked CIKs, market cap, public float, size universes, fundamentals per security | massive-subspace-stream, edgar-cortical-node |

Planned names (not built): features/signals (in `vinculum` or a new package), `unimatrix-zero` (backtesting),
`transwarp-conduit` (broker/execution), `borg-queen` (portfolio/risk/orchestration).

## Commands

```bash
uv sync --all-packages                 # install every workspace package
uv run pytest                          # offline tests (live API tests: uv run pytest -m live)
uv run ruff check .                    # lint
uv run python tools/check_architecture.py
uv run subspace --help                 # data CLI (see core/massive-subspace-stream/README.md)
uv run cortical --help                 # SEC EDGAR CLI (see core/edgar-cortical-node/README.md)
uv run vinculum --help                 # market cap / universes (see core/vinculum/README.md)
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
  `cortical profiles` (1.4 / 1.6 GB), or `service install`.
- Only one websocket connection per API key: use the stream service, don't open another.

## Conventions

- Python 3.12+, src layout, hatchling build, ruff (line length 100).
- Tests must run offline; mark real-API tests `@pytest.mark.live`.
- Every core package has a `CLAUDE.md` describing its public API, so other packages
  never need to read its internals.
- Record notable design choices in `docs/decisions/NNNN-title.md`.
