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

## Addendum 2026-11-07: phase 2, one nightly runner (`collective run`)

**Problem.** On macOS the four jobs were separate launchd services at fixed times (Massive
06:30, EDGAR 07:00, FINRA 07:15, vinculum 07:45). Nothing made a later job wait for an earlier
one: on 2026-11-06 the Massive sync ran until about 07:42, three minutes before vinculum
started. A longer sync would let vinculum build from yesterday's bars, and the risk model and
intraday summaries were not scheduled at all.

**Decision.** `collective run` runs every step one after another: `subspace sync`, `cortical
sync`, `sensor sync`, `vinculum build`, `nanoprobe build`, `queen build`.

- **Each step is the package's own CLI**, started as a subprocess (the script next to the
  interpreter, else PATH). A step behaves exactly as by hand, nothing is reimplemented, and a
  crash in one cannot take the runner down.
- **A failed step does not stop the rest** (vinculum rebuilds from whatever is stored, as
  `nightly_sync.sh` already did); the run is recorded as failed and exits 1.
- **Busy, not failed.** `subspace sync` exits 2 when another Massive job holds the writer lock
  (the Ingestor writer lock); the runner retries it every 5 minutes for up to 2 hours.
- **One lock, one log, one history.** `<UNICOMPLEX_DATA_ROOT>/_run/nightly.lock` (flock; a
  second runner exits 0), `runs/<run_id>.log` with every step's output, and one JSON line per
  run in `runs.jsonl` (status, and per step: exit code, seconds, attempts). These are the only
  files unicomplex writes.
- **Skip if recent.** A full run that succeeded under 3 hours ago makes `run` a no-op
  (`--force` overrides), so a manual run before 06:30 is not repeated.
- **One launchd job**, `com.collective-alpha.unicomplex.nightly`, Tue-Sat 06:30 local time. Its
  plist carries each package's data root as resolved at install time through that package's
  public settings, plus `COLLECTIVE_CONFIG`; no credentials. Installing refuses while the old
  per-package schedules exist; `--replace` removes them through each package's own `service
  remove` (never the Massive stream).
- **Status knows about it.** `collective status` gains `unicomplex/nightly_run`: ok when the
  last full run after the latest scheduled time (less the 3-hour skip window, after a
  15-minute grace) succeeded; stale when it is missing or failed (with the failed steps and
  the log); info while a run holds the lock or before the first run.
- `tools/nightly_sync.sh` (Linux) keeps its network wait and then calls `collective run`.

**Rejected.** A dependency graph with parallel steps: the steps share one disk and the
Massive sync dominates; sequence is simpler and as fast in practice. Calling the packages'
Python APIs in-process: one package's memory or a hung thread would affect the rest, and
the CLIs already handle their own settings, logging and exit codes.

**Correction 2026-11-07 (sleep, not slowness).** The 70-minute Massive sync on 2026-11-06 was
the Mac asleep with the lid closed: it woke for about 45 seconds every 15 minutes (Power Nap),
and the sync ran only during those wakes (`pmset -g log` matches each burst in the sync log).
Awake, the whole sequence takes about 12 minutes (Massive 3, EDGAR about 4, FINRA seconds,
vinculum 3.5, nanoprobe 1, queen 0.3). The launchd job now runs under `caffeinate -i -s`, which
keeps the Mac awake until the run exits. Starting it at its next wake after 06:30, rather than
waking the Mac at 06:30, is left to the user's power settings (`pmset repeat`).

**Addition 2026-11-07 (intraday summaries).** `collective status` now checks nanoprobe-array's
daily intraday summaries (`nanoprobe/summaries`) against the last completed session, like the
market-cap panel: the nightly run builds them after vinculum. The check reads
`Nanoprobe.status()` on the same Massive store as the other checks; when the summaries trail
the minute bars the detail says so.

**Addition 2026-11-11 (time limits, lateness, notifications).** The 2026-11-10 run took 12
hours: the Mac was on battery, where `caffeinate -s` does not hold, and slept through it in
Power Nap bursts; `status` still said ok afterwards, and nothing told anyone. Three changes:
- *Time limits per step* (`Step.timeout_minutes`, `step_timeout_minutes` setting). A hung step
  used to hold the lock forever, and the next night's run exited as `locked`, recording
  nothing. A step past its limit is now stopped with its process group and counts as failed.
  Limits count awake time (`time.monotonic`), so sleep cannot cause a false timeout: lateness
  is a separate check. SIGTERM (launchd stopping the job) now stops the running step too and
  records the run as interrupted.
- *Ready-by time* (`ready_by`, default 09:00 local, before the open). A run under way at that
  time is late: stale in `status` while running and afterwards until the next run. A full run
  started by hand after it is not late.
- *Notifications* after full runs that need attention (failed or timed-out steps, late, any
  stale or missing store), through `osascript`: no credentials, nothing to configure.
  Rejected: e-mail or phone push (credentials in the environment, an outside service) for now;
  `_notify.after_run` takes a `send` function if one is wanted later.
