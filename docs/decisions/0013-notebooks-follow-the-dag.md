# 0013: Demo notebooks follow the dependency graph

Date: 2026-11-04. Status: accepted.

## Problem
The demo notebooks in `projects/subspace-probe` were organized by data source, and the later
modules had no home of their own: the neural-processor, unimatrix-zero and borg-queen
demonstrations sat under `notebooks/vinculum/`, and Massive's reference-data notebook reached
into vinculum and FINRA. A reader could not tell which package did what, and nothing stopped a
notebook for an upstream module from calling a downstream one.

## Decision
One notebook folder per core module, named after the package, in `projects/subspace-probe/
notebooks/<core-package>/`. A notebook there shows that module and may import only it, the
core packages it declares as dependencies, and the project's own helpers. The rule is rule 6 of
`tools/check_architecture.py` (code cells are parsed like modules; IPython magics are dropped),
so it runs with the architecture test, the pre-commit hook and CI.

- **In the project, not under `core/`.** The owner chose to keep the notebooks together in
  subspace-probe (its helpers, one kernel, one place to open) rather than next to each package.
- **Direct dependencies only.** What a package's own code may import is what its notebooks may
  show; every package's `pyproject.toml` already lists what it uses.
- **Executed outputs are committed**, never stripped: the notebooks are read on GitHub.
- **A new core module brings a notebook folder** (step 7 of the `new-core-module` skill) and a
  dependency line in subspace-probe's `pyproject.toml`.

## Consequences
- 23 notebooks in nine folders; the moved ones were trimmed to their module's scope (the
  Massive cross-check moved from the EDGAR tour to vinculum's fundamentals notebook, the ratio
  library from vinculum's ratios notebook to neural-processor), and six were written new
  (EDGAR mechanics, FINRA, neural-processor signals up close, the risk model, paper execution
  with the simulated broker, `collective status`).
- Cross-module research that cannot live in one module's folder belongs in a project of its own,
  which is what `projects/` is for.
