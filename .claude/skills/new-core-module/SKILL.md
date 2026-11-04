---
name: new-core-module
description: Scaffold a new core package under core/ (e.g. features, backtesting, execution) and wire it into the dependency DAG. Use when the user wants a new reusable building block.
---

# New core module

1. **Agree on the plan first** (name, purpose, public API, which core packages it depends on).
   Names follow the Borg theme (planned: `vinculum` features, `unimatrix-zero` backtesting,
   `transwarp-conduit` execution, `borg-queen` portfolio/risk).
2. Check the graph: `uv run python tools/check_architecture.py --graph`. The new package may
   depend only on existing core packages, and nothing may end up depending on it in a cycle.
3. Create `core/<name>/`:
   ```
   pyproject.toml   README.md   CLAUDE.md
   src/<import_name>/__init__.py      # public API only; internals in _private modules
   tests/__init__.py  tests/test_*.py
   ```
   `pyproject.toml` template:
   ```toml
   [project]
   name = "<name>"
   version = "0.1.0"
   description = "..."
   readme = "README.md"
   requires-python = ">=3.12"
   dependencies = ["massive-subspace-stream"]   # core deps + third-party

   [tool.uv.sources]
   massive-subspace-stream = { workspace = true }

   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"

   [tool.hatch.build.targets.wheel]
   packages = ["src/<import_name>"]
   ```
4. Import dependencies only through their public API (top-level names). Read each dependency's
   `CLAUDE.md` for that API instead of its internals.
5. `uv sync --all-packages`, then `uv run pytest` and `uv run python tools/check_architecture.py`.
6. Add the package to the "Current core graph" table in the root `CLAUDE.md`, and write a short
   `docs/decisions/NNNN-<name>.md` if there were notable design choices.
7. Add a demo notebook folder `projects/subspace-probe/notebooks/<name>/` (at least `00_*.ipynb`)
   showing the module's functionality, using only the module and its declared dependencies;
   declare the module in subspace-probe's `pyproject.toml`; execute the notebook and commit it
   with outputs; add it to the project's README.
