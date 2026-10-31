# Projects

Each project is an independent research or trading effort (create one with the
`new-project` skill). A project:

- has its own `pyproject.toml`, `src/`, `notebooks/`, `configs/`, `tests/`;
- depends only on core packages (declared with `{ workspace = true }` sources);
- never imports from another project. Shared code moves into a core package.
