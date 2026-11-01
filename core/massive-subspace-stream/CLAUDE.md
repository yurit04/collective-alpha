# massive-subspace-stream (core, depends on nothing in the repo)

Massive (formerly Polygon.io) US equity data: download, store as Parquet, query.
User docs: README.md in this folder.

## Public API (`from massive_subspace_stream import ...`)

| Name | Use |
|---|---|
| `Store(root=None, settings=None)` | all reads; see below |
| `load_settings(config_file=None, **overrides)` / `Settings` | resolved configuration |
| `Ingestor(settings)` | `.flat(dataset, start, end, tickers)`, `.rest(names, full)`, `.initial()`, `.sync()`, `.verify()`, `.identity(fetch)` |
| `StreamService(settings, channels, watchlist, watch_channels)` | `await .run()`; prefer the CLI |
| `ConfigError`, `JobResult` | errors / job summaries |

`Store` methods (all take `backend="polars"|"pandas"`):
- `bars(tickers, start, end, freq="1d"|"1min", adjust=None|"splits"|"all", session="all"|"regular"|"extended", include_live=False, by="ticker"|"security", lazy=False)`
  (with a security master every row has `security_id`; `by="security"` returns a security's whole
  history across renames, e.g. `bars("META", by="security")` includes the FB years)
- `trades(tickers, start, end)`, `quotes(...)` (end defaults to start; warns if not downloaded)
- `universe(on, types=None)`: tickers that traded that day (survivorship-free) + point-in-time
  reference info and `security_id`
- `resolve(tickers, on)`, `securities(tickers, active)`, `security_master(tickers)`: the identity layer
- `tickers(active, types)`, `splits()`, `dividends()`, `financials("income"|"balance"|"cash_flow", tickers, start, end)`,
  `short_interest()`, `short_volume()`, `news()`, `dataset(name, ...)` for any REST dataset
- `trading_days(start, end)`, `sessions(start, end)` (NYSE, incl. early closes)
- `live(kind, tickers, start, end)`, `scan(dataset)` (lazy polars), `coverage()`, `datasets()`
- `follow(kind, tickers, poll_seconds=5, since=None, timeout=None)`: generator of new live rows
  (reads the stream service's files; never open a second websocket)
- `stream_status()`: is the stream service running, what it subscribes to, its counters

## Internals (private; for work inside this package only)

```
_config.py      settings + env-file parsing (credentials never in toml or repr)
_layout.py      every on-disk path
_catalog.py     SQLite record of stored partitions + incremental state
_storage.py     atomic Parquet writes, upserts
_sources/       rest.py (httpx, retries, cursor pagination), flatfiles.py (boto3 S3)
_datasets/      flat.py (CSV -> canonical schema), rest.py (REST dataset specs + normalization)
_ingest/        backfill.py (Ingestor), stream.py (websocket service, live writer)
_adjust.py      split/dividend factors (as-of joins), keyed by security_id or ticker
_identity/      security master: fetch.py (snapshots, dated lookups), build.py (episodes, segments,
                linking, ids), actions.py (splits/dividends per security), checks.py, run.py,
                overrides.csv (manual corrections, one documented row each)
_sessions.py    exchange_calendars XNYS wrapper
_store.py       Store
_services.py    launchd plists
cli.py          `subspace` (typer)
```

Invariants:
- A partition is marked in the catalog only after its file is safely renamed into place.
- Stored prices are unadjusted; adjustment happens in `_adjust.py` at query time.
- Timestamps are UTC ns; `date` is the US/Eastern trading date.
- Minute bars exclude the closing auction print; daily bars include it (verified with trades).
- Financials: one row per period from Massive, dated by the latest filing; `filing_date` is in
  the storage key so re-filed versions accumulate. Not point-in-time before the first download.
- Every live row has `received_at` (its flush time); compaction keeps it. `follow()` relies on it.
- Tickers are not identifiers. `security_id` (earliest composite FIGI, else share-class FIGI, else
  `CIK:<cik>:<ticker>`, else `SYM:<ticker>:<first date>`) is; ids must stay stable across
  rebuilds. Rebuilt nightly by `sync` once `subspace identity build` has run. Design:
  docs/decisions/0002-security-master.md.
- Corporate actions are filed by Massive under the company's current ticker; the master maps
  them to securities, and adjustment uses those (never ticker-keyed factors when a master exists).
- Adding a REST dataset = one `RestSpec` entry in `_datasets/rest.py` (+ optional `Store` helper).
- Flat-file throttling (HTTP 429) is retried with backoff in `FlatFileClient.download`;
  keep `download_workers` at about 4.
