# edgar-cortical-node (core, depends on nothing in the repo)

SEC EDGAR XBRL company facts: every filing kept with its filing date, point-in-time queries by CIK.
User docs: README.md in this folder. Design: docs/decisions/0003-edgar-cortical-node.md.

## Public API (`from edgar_cortical_node import ...`)

| Name | Use |
|---|---|
| `EdgarStore(root=None, settings=None)` | all reads; see below |
| `Ingestor(settings)` | `.bulk(force, download)`, `.profiles(force, download)`, `.sync(through)`, `.refresh_ciks(ciks)`, `.refresh_profiles(ciks)` |
| `load_settings(config_file=None, **overrides)` / `Settings` | resolved configuration |
| `CONCEPTS` / `Concept` | item name -> kind (flow, instant, per_share), unit, tags in priority order, nonnegative, second_quarter |
| `ConfigError`, `JobResult` | errors / job summaries |

`EdgarStore` methods (all take `backend="polars"|"pandas"`; `ciks` are ints or padded strings):
- `series(items, ciks, policy="first"|"latest")`: long table cik, item, unit, period
  (quarter/half/annual/instant), start, end, value, filed, available_from, tag, source
  (reported/derived/redated), ttm, ttm_available_from. 31 items (flows, instants, eps_basic and
  eps_diluted); per-share `ttm` = annual figure at fiscal year ends, else four quarters summed,
  empty across a split (values as filed, not split-adjusted)
- `shares(ciks, policy)`: cik, end, shares, filed, available_from, tag (as reported: splits are
  lasting jumps; main concept plus agreeing concepts; isolated outliers dropped)
- `as_of(on, ciks, items, policy, stale_days=400)`: newest usable value per company and item, plus shares
- `facts(ciks, tags, start, end, filed_before, lazy)`: raw facts; `scan(ciks)`: lazy frame
- `profiles(ciks)`: cik, name, entity_type, sic, sic_description, category, fiscal_year_end,
  state_of_incorporation, tickers, exchanges, first_periodic, last_periodic (current values)
- `names(ciks)`: cik, name, valid_from, valid_to (name history; null = open)
- `companies()`, `concepts()`, `status()`

CLI: `cortical init | backfill | profiles | sync | status | show CIK | service install|remove|status`.

## Internals (private)

```
_config.py    settings (collective.toml [edgar_cortical_node], EDGAR_DATA_ROOT, EDGAR_USER_AGENT)
_layout.py    paths, CIK buckets, state.json
_client.py    SEC HTTP: User-Agent, shared rate limit (<= 8/s), retries, streamed downloads
_parse.py     company-facts JSON -> long table; submissions -> profiles/names; daily index; workers
_ingest.py    bulk load (parallel processes, per-bucket compaction, swap), profiles, daily sync
_concepts.py  CONCEPTS and share tags
_pit.py       point-in-time rules: choose first/latest, tag priority, quarters, TTM, per-share TTM and
              split guard, float re-dating, shares (stitch, spikes), as-of
_store.py     EdgarStore
_services.py  launchd plist for the daily sync
cli.py        `cortical`
```

Invariants:
- Facts are never edited: a re-fetch replaces all rows of that CIK; only exact duplicates are dropped.
- A fact is usable from `filed + 1 day`; never use `end` (period end) as an availability date.
- Default policy is first-filed per (cik, tag, unit, start, end); across tags, the value known first
  wins (tag priority only breaks ties within a filing date). Restated values only with policy="latest".
- Quarters derived from YTD/annual take the later filing date of their two inputs; TTM the latest of four.
- A filing that reports a period in two currencies keeps the company's reporting currency (the
  one most of its facts for the item are in): USD convenience translations never replace it.
- Per-share TTM never sums quarters across a share-count jump (split) between their filings;
  at fiscal year ends it is the reported annual figure. Loading a per-share item also loads the
  share tags (`build_series(..., shares=)`).
- Concepts with `second_quarter=True` (public float): a 10-K value dated within 10 days of the
  filing's fiscal year end (its latest Assets/StockholdersEquity date) moves back six months,
  source "redated"; 10-Q copies and values dated after the year end are never moved.
- Never send the user's own e-mail to the SEC unless they put it in their config.
- Respect the SEC's 10 requests/second limit (default 8, shared across threads).
- The parse workers use the `spawn` start method (`_ingest._SPAWN`). Never let them fork: the
  parent has used polars' thread pool by then, and a forked child deadlocks in it (Linux's
  default; it hung CI and a forced fork hangs on macOS too).
