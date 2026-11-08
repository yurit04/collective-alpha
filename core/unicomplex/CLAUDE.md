# unicomplex (core, depends on massive-subspace-stream, edgar-cortical-node, finra-sensor-array, vinculum, nanoprobe-array, borg-queen, transwarp-conduit)

Orchestration: `collective run` (every nightly sync and build in order, one lock, one log) and
`collective status` (a read-only freshness report over the data stores).
User docs: README.md in this folder. Design: docs/decisions/0012-unicomplex.md (phase 2 in its addendum).

## Public API (`from unicomplex import ...`)

| Name | Use |
|---|---|
| `collect(massive=None, edgar=None, finra=None, vinculum=None, risk=None, conduit_root=None, now=None, runner=None, nanoprobe=None)` | every check as `list[Check]`; stores default to each package's configured one (`Nanoprobe` on the same Massive store), `runner` to `Runner()` |
| `Check` | source, name, state (ok / stale / info / missing), last, expected, detail; `.to_dict()` |
| `render(checks, now=None)` | the table with a verdict line |
| `STATES` | the four states |
| `Runner(settings=None, steps=STEPS, execute=None, clock=None, sleep=time.sleep, resolve=executable)` | `.run(only, skip, force, dry_run, echo, quiet) -> RunResult`, `.plan(only, skip)`, `.history(n)`, `.last(full=True)`, `.running()` |
| `STEPS` / `Step` | the nightly steps in order: name, command, busy_exit, note |
| `RunResult` / `StepResult` | status (ok / failed / skipped / locked / interrupted / dry-run), steps, log; `.exit_code`, `.failed` |
| `load_settings()` / `Settings` / `ConfigError` | data_root (`UNICOMPLEX_DATA_ROOT`, default ~/market_data/unicomplex), run_hour/minute, busy retry and wait minutes, skip_if_ok_hours; `[unicomplex]` in collective.toml |

CLI: `collective status [--json]` (exit 1 when anything is stale or missing),
`collective run [--only a,b] [--skip c] [--force] [--dry-run] [--quiet]` (exit 1 when a step
failed), `collective runs [-n N] [--json]`, `collective service install [--replace] | remove | status`.

## Internals (private)

```
_checks.py    one function per source (massive_checks, edgar_checks, finra_checks, vinculum_checks,
              nanoprobe_checks, risk_checks, conduit_checks, nightly_checks), the expectations,
              collect(), render(), to_json()
_config.py    settings
_runner.py    Step, STEPS, Runner (lock, per-run log, runs.jsonl, busy retries, skip-if-recent)
_services.py  launchd plist (com.collective-alpha.unicomplex.nightly), replacing the per-package schedules
cli.py        `collective`
```

Invariants:
- Writes only under its own root: `_run/nightly.lock`, `runs/<run_id>.log`, `runs.jsonl`,
  `_logs/nightly.launchd.log`. `status` and `run --dry-run` write nothing.
- A step is the package's own CLI run as a subprocess; never call another package's build or
  sync in-process, and never reimplement one here. A new nightly job = one `Step` in `STEPS`
  (and its package as a dependency).
- Steps run strictly one after another; a failed step never stops the later ones. Only a step's
  `busy_exit` code is retried (another job holds that package's writer lock).
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
