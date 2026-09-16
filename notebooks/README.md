# Notebooks

```bash
uv run jupyter lab
```

* `inspection/01_coverage_and_quality.ipynb` — what is in the store, gaps against the exchange
  calendar, sanity checks on bars, reference-table summaries. Run it after every backfill.
* `research/00_research_template.ipynb` — a study skeleton: load a panel, adjust for splits, define a
  universe, evaluate a signal by rank information coefficient.

Outputs are stripped on commit by `nbstripout`. Install the hook once with `uv run nbstripout --install`.

For scripted rather than interactive work see [`../examples`](../examples), and for the research
workflow in prose see [the research guide](../docs/research.md).
