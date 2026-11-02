# finra-sensor-array (core, depends on nothing in the repo)

FINRA short interest (twice monthly, from 2017-12-29) and daily off-exchange short sale volume
(from 2009-08-03): free public data, stored as Parquet, keyed by FINRA symbols.
User docs: README.md in this folder. Design: docs/decisions/0006-finra-sensor-array.md.

## Public API (`from finra_sensor_array import ...`)

| Name | Use |
|---|---|
| `FinraStore(root=None, settings=None)` | reads; see below |
| `Ingestor(settings, client=None, today=...)` | `.short_interest(start, end)`, `.short_volume(start, end)`, `.sync()` -> `JobResult` |
| `load_settings(config_file=None, **overrides)` / `Settings` | data_root (`FINRA_DATA_ROOT`), rate limit, workers, sync time |
| `ConfigError`, `JobResult` | errors / job summaries |

`FinraStore` methods (take `backend="polars"|"pandas"`, `lazy=False`):
- `short_interest(tickers, start, end, date_by="settlement"|"published")`: settlement_date,
  published_date, symbol, issue_name, market, short_interest, previous_short_interest, change,
  change_pct, avg_daily_volume, days_to_cover, split_flag, revision_flag, fetched_at
- `short_volume(tickers, start, end)`: date, symbol, short_volume, short_exempt_volume,
  total_volume (off-exchange), markets, source (CNMS | TRF+ADF | ORF)
- `scan(dataset)`, `status()`
- `tickers` accept Massive-style tickers (BRK.B, ABRpD) or FINRA symbols

CLI: `sensor backfill short-interest|short-volume [--start --end] | sync | status | show T | service install|remove|status`.

## Internals (private)

```
_config.py    settings ([finra_sensor_array] in collective.toml, FINRA_DATA_ROOT)
_layout.py    paths, state.json, atomic writes
_client.py    API (POST, paged by 5000) and CDN (403/404 = no file); shared rate limit, retries
_calendar.py  NYSE business days, si_published() = settlement + 7 business days
_parse.py     API rows / daily files -> tables; facility summing; finra_symbols()
_ingest.py    Ingestor: per-day fetch, one write per year file, state of done/empty days
_store.py     FinraStore
_services.py  launchd plist (com.collective-alpha.sensor.sync)
cli.py        `sensor`
```

Invariants:
- Point in time: short interest is usable from `published_date`, never `settlement_date`; daily
  short volume from the next trading day.
- Symbols are not identifiers: never join to other data by symbol; go through the security master
  (vinculum).
- A day without data is recorded as empty only after it would surely have been published
  (3 business days later), so an early request never hides a day.
- Short volume = consolidated NMS file (2018-08+) or the sum of FNSQ+FNYX+FNRA before, plus ORF
  (OTC); the consolidated file equals the facility sum (checked).
- Tests are offline (fake client); never hit FINRA in tests.
