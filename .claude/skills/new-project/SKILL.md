---
name: new-project
description: Scaffold a new independent research or strategy project under projects/ that uses core packages. Use when the user starts a new study, strategy, or experiment.
---

# New project

1. Agree on the name, goal, and which core packages it needs.
2. Create `projects/<name>/`:
   ```
   pyproject.toml   README.md        # goal, hypothesis, how to run, results summary
   src/<import_name>/__init__.py
   notebooks/                         # exploration; keep reusable logic in src/
   configs/                           # parameters (toml/yaml)
   tests/__init__.py
   ```
   `pyproject.toml`:
   ```toml
   [project]
   name = "<name>"
   version = "0.1.0"
   requires-python = ">=3.12"
   dependencies = ["massive-subspace-stream"]   # core packages + third-party only

   [tool.uv.sources]
   massive-subspace-stream = { workspace = true }

   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"

   [tool.hatch.build.targets.wheel]
   packages = ["src/<import_name>"]
   ```
3. Rules: never import another project; if two projects need the same code, propose a core
   package (or a function in an existing one) instead.
4. Read market data through `massive_subspace_stream.Store` (see the `query-market-data` skill).
   Large outputs (backtest results, models) go outside the repo or in a git-ignored folder.
5. `uv sync --all-packages`, `uv run pytest projects/<name>`, `uv run python tools/check_architecture.py`.
6. If the project needs third-party versions that conflict with the workspace, discuss moving it
   out of the workspace (path dependencies on core) rather than changing shared pins.
