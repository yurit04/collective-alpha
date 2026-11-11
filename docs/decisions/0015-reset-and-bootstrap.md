# 0015: reset and bootstrap scripts

Date: 2026-11-11. Status: accepted.

## Problem
A fresh machine needs about fifteen commands across six CLIs, in the right order (the security
master before vinculum, EDGAR profiles before vinculum, minute bars before the intraday
summaries, vinculum before the risk model), then the schedules: launchd on the Mac, hand-made
systemd units on the Linux box that were not in the repo. Going back to a clean state means
finding every job and every store, and some stores may have been moved through
`collective.toml` or `*_DATA_ROOT`.

## Decision
Two bash scripts in `tools/`, plus the Linux units in `tools/systemd/`:

- **`tools/bootstrap.sh`** runs each package's own CLI in order: setup (`uv sync`, the config
  file, `subspace init`, `cortical init`), massive, edgar, finra, vinculum, nanoprobe, queen,
  services, status. It never reimplements a step: a backfill's own resume logic, disk guard and
  plan limits apply. A failed step stops it (later steps need the earlier ones) and says how to
  resume (`--from STEP`). One question before the long download; `--yes` for scripts.
- **`tools/reset.sh`** removes every scheduled job (all `com.collective-alpha.*` launchd plists;
  all `collective-*` systemd user units and the crontab lines bootstrap adds), refuses while a
  collective CLI from this checkout is still running (`--force` stops it: SIGTERM, then SIGKILL
  after 60 s), lists every store with its size and deletes after the user types `delete`.
- **Stores come from `collective roots`**, a new read-only command in unicomplex that resolves
  each root through the package's public settings, as the CLIs do. The scripts never hard-code
  `~/market_data`. Bootstrap writes the resolved roots to `~/.config/collective-alpha/roots.env`,
  which `nightly_sync.sh` and the systemd units read: the Linux equivalent of the roots the
  launchd plist carries.

Choices:
- **The trading journal stays** (`transwarp_conduit`, kind `journal`): order sheets and fills
  are the record of real or paper trades, not data that can be downloaded again. A reset
  deletes it only with `--include-journal`.
- **The stream runs on the Mac by default, on Linux only with `--stream`.** Massive allows one
  websocket per API key; a second machine's stream would push the first one off.
- **Steps run one after another.** EDGAR and FINRA could download next to the Massive backfill,
  saving about an hour of a run that takes many, at the price of interleaved logs and harder
  failures.
- **Jobs are removed generically** (by label or unit-name prefix), not through each package's
  `service remove`: it works with a broken or missing `.venv`, and finds schedules a package no
  longer knows about.
- **WSL2 without systemd** gets a crontab line for the nightly run and a note on turning systemd
  on (`/etc/wsl.conf`); the stream needs systemd there.
- **macOS: the bootstrap runs under `caffeinate -i -s`**, for the same reason the nightly job
  does (0012 addendum).
- **Bash 3.2.** macOS ships it: no `mapfile`, no associative arrays, empty arrays expanded with `${a[@]+...}`.

## Consequences
- `tests/test_lifecycle_scripts.py` runs both scripts' dry runs against temporary stores, so CI
  (Ubuntu) exercises the Linux paths and the Mac exercises the launchd ones.
- `collective roots` prints tab-separated columns the scripts read; changing them means
  changing the scripts.
- A new store means a line in `unicomplex._roots`; a new first-time step means a function in
  `bootstrap.sh` (and its nightly counterpart a `Step` in `STEPS`).
