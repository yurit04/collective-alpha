#!/usr/bin/env bash
# The nightly run on Linux: wait for the network, then `collective run`, which syncs Massive,
# SEC and FINRA, then builds vinculum, the intraday summaries and the risk model, one after
# another, under one lock and with one log per run (core/unicomplex/README.md).
#
# Each sync resumes from its own last stored day, so a machine that was off for a few days
# catches up on the next run. Run by the collective-sync systemd user timer
# (`systemctl --user cat collective-sync.timer collective-sync.service`), or by hand:
#
#   tools/nightly_sync.sh           # skips if the last full run succeeded < 3 hours ago
#   tools/nightly_sync.sh --force   # always runs
#
# Exits 1 if any step failed; re-running retries only what is missing. A second run while one
# is going exits 0. History: `uv run collective runs`.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
cd "$REPO" || exit 1

# Right after WSL starts the network can take a moment.
for _ in $(seq 30); do
    getent hosts api.massive.com > /dev/null && break
    sleep 10
done

exec "$UV" run collective run "$@"
