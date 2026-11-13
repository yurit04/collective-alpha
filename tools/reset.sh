#!/usr/bin/env bash
# Back to a fresh start: stop every collective-alpha job, then delete the data stores.
#
#   tools/reset.sh --dry-run            # show what would be stopped and deleted; change nothing
#   tools/reset.sh                      # stop jobs, list the stores with sizes, ask, delete
#   tools/reset.sh --yes                # no question (scripts)
#   tools/reset.sh --include-journal    # also delete the transwarp-conduit journal (order sheets, fills)
#   tools/reset.sh --include-archive    # also delete the adaptation-matrix cost archive (from trades and
#                                       # quotes the Massive plan may no longer download)
#   tools/reset.sh --force              # stop collective processes still running (a backfill, a run)
#
# Jobs: on macOS every ~/Library/LaunchAgents/com.collective-alpha.*.plist (the nightly run, the
# stream, the old per-package schedules); on Linux every collective-* systemd user unit and the
# crontab lines tools/bootstrap.sh adds. Stores: whatever `collective roots` resolves (each
# package's own settings, collective.toml, *_DATA_ROOT), so a moved root is found too.
#
# Never touched: ~/.massive.env, collective.toml, this repository and its .venv.
# Rebuild with tools/bootstrap.sh. Works with macOS's bash 3.2 and on Linux / WSL2.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
OS=$(uname -s)
CRON_TAG="# collective-alpha"
ROOTS_ENV="$HOME/.config/collective-alpha/roots.env"
TOOLS="subspace|cortical|sensor|vinculum|nanoprobe|queen|conduit|adapt|collective"

YES=0 DRY=0 JOURNAL=0 ARCHIVE=0 FORCE=0
usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) YES=1 ;;
        -n|--dry-run) DRY=1 ;;
        --include-journal) JOURNAL=1 ;;
        --include-archive) ARCHIVE=1 ;;
        -f|--force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '%s\n' "$*"; }
head_() { printf '\n== %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
act()  {   # run a command, or only show it under --dry-run
    if [ "$DRY" = 1 ]; then say "  would run: $*"; else "$@"; fi
}

[ -x "$UV" ] || die "uv not found (install: https://docs.astral.sh/uv/)"
case "$OS" in Darwin|Linux) ;; *) die "unsupported system: $OS (macOS or Linux / WSL2 only)" ;; esac

# ------------------------------------------------------------------ 1. scheduled jobs
stop_launchd() {
    local agents="$HOME/Library/LaunchAgents" found=0 plist label
    for plist in "$agents"/com.collective-alpha.*.plist; do
        [ -e "$plist" ] || continue
        found=1
        label=$(basename "$plist" .plist)
        say "  $label"
        act launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
        act rm -f "$plist"
    done
    [ "$found" = 1 ] || say "  no launchd jobs installed"
}

systemd_user() {   # is a systemd user manager reachable? (WSL2 without systemd: no)
    command -v systemctl > /dev/null && systemctl --user show-environment > /dev/null 2>&1
}

stop_systemd() {
    local units unit dir="$HOME/.config/systemd/user"
    if systemd_user; then
        # Timers first, so nothing starts a service while it is being stopped.
        units=$(systemctl --user list-unit-files 'collective-*' --no-legend 2>/dev/null \
                | awk '{print $1}' | sort -r)
        if [ -z "$units" ]; then
            say "  no systemd user units installed"
        fi
        for unit in $units; do
            say "  $unit"
            act systemctl --user disable --now "$unit" 2>/dev/null
            [ -f "$dir/$unit" ] && act rm -f "$dir/$unit"
        done
        [ -n "$units" ] && act systemctl --user daemon-reload
    else
        say "  no systemd user manager (WSL2 without systemd?); checking cron only"
    fi
    if command -v crontab > /dev/null && crontab -l 2>/dev/null | grep -q -e "$CRON_TAG" -e nightly_sync.sh; then
        say "  crontab lines:"
        crontab -l 2>/dev/null | grep -e "$CRON_TAG" -e nightly_sync.sh | sed 's/^/    /'
        if [ "$DRY" = 1 ]; then
            say "  would remove them from the crontab"
        else
            crontab -l 2>/dev/null | grep -v -e "$CRON_TAG" -e nightly_sync.sh | crontab -
        fi
    fi
}

head_ "Stopping scheduled jobs"
if [ "$OS" = Darwin ]; then stop_launchd; else stop_systemd; fi

# ------------------------------------------------------------------ 2. running processes
# Any collective CLI started from this checkout's .venv: the stream, a backfill, a nightly run.
running() {
    ps -eo pid=,args= 2>/dev/null | grep -E "$REPO/\.venv/bin/($TOOLS)( |$)"
}

head_ "Checking for running collective processes"
procs=$(running)
if [ -z "$procs" ]; then
    say "  none"
elif [ "$DRY" = 1 ]; then
    say "$procs" | sed 's/^/  /'
    say "  (a real reset needs --force to stop these)"
elif [ "$FORCE" = 0 ]; then
    say "$procs" | sed 's/^/  /'
    die "collective processes are still running; let them finish, or re-run with --force to stop them"
else
    pids=$(say "$procs" | awk '{print $1}')
    say "$procs" | sed 's/^/  stopping /'
    # SIGTERM: `collective run` records the run as interrupted and stops its step's process
    # group; the stream flushes what it buffered.
    kill -TERM $pids 2>/dev/null
    for _ in $(seq 60); do
        [ -z "$(running)" ] && break
        sleep 1
    done
    left=$(running | awk '{print $1}')
    if [ -n "$left" ]; then
        warn "still running after 60 s, killing: $left"
        kill -KILL $left 2>/dev/null
        sleep 2
    fi
fi

# ------------------------------------------------------------------ 3. what to delete
head_ "Data stores"
roots=$("$UV" run --project "$REPO" --quiet collective roots) || die "\`collective roots\` failed"

targets=""
total=0
while IFS="$(printf '\t')" read -r name kind _env path; do
    [ -n "$name" ] || continue
    case "$path" in
        ""|/|"$HOME"|"$HOME/"|"$REPO"|"$REPO"/*) die "refusing to delete $name root '$path'" ;;
    esac
    case "$HOME/" in "$path"/*) die "refusing to delete $name root '$path' (it holds your home folder)" ;; esac
    case "$REPO/" in "$path"/*) die "refusing to delete $name root '$path' (it holds this repository)" ;; esac
    if [ ! -e "$path" ]; then
        printf '  %-11s %-8s %s  (absent)\n' "$name" "-" "$path"
        continue
    fi
    size=$(du -sh "$path" 2>/dev/null | cut -f1)
    if [ "$kind" = journal ] && [ "$JOURNAL" = 0 ]; then
        printf '  %-11s %-8s %s  (kept: trading journal; --include-journal deletes it)\n' "$name" "$size" "$path"
        continue
    fi
    if [ "$kind" = archive ] && [ "$ARCHIVE" = 0 ]; then
        printf '  %-11s %-8s %s  (kept: cost archive, cannot be re-downloaded on a smaller plan; --include-archive deletes it)\n' "$name" "$size" "$path"
        continue
    fi
    kb=$(du -sk "$path" 2>/dev/null | cut -f1)
    total=$((total + ${kb:-0}))
    printf '  %-11s %-8s %s\n' "$name" "$size" "$path"
    targets="$targets$path
"
done <<EOF
$roots
EOF

[ -f "$ROOTS_ENV" ] && say "  (and $ROOTS_ENV, written by tools/bootstrap.sh)"

if [ -z "$targets" ]; then
    [ "$DRY" = 1 ] || rm -f "$ROOTS_ENV"
    head_ "Nothing to delete: the system is already clean. Rebuild with tools/bootstrap.sh."
    exit 0
fi
say "  total: $(awk -v kb="$total" 'BEGIN { if (kb >= 1048576) printf "%.1f GB", kb / 1048576; else printf "%.0f MB", kb / 1024 }')"

if [ "$DRY" = 1 ]; then
    head_ "Dry run: nothing was stopped or deleted."
    exit 0
fi

# ------------------------------------------------------------------ 4. delete
if [ "$YES" = 0 ]; then
    [ -t 0 ] || die "not a terminal; pass --yes to delete without the question"
    printf '\nThis deletes the stores listed above and cannot be undone. Type "delete" to go on: '
    read -r answer
    [ "$answer" = delete ] || die "nothing deleted"
fi

head_ "Deleting"
parents=""
while IFS= read -r path; do
    [ -n "$path" ] || continue
    say "  $path"
    rm -rf "$path" || warn "could not delete all of $path"
    parents="$parents$(dirname "$path")
"
done <<EOF
$targets
EOF
rm -f "$ROOTS_ENV"
# The folder that held the stores (~/market_data), only if nothing else is left in it (a
# .DS_Store that Finder left there does not count).
say "$parents" | sort -u | while IFS= read -r dir; do
    [ -n "$dir" ] && [ "$dir" != "$HOME" ] && [ -d "$dir" ] || continue
    [ "$(ls -A "$dir")" = .DS_Store ] && rm -f "$dir/.DS_Store"
    rmdir "$dir" 2>/dev/null && say "  $dir (empty)"
done

head_ "Done. Rebuild with tools/bootstrap.sh."
