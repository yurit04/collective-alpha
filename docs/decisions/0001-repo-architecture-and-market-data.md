# 0001: Repo architecture and market-data storage

Date: 2026-10-31. Status: accepted.

## Repo
- One uv workspace (`core/*`, `projects/*`), one lockfile, one virtual env. Simple to work
  in. Trade-off: all projects share third-party versions; a project with conflicting needs can
  leave the workspace and use path dependencies on core.
- A shared env means anything is importable, so the rules (core DAG, independent projects,
  declared and public-only imports) are enforced by `tools/check_architecture.py`. It runs in
  pytest, pre-commit, and a Claude Code PostToolUse hook.

## massive-subspace-stream
- **Source split:** bulk history from flat files (S3), reference/fundamental data from REST,
  real-time from one websocket connection (Massive allows one per key).
- **Parquet + zstd, partitioned by time:** day bars per year, minute bars and ticks per date.
  Ticker-sorted row groups keep single-ticker reads fast without per-symbol files.
- **Unadjusted storage, adjustment at query time** from the splits/dividends tables, so a new
  split never forces rewriting history.
- **Catalog (SQLite)** records each stored partition after an atomic rename: jobs are resumable,
  and `verify` can diff the catalog against the bucket.
- **Trades/quotes on demand only.** Measured Oct 2026: about 3 GB (trades) and 9 GB (quotes)
  per day compressed, so full history would be hundreds of TB. Partial (ticker-filtered) days
  record their ticker set; later requests download again and merge.
- **Live data is provisional:** written as small part files every 30 s, merged hourly, and
  pruned after the official flat files for that day arrive.
- **Throttling:** Massive's flat-file server returns HTTP 429 at about 8 parallel downloads;
  default 4 workers plus jittered backoff retries.
