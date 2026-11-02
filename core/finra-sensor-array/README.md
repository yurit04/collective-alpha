# finra-sensor-array

Core module of collective-alpha. It downloads FINRA's free short-selling data and stores it as
Parquet under `~/market_data/finra`. It depends on no other package in this repo. Data is keyed
by FINRA's own symbols; vinculum maps them to securities.

| Dataset | Source | History | Frequency |
|---|---|---|---|
| `short_interest` | FINRA Query API, `consolidatedShortInterest` (no key needed) | 2017-12-29 | twice a month (15th and month end) |
| `short_volume` | Reg SHO daily files on `cdn.finra.org` | 2009-08-03 | every trading day |

This is the same data Massive resells. Checked on 2025-10-15 and 2024-03-04/05:
- **Short interest:** 99.97% of Massive's rows match, with identical quantities. Only rights
  issues are unmatched.
- **Short volume:** every row matches exactly, in both directions.

Short volume goes back to 2009, where Massive's starts in 2024.

## Setup

```bash
uv sync --all-packages
uv run sensor backfill short-interest    # ~2,300 small API requests, ~10-15 min
uv run sensor backfill short-volume      # ~13,000 files, ~3 GB downloaded, ~1 GB stored
uv run sensor service install            # daily sync, Tue-Sat 07:15
```

Both jobs can be stopped and re-run: finished days are recorded in `state.json`.

## Querying

```python
from finra_sensor_array import FinraStore

f = FinraStore()
f.short_interest("AAPL", start="2020-01-01")                  # by settlement date
f.short_interest(start="2024-01-01", date_by="published")    # by publication date
f.short_volume(["AAPL", "BRK.B"], start="2024-01-01")
f.status()
```

`tickers` can be FINRA symbols or Massive-style tickers. FINRA writes them differently in each
dataset:

| Massive | short interest | short volume |
|---|---|---|
| `BRK.B` | `BRKB` | `BRK/B` |
| `ABRpD` (preferred) | `ABRPRD` | `ABRpD` |

Symbols are not identifiers. A symbol can belong to different companies over time. Join to
securities through the security master; vinculum does this for you.

CLI: `sensor status`, `sensor show BRK.B`, `sensor sync`, `sensor backfill short-interest|short-volume
[--start --end]`.

## Point in time

- **Short interest.** The position is measured on `settlement_date` but published on
  `published_date`, the 7th business day after it. That date follows FINRA's Rule 4560
  schedule and matches FINRA's published calendar. Signals must use `published_date`. Using
  the settlement date looks about 1.5 weeks ahead.
- **Short volume.** FINRA posts each day's file by 6 pm ET that day, so it is usable from the
  next trading day.
- **Revisions.** Each run fetches short interest published in the last 30 days again, and the
  last 3 days of daily files, to pick up FINRA's corrections.

## What the numbers mean

- `short_interest`: shares sold short and not yet covered, across all broker-dealers, on the
  settlement date. `days_to_cover` = short interest / average daily volume, as FINRA computes it.
- `short_volume`: shares sold short in **off-exchange** trades only, meaning trades reported to
  FINRA's TRFs and ADF, and the ORF for OTC stocks. Trades on the exchanges themselves are not
  included. `total_volume` is off-exchange volume, not total market volume.
- **Not a short-interest measure.** Market makers sell short to fill buy orders, so
  short_volume / total_volume is typically 40–50% for every stock. It is a noisy signal.
- **Sources in `short_volume`:**
  - From 2018-08-01: FINRA's consolidated file (`source = "CNMS"`).
  - Before that: the sum of the Nasdaq TRF, NYSE TRF and ADF files (`"TRF+ADF"`). The
    consolidated file is exactly that sum; this was checked per symbol.
  - OTC stocks come from the ORF file (`"ORF"`).
  - `short_exempt_volume` is null before 2011.

## Limits

- Short interest before 2017-12-29 is not in FINRA's free API. Exchange-listed short interest
  was published by the exchanges before then.
- FINRA's terms allow personal use; don't redistribute the data.
- FINRA has proposed weekly short interest (SR-FINRA-2026-012). Every business day is probed,
  so new settlement dates are found under either schedule. The 7-day publication lag is the
  later of the two schedules, so it stays on the safe side.

## Storage layout

```
~/market_data/finra/
  short_interest/year=YYYY/part.parquet   one row per settlement date, symbol, market
  short_volume/year=YYYY/part.parquet     one row per trading day and symbol
  state.json                              days fetched, days known to have no data
  _logs/
```
