# unicomplex (core, depends on massive-subspace-stream, edgar-cortical-node, finra-sensor-array, vinculum, nanoprobe-array, borg-queen, transwarp-conduit, adaptation-matrix)

Orchestration: `collective run` (every nightly sync and build in order, one lock, one log) and
`collective status` (a read-only freshness report over the data stores).
User docs: README.md in this folder. Design: docs/decisions/0012-unicomplex.md (phase 2 in its addendum);
catch-up runs and battery deferral: docs/decisions/0018-catch-up-runs.md.

## Public API (`from unicomplex import ...`)

| Name | Use |
|---|---|
| `collect(massive=None, edgar=None, finra=None, vinculum=None, risk=None, conduit_root=None, now=None, runner=None, nanoprobe=None, adaptation=None)` | every check as `list[Check]`; stores default to each package's configured one (`Nanoprobe` on the same Massive store), `runner` to `Runner()` |
| `Check` | source, name, state (ok / stale / info / missing), last, expected, detail; `.to_dict()` |
| `render(checks, now=None)` | the table with a verdict line |
| `STATES` | the four states |
| `Runner(settings=None, steps=STEPS, execute=None, clock=None, sleep=time.sleep, resolve=executable, power=power_source)` | `.run(only, skip, force, dry_run, echo, quiet, scheduled) -> RunResult`, `.plan(only, skip)`, `.history(n)`, `.last(full=True)`, `.running()`, `.todo(now)` (what a scheduled start does: full / catch-up / done), `.day_runs(now)`, `.outstanding(runs)` |
| `STEPS` / `Step` | the nightly steps in order: name, command, busy_exit, note, timeout_minutes (awake time; 0 = none), needs (earlier steps whose output it reads) |
| `RunResult` / `StepResult` | run status (ok / failed / deferred / skipped / locked / interrupted / dry-run), mode (full / partial / catch-up), catch_up_of, steps (ok / failed / busy / timeout / interrupted / deferred), log; `.exit_code`, `.failed` |
| `roots()` / `Root` | every store's root as its package resolves it (settings, collective.toml, `*_DATA_ROOT`): name, kind (`data`; `archive` for adaptation-matrix, which a smaller Massive plan cannot rebuild; `journal` for transwarp-conduit), env, path; `.to_dict()`. Creates nothing |
| `load_settings()` / `Settings` / `ConfigError` | data_root (`UNICOMPLEX_DATA_ROOT`, default ~/market_data/unicomplex), run_hour/minute, busy retry and wait minutes, skip_if_ok_hours, step_timeout_minutes (per step), ready_by ("09:00"), notify (true), catch_up_times (08:30 ... 18:30), require_ac (true); `.schedule_times()`; `[unicomplex]` in collective.toml |

CLI: `collective status [--json]` (exit 1 when anything is stale or missing),
`collective run [--only a,b] [--skip c] [--force] [--dry-run] [--quiet] [--scheduled]` (exit 1 when a step
failed; `--scheduled` is what the job runs), `collective runs [-n N] [--json]`, `collective roots [--json]` (tab-separated: name, kind, env, path;
read by tools/reset.sh and tools/bootstrap.sh, so keep the columns),
`collective service install [--replace] | remove | status`.

## Internals (private)

```
_checks.py    one function per source (massive_checks, edgar_checks, finra_checks, vinculum_checks,
              nanoprobe_checks, risk_checks, adaptation_checks, conduit_checks, nightly_checks), the expectations,
              collect(), render(), to_json()
_config.py    settings
_runner.py    Step, STEPS, Runner (lock, per-run log, runs.jsonl, busy retries, skip-if-recent,
              time limits: StepTimeout, process-group stop, SIGTERM -> interrupted; scheduled
              starts: todo/outstanding, catch-ups, power_source and battery deferral)
_roots.py     Root, roots(): each package's data root through its public settings
_notify.py    after_run(): problems after a full run (failed steps, late, stale/missing checks) -> macOS notification
_services.py  launchd plist (com.collective-alpha.unicomplex.nightly, one start per schedule time),
              replacing the per-package schedules
cli.py        `collective`
```

Invariants:
- Writes only under its own root: `_run/nightly.lock`, `runs/<run_id>.log`, `runs.jsonl`,
  `_logs/nightly.launchd.log`. `status`, `roots` and `run --dry-run` write nothing.
- A step is the package's own CLI run as a subprocess; never call another package's build or
  sync in-process, and never reimplement one here. A new nightly job = one `Step` in `STEPS`
  (and its package as a dependency).
- Steps run strictly one after another; a failed step never stops the later ones. Only a step's
  `busy_exit` code is retried (another job holds that package's writer lock).
- A step runs in its own process group; past its time limit (or when the run is interrupted)
  the whole group is stopped (SIGTERM, SIGKILL after 60 s), so spawned workers never outlive it.
  Limits count awake time (`time.monotonic` stops during sleep on macOS): a sleeping Mac makes
  a run late, never a timeout. Lateness is `ready_by`: a run under way at that time is late
  (stale in `status`, and notified); a run started by hand after it is not.
- Only full runs notify, and only when something needs attention; partial runs and catch-ups never do.
- The scheduled job always runs `collective run --scheduled`; the run decides between the full
  run, a catch-up and nothing (`todo`). What is left to do comes from the day's full run and
  every run after it (`outstanding`), so a run by hand counts. A new step that reads another
  step's output declares it in `needs` (earlier steps only), or a catch-up will not rebuild it.
- A scheduled run never starts a step on battery power (macOS, `require_ac`): the step and the
  rest are `deferred`. A run by hand only warns. A catch-up defers a step whose input failed
  again in it; a full run still runs every step.
- One run at a time (flock on `_run/nightly.lock`); a second run exits 0 and records nothing.
- Only public APIs of the other packages (`Store.coverage/stream_status/trading_days`,
  `EdgarStore.status`, `FinraStore.status`, `Vinculum.status`, `Nanoprobe.status`, `RiskModel.status`,
  `transwarp_conduit.Journal/OrderSheet/load_settings`, each package's `load_settings` /
  `ENV_ROOT` for the plist): a status field another package adds or renames is picked up here.
- The plist never carries credentials; it never touches the Massive stream service.
- The launchd job runs under `caffeinate -i -s` (no sleep until the run exits); keep it: a
  sleeping Mac otherwise runs the steps in 45-second Power Nap bursts.
- One failing store reports `missing` with its error; the other checks still run.
- Expectations follow the schedules documented in each package (sync times, publication lags);
  change them here when a schedule changes there.
