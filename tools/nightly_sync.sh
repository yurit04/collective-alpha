#!/usr/bin/env bash
# Catch up every data source, then rebuild vinculum.
#
# Each sync resumes from its own last stored day, so a machine that was off for a few days
# catches up on the next run. Run by the collective-sync systemd user timer
# (`systemctl --user cat collective-sync.timer collective-sync.service`), or by hand:
#
#   tools/nightly_sync.sh           # skips if the last full run succeeded < 3 hours ago
#   tools/nightly_sync.sh --force   # always runs
#
# Exits 1 if any step failed; re-running retries only what is missing.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/collective-alpha
LAST_OK=$STATE_DIR/last_sync_ok
SKIP_IF_OK_WITHIN=$((3 * 3600))

mkdir -p "$STATE_DIR"
cd "$REPO" || exit 1

# One sync at a time (timer, startup run and manual runs share this lock).
exec 9>"$STATE_DIR/sync.lock"
if ! flock -n 9; then
    echo "another sync is already running; exiting"
    exit 0
fi

if [ "${1:-}" != "--force" ] && [ -f "$LAST_OK" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$LAST_OK") ))
    if [ "$age" -lt "$SKIP_IF_OK_WITHIN" ]; then
        echo "last full sync succeeded $((age / 60)) min ago; skipping (use --force to run anyway)"
        exit 0
    fi
fi

# Right after WSL starts the network can take a moment.
for _ in $(seq 30); do
    getent hosts api.massive.com > /dev/null && break
    sleep 10
done

failed=()
run() {
    echo "=== $(date '+%F %T') $*"
    if ! "$UV" run "$@"; then
        failed+=("$*")
        echo "=== FAILED: $*"
    fi
}

# Same order as the macOS schedule: Massive, SEC, FINRA, then the join.
run subspace sync
run cortical sync
run sensor sync
run vinculum build   # rebuilds from whatever is stored, so it runs even if a sync failed

if [ ${#failed[@]} -gt 0 ]; then
    echo "=== $(date '+%F %T') done with failures: ${failed[*]}"
    exit 1
fi
touch "$LAST_OK"
echo "=== $(date '+%F %T') all steps OK"
