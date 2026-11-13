# unicomplex

Orchestration for the collective: one nightly run of every sync and build, and one read-only
status of how fresh the stores are.

```bash
uv run collective run --dry-run   # the steps and the commands they run; runs nothing
uv run collective run             # run them now, one after another (about 15 minutes)
uv run collective runs            # recent runs: status and minutes per step
uv run collective status          # how fresh the stores are against the nightly schedule
uv run collective status --json   # the same as JSON (one object per check)
uv run collective roots           # where every store lives: name, kind, variable, path (tab-separated)
```

A fresh machine is built by `tools/bootstrap.sh` and taken back to nothing by `tools/reset.sh`
(both in the repo root's README); both read the stores from `collective roots`.

## The nightly run

`collective run` runs these in order, each only after the previous one has finished:

| Step | Command | What it does |
|---|---|---|
| massive | `subspace sync` | bars, flat files, reference data and the security master through yesterday |
| edgar | `cortical sync` | SEC company facts filed since the last sync |
| finra | `sensor sync` | FINRA short interest and daily short sale volume |
| vinculum | `vinculum build` | market cap, universes and the joins |
| nanoprobe | `nanoprobe build` | intraday summaries for the years behind the minute bars |
| queen | `queen build` | the risk model |

- A failed step does not stop the rest (vinculum rebuilds from whatever is stored); the run
  exits 1 and `collective status` shows it as stale with the failed steps.
- If another Massive job holds the writer lock, `subspace sync` is retried every 5 minutes for
  up to 2 hours.
- One run at a time: a second `collective run` while one is going exits 0 and does nothing.
- A full run that succeeded in the last 3 hours makes `run` a no-op; `--force` runs anyway.
- `--only massive,vinculum` or `--skip queen` run part of it (partial runs don't count as the
  nightly run in `status`). `--quiet` prints only the progress lines.
- Each step has a time limit (massive 4 h, edgar 4 h, finra 30 min, vinculum 1 h, nanoprobe
  3 h, queen 1 h). A step past it is stopped with its workers and counts as failed
  (`timeout`); the next step starts. The limits count time the Mac is awake, so sleep never
  trips them.
- A full run that needs attention ends with a macOS notification: a step failed or timed out,
  the run was still going at the `ready_by` time (09:00), or `collective status` finds a store
  stale or missing afterwards. Nothing is sent when all is well; partial runs never notify. The
  notifications come from Script Editor (allow it under System Settings > Notifications).

```toml
[unicomplex]
ready_by = "09:00"       # a run still going at this time is late (status: stale)
notify = true

[unicomplex.step_timeout_minutes]   # override a step's limit; 0 = no limit
edgar = 300
```

Everything goes under `~/market_data/unicomplex` (`UNICOMPLEX_DATA_ROOT`): `runs/<run_id>.log`
(every step's output), `runs.jsonl` (one line per run) and `_run/nightly.lock`.

### Scheduling it

macOS, one launchd job at 06:30 Tuesday to Saturday (machine local time):

```bash
uv run collective service install --replace   # removes the four per-package schedules first
uv run collective service status
uv run collective service remove
```

The job runs under `caffeinate`, so the Mac stays awake until the run ends. A Mac asleep at
06:30 starts it at its next Power Nap wake (within about 15 minutes). Without that, it would
sleep again after ~45 seconds and the steps would crawl along in short bursts: three minutes of
work took 70 on 2026-11-06. `caffeinate` holds only on AC power: on battery, or with the lid
closed and no external display, macOS sleeps anyway (on 2026-11-10 a run on battery took 12
hours). Keep the Mac plugged in, lid open, for the 06:30 run; `pmset -g log` shows whether it
slept, and a run still going at `ready_by` shows as stale and notifies. To wake the Mac at 06:29 itself (a system setting, needs sudo):
`sudo pmset repeat wakeorpoweron TWRFS 06:29:00`.

Without `--replace`, installing refuses while the old Massive sync, EDGAR sync, FINRA sync or
vinculum build schedules exist, since they would run next to it. The Massive stream service is
never touched; install it on its own with `uv run subspace service install --only stream`. The time is set in `collective.toml`:

```toml
[unicomplex]
run_hour = 6
run_minute = 30
```

Linux: `tools/nightly_sync.sh` waits for the network and calls `collective run`. The systemd
user units that start it (Tue-Sat at `run_hour:run_minute`, `Persistent=true`) and the optional
stream unit are in `tools/systemd/`; `tools/bootstrap.sh --only services` installs them. On WSL2
without systemd it adds a crontab line instead.

## Status

`collective status` exits 1 when anything is stale or missing, so a cron job or a shell prompt
can act on it. It only reads.

### What it checks

| Source | Check | Fresh when |
|---|---|---|
| massive | `bars_day`, `bars_minute` | the last stored day is the last completed NYSE session before today (the 06:30 sync) |
| massive | `stream` | the stream service is running and, in market hours, has received a message in the last 30 minutes |
| edgar | `facts` | `sync_through` is within 3 days of today (the sync runs Tue-Sat through yesterday) |
| edgar | `series` | the materialized series were built from the current facts (`cortical series`) |
| finra | `short_interest` | the last settlement date is within 26 days (two cycles plus the 7-business-day publication lag) |
| finra | `short_volume` | the last day is the last completed session |
| vinculum | `market_cap` | the panel runs through the last completed session (`vinculum build` after the syncs) |
| nanoprobe | `summaries` | the daily intraday summaries reach the last completed session (`nanoprobe build` after vinculum); the detail says when they trail the minute bars |
| borg-queen | `risk_model` | the last month end in the model is within 45 days (rebuilt by the nightly run) |
| adaptation-matrix | `costs` | informational: days of trades and quotes measured, by set, and the store's size (built once with `adapt run`, not nightly) |
| transwarp-conduit | `orders` | informational: the last order sheet and its status, fills journaled (read from the journal, no Gateway connection) |
| unicomplex | `nightly_run` | the last full `collective run` after the latest scheduled time (Tue-Sat 06:30, 15 minutes' grace) succeeded and finished by `ready_by` (09:00); stale while one is still running past it; info while one is running before it, or before the first |

States: `ok`, `stale` (behind the schedule), `info` (no expectation), `missing` (the store is
absent or unreadable; the error is shown, the other checks still run).

```python
from unicomplex import collect, render
checks = collect()                 # list[Check]; pass store objects or now= for tests
print(render(checks))
```

## Later phases
Rebalance-day orchestration for transwarp-conduit.
