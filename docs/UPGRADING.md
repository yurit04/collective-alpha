# Upgrading: manual steps after a `git pull`

Most changes take effect on the next run once you `git pull` (the workspace packages are
installed editable). The ones below also need something done by hand on each machine. Newest
first. Delete an entry once every machine has done it.

## 2026-11-21: reinstall the nightly schedule (decision 0018)

**Who:** every machine that runs the nightly job. The Mac was done on 2026-11-20. **Still to
do: the Linux box.**

**Why:** the nightly job now starts at 06:30 and again at 08:30, 10:30, 12:30, 14:30 and 18:30
(Tue-Sat). Each start runs `collective run --scheduled`, which re-runs only what failed earlier
that day, or does nothing. The systemd timer and service installed before this change still
start one plain `collective run` at 06:30. They keep working, but get no catch-ups until
they're reinstalled.

**On the Linux box**, from the repo folder:

```bash
git pull
tools/bootstrap.sh --only services --dry-run   # shows what it will install; changes nothing
tools/bootstrap.sh --only services             # setup (uv sync) + the systemd user units
```

- This downloads nothing. `--only services` runs the quick `setup` step (`uv sync`, the
  config and credential checks) and then reinstalls `collective-sync.service` and
  `collective-sync.timer` under `~/.config/systemd/user/`.
- Leave out `--stream`: the Mac runs the stream, and Massive allows one websocket per API key.
- WSL2 without systemd: the same command rewrites the crontab lines instead (one per time).

**Check it worked:**

```bash
systemctl --user cat collective-sync.timer | grep OnCalendar      # 6 lines: 06:30 ... 18:30
systemctl --user cat collective-sync.service | grep ExecStart     # ends in nightly_sync.sh --scheduled
systemctl --user list-timers collective-sync.timer --no-pager     # the next start
uv run collective run --scheduled --dry-run                       # what that start would do
uv run collective status                                          # exit 0 = every store current
```

On WSL2 with cron: `crontab -l | grep nightly_sync` shows 6 lines ending in `--scheduled`.

**If the box was down for a while:** the timer is `Persistent=true`, so the first start after
boot runs the missed nightly. Each sync fetches everything since its last success, so the
stores catch up in one run (it can take longer than the usual 15-25 minutes).
`collective status` says when it's done.

**Options** (`[unicomplex]` in `collective.toml`; re-run `--only services` after changing them):
`catch_up_times = [...]` (`[]` turns catch-ups off). `require_ac` only matters on macOS: Linux
never defers on battery.
