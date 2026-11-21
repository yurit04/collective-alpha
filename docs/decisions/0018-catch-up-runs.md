# 0018: catch-up runs and no steps on battery

Date: 2026-11-20. Status: accepted.

## Problem
The nightly run starts once, at 06:30 Tuesday to Saturday. A failed step waited a whole day
for the next run: on 2026-11-14 the SEC's rate limit failed the EDGAR sync, and on 2026-11-17
the Mac was unplugged at 06:37. `caffeinate -s` holds only on AC power, so the Mac slept in
dark-wake bursts, DNS lookups failed, and EDGAR and FINRA failed after a run of 14 hours
(vinculum took 2.8 hours instead of 3 minutes). The data stayed stale until the next morning
(Monday's failure until Tuesday), unless someone re-ran the steps by hand.

## Decision
- **One scheduled job, several starts.** The launchd job (and the Linux timer or cron lines)
  start `collective run --scheduled` at the run time and at each of `catch_up_times`
  (default 08:30, 10:30, 12:30, 14:30, 18:30), Tuesday to Saturday. Each start decides for
  itself (`Runner.todo`):
  - no full run for the scheduled day yet: the full run, as before;
  - the day's runs left steps to do: a **catch-up** of just those;
  - else nothing (not recorded).
  launchd and systemd fire once at wake for starts missed while asleep, and the existing lock
  keeps a catch-up from running next to the full run.
- **What is left to do** (`Runner.outstanding`) comes from the day's full run and every run
  after it, catch-ups and runs by hand alike. It is each step whose latest result is not ok,
  plus each step that needs one of those (`Step.needs`), plus each step that last succeeded
  before a step it needs did. vinculum needs massive, edgar and finra; nanoprobe needs massive
  and vinculum; queen needs massive and vinculum. So a failed EDGAR sync is re-run with
  vinculum, nanoprobe and queen after it, not just on its own: vinculum built on the old facts
  that morning.
- **A catch-up does not rebuild on an input that failed again.** Its dependent steps are
  deferred ("waits for edgar") instead of repeating the morning's build every two hours. A
  full run keeps its rule: builds run even when a sync failed.
- **No steps on battery.** Before each step a scheduled run reads the power source
  (`pmset -g ps`). On battery the step and the ones after it are "deferred" and the run ends;
  a later start picks them up once the Mac is plugged in. A run by hand only warns. On Linux,
  or when `pmset` cannot tell, steps run. `require_ac = false` turns this off.
- **Status.** `collective status` shows a failed or deferred day as ok once the runs after it
  leave nothing to do ("caught up by ... at 10:32"); until then it is stale, with how many
  catch-ups tried and what is still to do, and "plug the Mac in" when the last run deferred
  on battery. `collective runs` marks catch-ups ("[catch-up of 20261117-063448]").
- **Notifications** stay as they were: only full runs notify. The 06:30 notification says what
  failed or was deferred; catch-ups are quiet.

## Alternatives
- **Wait for AC inside the run.** A run waiting on battery sleeps with the Mac anyway, and
  holds the lock; ending the run and letting the next start pick up is simpler and needs no
  timer of its own.
- **A second launchd job for catch-ups.** Two labels to install, remove and report on; one job
  whose command decides keeps `service install`, `reset.sh` and `bootstrap.sh` as they were.
- **Retrying a failed step inside the run.** It helps a brief network error, not a sleeping
  Mac, a 10-minute SEC block or an unplugged laptop; a start hours later does.
- **Running the nightly only on the Linux box.** It stays an option (the timer gets the same
  catch-ups); the Mac keeps the stream and the stores the notebooks read.

## Consequences
- A failure is retried up to five times the same day; the data is usually current by noon
  instead of the next morning.
- A step already running when the Mac is unplugged is not stopped; if it fails, a catch-up
  re-runs it.
- A catch-up that fails again does not notify; `collective status` (exit 1) shows it.
- Reinstall the job after updating (`collective service install`; on Linux
  `tools/bootstrap.sh --only services`) to get the new starts and `--scheduled`.
