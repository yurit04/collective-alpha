# 0012: unicomplex, orchestration seed (`collective status`)

Date: 2026-11-04. Status: accepted.

## Problem
Six stores are kept fresh by four scheduled jobs (Massive, SEC and FINRA syncs, the vinculum
build; on Linux `tools/nightly_sync.sh` runs the same four), a risk model rebuilt by hand and
an execution journal. Each package reports its own status, but nothing says in one place
whether everything a rebalance depends on is as fresh as it should be, and a notebook or a
`conduit plan` run would price from a stale close without noticing.

## Decision
A new core package, `unicomplex`, for orchestration, starting with one read-only command,
`collective status`. It depends on every store package and nothing depends on it, so it sits
at the top of the dependency graph.

- **Read-only, public API only.** It calls each package's `status()` (and `Store.coverage`,
  `stream_status`, `trading_days`) and the conduit's `Journal`; it never downloads, builds,
  connects to IB or writes under a data root. A status field added elsewhere shows up here.
- **Expectations, not ages.** Each check compares the store with what the schedule should
  have produced by now: daily bars, short volume and the market-cap panel against the last
  completed NYSE session; EDGAR against a three-day slack (the sync runs Tue-Sat through
  yesterday); short interest against two settlement cycles plus FINRA's publication lag;
  the risk model against 45 days (rebuilt by hand); the stream against being up and, in
  market hours, recently heard from. The thresholds live in `_checks.py` and follow the
  schedules documented in each package.
- **One failure never hides the rest.** A missing or unreadable store is one `missing` line
  with its error.
- **Exit code 1 on stale or missing**, so `nightly_sync.sh`, cron or a shell prompt can use it.

## Consequences
- A ninth core package and a `collective` command; the root CLAUDE.md names the package
  where it listed "orchestration" as planned.
- Later phases (scheduled runs with one log and one lock, rebalance-day orchestration for
  transwarp-conduit) go in this package.
