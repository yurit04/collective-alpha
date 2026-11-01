<p align="center"><img src="docs/assets/logo.webp" alt="collective-alpha logo" width="300"></p>

# collective-alpha
Assimilation of multi-asset market data, quantitative signals, and systematic execution algorithms. Resistance to alpha decay is futile.

## Structure

- `core/`: reusable packages. Their dependencies form a DAG.
  - [`massive-subspace-stream`](core/massive-subspace-stream/README.md): Massive equity data (backfill, live stream, pandas/polars queries, security master)
  - [`edgar-cortical-node`](core/edgar-cortical-node/README.md): SEC EDGAR fundamentals and share counts, point in time
- `projects/`: independent research and strategies. They depend on core only, never on each other.
  - [`subspace-probe`](projects/subspace-probe/README.md): exploratory data analysis notebooks, one folder per data source
- `tools/check_architecture.py`: enforces those rules (`--graph` prints the graph).

## Quick start

```bash
uv sync --all-packages
uv run subspace init                 # checks credentials (~/.massive.env) and plan coverage
uv run subspace backfill initial     # day + minute bars since 2003, reference, fundamentals, news
uv run subspace status
```

```python
from massive_subspace_stream import Store
Store().bars("AAPL", start="2020-01-01", adjust="splits", backend="pandas")
```

Development: `uv run pytest`, `uv run ruff check .`. Agent guidance lives in [CLAUDE.md](CLAUDE.md).
