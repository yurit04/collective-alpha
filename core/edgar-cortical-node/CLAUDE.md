# edgar-cortical-node (core, depends on nothing in the repo)

SEC EDGAR XBRL company facts: every filing kept with its filing date, point-in-time queries by CIK.
User docs: README.md in this folder. Design: docs/decisions/0003-edgar-cortical-node.md.

## Public API (`from edgar_cortical_node import ...`)

| Name | Use |
|---|---|
| `EdgarStore(root=None, settings=None)` | all reads; see below |
| `Ingestor(settings)` | `.bulk(force, download)`, `.profiles(force, download)`, `.sync(through)`, `.refresh_ciks(ciks)`, `.refresh_profiles(ciks)` |
| `load_settings(config_file=None, **overrides)` / `Settings` | resolved configuration |
| `CONCEPTS` / `Concept` | item name -> kind (flow, instant, per_share), unit, tags in priority order |
| `ConfigError`, `JobResult` | errors / job summaries |

`EdgarStore` methods (all take `backend="polars"|"pandas"`; `ciks` are ints or padded strings):
- `series(items, ciks, policy="first"|"latest")`: long table cik, item, unit, period
  (quarter/half/annual/instant), start, end, value, filed, available_from, tag, source
  (reported/derived), ttm, ttm_available_from
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
_pit.py       point-in-time rules: choose first/latest, tag priority, quarters, TTM, shares (stitch,
              spikes), as-of
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
- Never send the user's own e-mail to the SEC unless they put it in their config.
- Respect the SEC's 10 requests/second limit (default 8, shared across threads).
