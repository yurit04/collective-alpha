# 0005: Running on a smaller Massive plan

Date: 2026-11-02. Status: accepted.

## Problem
The repo was built on Massive Stocks Advanced: 20+ years of history, a real-time websocket,
trades, quotes and financials. Starter ($29) has 5 years of history and a 15-minute delayed
feed, and lacks trades, quotes and financials. On Starter, several parts would have failed:

- The stream service would connect to the real-time feed and be refused. launchd would then
  restart it every 30 seconds indefinitely.
- `verify --repair` and backfills would request days older than 5 years and fail.
- Nightly refreshes would log permission errors for financials every night.
- Worst, and silent: splits, dividends, IPOs and the ticker list are refreshed as full
  snapshots that replace the stored file. If Starter answers with 5 years of history, nothing
  errors. The nightly sync would just overwrite 1978-2021 splits, which would break adjusted
  prices and vinculum's market caps.

## Decision
One `plan` setting (`starter`, `developer` or `advanced`; default `advanced`) describes what
the subscription includes (`_config.PLANS`):

- Flat files: requests start at `today - history years`, and datasets the plan lacks are
  skipped. `verify` reports days it can't repair instead of failing.
- Stream: uses the delayed feed on delayed plans, even if the env file still names the
  real-time one. Channels outside the plan are dropped. A refused login stops the service
  cleanly: launchd now restarts it only after a failure exit, and under launchd a refusal exits 0.
- REST: default refreshes leave out what the plan lacks. Named datasets are still tried, and
  a 403 is still skipped.
- Security master: dated lookups older than the plan's history are not requested.

Snapshot refreshes are guarded on every plan (`guard_snapshot`):

- Stored rows dated before the answer's first date are kept.
- If the answer has more than 2% fewer rows than stored over the same span, stored rows
  missing from it are kept too, matched by key.
- A spec without keys refuses such an answer and keeps the old file.

This also covers a truncated response on a full plan. The cost is that a correction Massive
makes by deleting an old record is no longer applied when the record is older than the answer's
span. That record then has to be overridden in the security master's overrides.

## Why not
- **Switching splits and dividends to accumulate mode:** Massive does withdraw bogus splits;
  an accumulating file would keep every one forever. The guard still applies withdrawals
  within the answer's span.
- **Refusing every shorter snapshot:** new splits would then stop arriving. Adjustment for new
  splits matters more than an empty diff.
- **Relying on 403s alone:** a shortened answer is not an error, so it would not be caught.

## Not verified
There was no Starter key to test against. Three things are checked on the first syncs after
the switch:

- Whether Starter shortens the snapshot answers. The guard logs a warning if it does.
- Whether `ticker_details` stays available. vinculum's ADR and class ratios depend on it.
- Whether short interest, short volume and float are included. If not, they are skipped by 403
  and will be replaced by FINRA and SEC data.
