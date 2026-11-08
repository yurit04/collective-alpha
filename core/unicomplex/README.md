# unicomplex

Orchestration for the collective: one nightly run of every sync and build, and one read-only
status of how fresh the stores are.

```bash
uv run collective run --dry-run   # the steps and the commands they run; runs nothing
uv run collective run             # run them now, one after another (about 15 minutes)
uv run collective runs            # recent runs: status and minutes per step
uv run collective status          # how fresh the stores are against the nightly schedule
uv run collective status --json   # the same as JSON (one object per check)
```

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
work took 70 on 2026-11-06. With the lid closed and no external display macOS may still sleep;
`pmset -g log` shows it. To wake the Mac at 06:29 itself (a system setting, needs sudo):
`sudo pmset repeat wakeorpoweron TWRFS 06:29:00`.

Without `--replace`, installing refuses while the old Massive sync, EDGAR sync, FINRA sync or
vinculum build schedules exist, since they would run next to it. The Massive stream service is
never touched; install it on its own with `uv run subspace service install --only stream`. The time is set in `collective.toml`:

```toml
[unicomplex]
run_hour = 6
run_minute = 30
```

Linux: `tools/nightly_sync.sh` (the systemd timer) waits for the network and calls
`collective run`.

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
| transwarp-conduit | `orders` | informational: the last order sheet and its status, fills journaled (read from the journal, no Gateway connection) |
| unicomplex | `nightly_run` | the last full `collective run` after the latest scheduled time (Tue-Sat 06:30, 15 minutes' grace) succeeded; info while one is running or before the first |

States: `ok`, `stale` (behind the schedule), `info` (no expectation), `missing` (the store is
absent or unreadable; the error is shown, the other checks still run).

```python
from unicomplex import collect, render
checks = collect()                 # list[Check]; pass store objects or now= for tests
print(render(checks))
```

## Later phases
Rebalance-day orchestration for transwarp-conduit.
