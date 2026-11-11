#!/usr/bin/env bash
# Build collective-alpha from nothing: install the packages, create the stores, download every
# source, build everything derived from them, then schedule the nightly run (and the stream).
#
#   tools/bootstrap.sh --dry-run          # the steps and commands; runs nothing
#   tools/bootstrap.sh                    # everything (asks once: hours, ~150 GB with minute bars)
#   tools/bootstrap.sh --from edgar       # resume at a step (every step can be stopped and re-run)
#   tools/bootstrap.sh --only services    # one step, or a comma-separated list
#   tools/bootstrap.sh --since 2015-01-01 --no-minute   # a smaller Massive history
#
# Steps, in order (each calls the package's own CLI; a failed step stops the bootstrap):
#   setup      uv sync --all-packages, collective.toml from the example if missing, disk and
#              system checks, `subspace init` (credentials, plan), `cortical init` (SEC contact)
#   massive    subspace backfill initial, subspace identity build        (hours; ~100 GB with minute bars)
#   edgar      cortical backfill, cortical profiles                      (~30 min; 3 GB download)
#   finra      sensor backfill short-interest, sensor backfill short-volume   (~1 h; 3 GB download)
#   vinculum   vinculum build, vinculum check                            (~1 min)
#   nanoprobe  nanoprobe build                                           (~30 min; skipped with --no-minute)
#   queen      queen build                                               (minutes)
#   services   macOS: the launchd nightly run (`collective service install --replace`) and the
#              stream (`subspace service install --only stream`). Linux: the systemd user units in
#              tools/systemd/ (WSL2 without systemd: a crontab line for the nightly run)
#   status     collective status
#
# Options:
#   --from STEP     start at STEP (setup always runs: it is quick and makes the tools available)
#   --only A,B      run only these steps (plus setup)
#   --since DATE    first day of Massive history (default: the earliest, 2003-09-10)
#   --no-minute     no minute bars (and no intraday summaries)
#   --no-services   schedule nothing
#   --stream        install the live stream (default on macOS)
#   --no-stream     don't (default on Linux). Massive allows ONE websocket per API key: run the
#                   stream on one machine only.
#   -y, --yes       don't ask before the long download
#   -n, --dry-run   show what would run
#
# On macOS the whole bootstrap runs under `caffeinate` (no sleep while it works; on AC power).
# A copy of everything printed goes to <unicomplex root>/_logs/bootstrap-<time>.log.
# Undo with tools/reset.sh. Works with macOS's bash 3.2 and on Linux / WSL2.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
OS=$(uname -s)
BIN="$REPO/.venv/bin"
STEPS="setup massive edgar finra vinculum nanoprobe queen services status"
ROOTS_ENV="$HOME/.config/collective-alpha/roots.env"
UNIT_DIR="$HOME/.config/systemd/user"
CRON_TAG="# collective-alpha"

WSL=0
if [ "$OS" = Linux ] && grep -qi microsoft /proc/version 2>/dev/null; then WSL=1; fi

FROM="" ONLY="" SINCE="" MINUTE=1 SERVICES=1 YES=0 DRY=0 STREAM=""
usage() { sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'; }
need_arg() { [ $# -ge 2 ] && [ -n "$2" ] || { echo "$1 needs a value" >&2; exit 2; }; }
ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --from) need_arg "$@"; FROM=$2; shift ;;
        --only) need_arg "$@"; ONLY=$(echo "$2" | tr ',' ' '); shift ;;
        --since) need_arg "$@"; SINCE=$2; shift ;;
        --no-minute) MINUTE=0 ;;
        --no-services) SERVICES=0 ;;
        --stream) STREAM=1 ;;
        --no-stream) STREAM=0 ;;
        -y|--yes) YES=1 ;;
        -n|--dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
if [ -z "$STREAM" ]; then
    if [ "$OS" = Darwin ]; then STREAM=1; else STREAM=0; fi
fi

say()   { printf '%s\n' "$*"; }
head_() { printf '\n== %s\n' "$*"; }
warn()  { printf 'warning: %s\n' "$*" >&2; }
die()   { printf 'error: %s\n' "$*" >&2; exit 1; }
has()   { case " $1 " in *" $2 "*) return 0 ;; esac; return 1; }

case "$OS" in Darwin|Linux) ;; *) die "unsupported system: $OS (macOS or Linux / WSL2 only)" ;; esac
[ -x "$UV" ] || die "uv not found. Install it (https://docs.astral.sh/uv/getting-started/installation/), open a new shell, and run this again."
for s in $FROM $ONLY; do
    has "$STEPS" "$s" || die "unknown step '$s' (steps: $STEPS)"
done

# Which steps run.
selected=""
started=0
[ -z "$FROM" ] && started=1
for s in $STEPS; do
    [ "$s" = "$FROM" ] && started=1
    if [ "$s" = setup ]; then selected="setup"; continue; fi
    if [ -n "$ONLY" ]; then has "$ONLY" "$s" || continue; else [ "$started" = 1 ] || continue; fi
    [ "$s" = nanoprobe ] && [ "$MINUTE" = 0 ] && continue
    [ "$s" = services ] && [ "$SERVICES" = 0 ] && continue
    selected="$selected $s"
done

# macOS: keep the machine awake for the whole bootstrap (not for a dry run).
if [ "$OS" = Darwin ] && [ "$DRY" = 0 ] && [ -z "${COLLECTIVE_BOOTSTRAP_AWAKE:-}" ] && [ -x /usr/bin/caffeinate ]; then
    export COLLECTIVE_BOOTSTRAP_AWAKE=1
    exec /usr/bin/caffeinate -i -s /bin/bash "$0" ${ARGS[@]+"${ARGS[@]}"}
fi

run() {   # run a command (or show it under --dry-run); stop the bootstrap when it fails
    say "\$ $*"
    [ "$DRY" = 1 ] && return 0
    "$@"
    local code=$?
    [ "$code" = 0 ] && return 0
    printf '\nerror: `%s` failed (exit %s).\n' "$*" "$code" >&2
    printf 'Fix the cause and resume with: tools/bootstrap.sh --from %s\n' "$CURRENT" >&2
    exit "$code"
}
tool() { local name=$1; shift; run "$BIN/$name" "$@"; }

free_gb() {   # free space where $1 is (or would be created)
    local d=$1
    while [ ! -d "$d" ]; do d=$(dirname "$d"); done
    df -Pk "$d" | awk 'NR == 2 { printf "%d", $4 / 1048576 }'
}

# ------------------------------------------------------------------ steps
step_setup() {
    run "$UV" sync --all-packages --project "$REPO"
    if [ ! -f "$REPO/collective.toml" ]; then
        run cp "$REPO/collective.toml.example" "$REPO/collective.toml"
    fi

    local roots
    if ! roots=$("$UV" run --project "$REPO" --quiet collective roots 2>/dev/null); then
        [ "$DRY" = 1 ] || die "\`collective roots\` failed"
        roots=$(printf 'massive\tdata\tSUBSPACE_DATA_ROOT\t%s\nunicomplex\tdata\tUNICOMPLEX_DATA_ROOT\t%s' \
                "$HOME/market_data/massive" "$HOME/market_data/unicomplex")
        say "(dry run before the packages are installed: assuming the default stores)"
    fi
    say "Stores:"
    say "$roots" | awk -F '\t' '{ printf "  %-11s %s\n", $1, $4 }'
    MASSIVE_ROOT=$(say "$roots" | awk -F '\t' '$1 == "massive" { print $4 }')
    UC_ROOT=$(say "$roots" | awk -F '\t' '$1 == "unicomplex" { print $4 }')

    # The env file the nightly run and the systemd units read, so they see these same stores.
    if [ "$DRY" = 0 ]; then
        mkdir -p "$(dirname "$ROOTS_ENV")"
        say "$roots" | awk -F '\t' '{ printf "%s=\x27%s\x27\n", $3, $4 }' > "$ROOTS_ENV"
        say "wrote $ROOTS_ENV"
        mkdir -p "$UC_ROOT/_logs"
        LOG="$UC_ROOT/_logs/bootstrap-$(date +%Y%m%d-%H%M%S).log"
        exec > >(tee -a "$LOG") 2>&1
        say "log: $LOG"
    fi

    # System checks.
    case "$MASSIVE_ROOT" in
        /mnt/*) [ "$WSL" = 1 ] && warn "the Massive store is on the Windows drive ($MASSIVE_ROOT); WSL2 reads it 5-10x slower than a Linux path such as ~/market_data" ;;
    esac
    local need=150 free
    [ "$MINUTE" = 0 ] && need=60
    free=$(free_gb "$MASSIVE_ROOT")
    say "Disk: ${free} GB free where the Massive store goes; the full history needs about ${need} GB (with the 50 GB margin the backfill keeps)."
    [ -n "$free" ] && [ "$free" -lt "$need" ] && warn "less free disk than the full history needs; the backfill stops when it reaches its margin (disk_margin_gb)"
    if [ -z "${MASSIVE_API_KEY:-}" ] && ! grep -q '^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}MASSIVE_API_KEY=' "$HOME/.massive.env" 2>/dev/null; then
        warn "MASSIVE_API_KEY is neither in the environment nor in ~/.massive.env (the next check will tell)"
    fi

    if has "$selected" massive && [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
        [ -t 0 ] || die "not a terminal; pass --yes to start the download without the question"
        printf '\nSteps: %s\nThe Massive backfill takes hours and about %s GB. Continue? [y/N] ' "$selected" "$need"
        local answer
        read -r answer
        case "$answer" in y|Y|yes|YES) ;; *) die "stopped before downloading anything" ;; esac
    fi

    tool subspace init     # data folders, credentials, what the plan includes
    tool cortical init     # SEC folders and the contact e-mail check
}

step_massive() {
    local args=""
    [ -n "$SINCE" ] && args="--since $SINCE"
    [ "$MINUTE" = 0 ] && args="$args --no-minute"
    # shellcheck disable=SC2086  # word splitting of the options is intended
    tool subspace backfill initial $args
    tool subspace identity build
}

step_edgar() {
    tool cortical backfill
    tool cortical profiles
}

step_finra() {
    tool sensor backfill short-interest
    tool sensor backfill short-volume
}

step_vinculum() {
    tool vinculum build
    say "\$ $BIN/vinculum check"
    if [ "$DRY" = 0 ] && ! "$BIN/vinculum" check; then
        warn "some vinculum regression checks failed (above); the build is there, look at them before research"
    fi
}

step_nanoprobe() { tool nanoprobe build; }
step_queen()     { tool queen build; }

render_unit() {   # tools/systemd/<unit> with this checkout and the run time filled in
    sed -e "s|@REPO@|$REPO|g" -e "s|@RUN_TIME@|$RUN_TIME|g" "$REPO/tools/systemd/$1"
}

step_services() {
    if [ "$OS" = Darwin ]; then
        tool collective service install --replace
        if [ "$STREAM" = 1 ]; then
            tool subspace service install --only stream
        else
            say "stream: not installed (--no-stream)"
        fi
        return
    fi

    RUN_TIME=$("$BIN/python" -c 'from unicomplex import load_settings as l; s = l(); print(f"{s.run_hour:02d}:{s.run_minute:02d}")' 2>/dev/null)
    [ -n "$RUN_TIME" ] || RUN_TIME="06:30"
    local units="collective-sync.service collective-sync.timer" unit
    [ "$STREAM" = 1 ] && units="$units collective-stream.service"

    if command -v systemctl > /dev/null && systemctl --user show-environment > /dev/null 2>&1; then
        for unit in $units; do
            say "\$ install $UNIT_DIR/$unit"
            if [ "$DRY" = 0 ]; then
                mkdir -p "$UNIT_DIR"
                render_unit "$unit" > "$UNIT_DIR/$unit"
            fi
        done
        run systemctl --user daemon-reload
        run systemctl --user enable --now collective-sync.timer
        if [ "$STREAM" = 1 ]; then
            warn "starting the stream here: stop any other machine's stream first (one websocket per API key)"
            run systemctl --user enable --now collective-stream.service
        else
            say "stream: not installed (Linux default; --stream installs it)"
        fi
        # Without lingering, user units stop at logout and never start before a login.
        if command -v loginctl > /dev/null && [ "$DRY" = 0 ] && \
           [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
            loginctl enable-linger "$USER" 2>/dev/null || \
                warn "could not enable lingering; run \`sudo loginctl enable-linger $USER\` so the timer runs without a login"
        fi
        [ "$WSL" = 1 ] && say "WSL2: the timer only fires while the WSL VM runs (Windows stops it some time after the last terminal closes)."
        run systemctl --user list-timers collective-sync.timer --no-pager
        return
    fi

    # No systemd (WSL2 default): cron for the nightly run, nothing for the stream.
    warn "no systemd user manager. For the units, add to /etc/wsl.conf:  [boot] systemd=true  then run \`wsl.exe --shutdown\` in Windows and \`tools/bootstrap.sh --only services\`. Using cron for now."
    if ! command -v crontab > /dev/null; then
        [ "$DRY" = 1 ] || die "neither systemd nor crontab is available; schedule tools/nightly_sync.sh yourself"
        warn "no crontab command either; a real run would stop here"
        return 0
    fi
    local hour=${RUN_TIME%%:*} minute=${RUN_TIME##*:}
    hour=${hour#0} minute=${minute#0}
    local line="${minute:-0} ${hour:-0} * * 2-6 \"$REPO/tools/nightly_sync.sh\" >> \"$UC_ROOT/_logs/nightly.cron.log\" 2>&1 $CRON_TAG"
    say "\$ crontab: $line"
    if [ "$DRY" = 0 ]; then
        { crontab -l 2>/dev/null | grep -v -e "$CRON_TAG" -e nightly_sync.sh; say "$line"; } | crontab -
    fi
    say "cron must be running: \`sudo service cron start\` (WSL2 starts no services by itself)."
    [ "$STREAM" = 1 ] && warn "the stream needs systemd here; it was not installed"
}

step_status() {
    say "\$ $BIN/collective status"
    [ "$DRY" = 1 ] && return 0
    # Exit 1 means something is stale (the stream before the next session, say); not a failure here.
    "$BIN/collective" status || true
}

# ------------------------------------------------------------------ main
say "collective-alpha bootstrap: $OS$([ "$WSL" = 1 ] && echo ' (WSL2)'), repo $REPO"
say "steps: $selected$([ "$DRY" = 1 ] && echo '   (dry run: nothing runs)')"
MASSIVE_ROOT="" UC_ROOT="" LOG="" RUN_TIME="06:30"
T0=$(date +%s)
for CURRENT in $selected; do
    head_ "$CURRENT  ($(date '+%Y-%m-%d %H:%M'))"
    "step_$CURRENT"
done
mins=$(( ($(date +%s) - T0) / 60 ))
head_ "Bootstrap finished in ${mins} min.$([ -n "$LOG" ] && echo " Log: $LOG")"
