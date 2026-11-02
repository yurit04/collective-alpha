# 0006: FINRA short data (finra-sensor-array)

Date: 2026-11-02. Status: accepted.

## Problem
Massive resells FINRA's short interest (from 2017) and daily short sale volume (from 2024).
These may not be included in its smaller plans. Massive's copy also has no publication date,
so a backtest that dates short interest by settlement date looks about 1.5 weeks ahead. FINRA
publishes the same data free, and its daily volume goes back to 2009.

## Decision
A core package, `finra-sensor-array`, that depends on nothing in the repo (like
edgar-cortical-node) and stores FINRA's data keyed by FINRA's symbols. Mapping symbols to
securities is vinculum's job.

- **Short interest.** Fetched from FINRA's Query API (`otcMarket/consolidatedShortInterest`,
  no credentials needed).
  - The API has to be asked for one settlement date at a time, and it can't list which dates
    exist. So every business day is tried once. Days that would surely have been published
    already, but have no data, are remembered as empty. This also picks up the proposed weekly
    schedule without a code change.
  - `published_date` is the settlement date plus 7 business days, the Rule 4560 schedule. It
    reproduces FINRA's calendar.
  - Rows published in the last 30 days are fetched again for revisions.
- **Short volume.** Read from the Reg SHO daily files.
  - From 2018-08 the consolidated file is used. Before that, the Nasdaq TRF, NYSE TRF and ADF
    files are summed. The consolidated file equals that sum exactly, checked per symbol.
  - The ORF file adds OTC stocks, as Massive does.
- **Checks.** Against Massive's stored copy, short interest matches on 99.97% of rows with
  identical quantities (the misses are rights issues). Short volume matches on every row, in
  both directions.

## Why not
- **Keep using Massive:** same numbers, but a shorter history, possibly a plan dependency, and
  no publication date.
- **Put FINRA into massive-subspace-stream:** that package is one vendor's API. A second source
  belongs in its own package, as the SEC data does.
- **Normalise class-share symbols at download:** FINRA's short interest drops the separator
  (`BRKB`, `FOXA`), and its issue names are truncated, so `BRKB` vs `FOXA` can't be resolved
  without a ticker list. Queries translate in the safe direction (Massive's `BRK.B` -> `BRKB`).
  vinculum resolves the rest against Massive's tickers.
